"""Explicit persistent Docker inference, without networking, keys or application volumes."""

import asyncio
import re
from contextlib import suppress
from typing import Annotated
from uuid import uuid4

from pydantic import Field

from autoscholar.evaluation.datasets import decode_json
from autoscholar.evaluation.models import EvaluationModel
from autoscholar.evaluation.neural_worker import (
    DENSE_MODEL,
    MAX_PACKET_BYTES,
    MODEL_REPOSITORIES,
    RERANKER_MODEL,
    SPARSE_MODEL,
    Method,
    NeuralRequest,
    NeuralResponse,
    inference_code_digest,
)


class NeuralIdentity(EvaluationModel):
    resources: dict[str, Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]]
    models: list[str] = Field(min_length=3, max_length=3)
    snapshot_revisions: dict[str, Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]]
    fastembed: str = Field(max_length=40)
    onnxruntime: str = Field(max_length=40)
    cpu_only: bool
    local_files_only: bool
    setup_inference_calls: dict[str, Annotated[int, Field(ge=0)]]
    isolation_verified: bool


async def metadata(*arguments: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "docker", *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0 or len(output) > 65536:
        raise ValueError("Local Docker metadata unavailable")
    return output


class DockerNeuralClient:
    def __init__(self) -> None:
        self.name = "autoscholar-eval-neural-" + uuid4().hex
        self.context: str | None = None
        self.image_sha256: str | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.identity: NeuralIdentity | None = None
        self.counts: dict[str, int] = {}
        self.sequence = 0
        self.lock = asyncio.Lock()
        self.closed = False
        self.cleanup_verified = False

    async def start(self, *, source: str, image: str, cache_volume: str) -> NeuralIdentity:
        if not re.fullmatch(r"[a-zA-Z0-9._/-]+(?::[a-zA-Z0-9._-]+)?", image):
            raise ValueError("Invalid explicit cached image")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", cache_volume):
            raise ValueError("Invalid model cache volume")
        context = (await metadata("context", "show")).decode("utf-8").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", context):
            raise ValueError("Invalid local Docker context")
        endpoint = decode_json(
            await metadata(
                "context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}"
            )
        )
        if not isinstance(endpoint, str) or not endpoint.startswith(("npipe://", "unix://")):
            raise ValueError("Only a local Docker socket is allowed")
        self.context = context
        image_id = (
            (await metadata("--context", context, "image", "inspect", image, "--format", "{{.Id}}"))
            .decode()
            .strip()
        )
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise ValueError("A pinned cached inference image is required")
        self.image_sha256 = image_id.removeprefix("sha256:")
        await metadata("--context", context, "volume", "inspect", cache_volume)
        # No .git/.env/design/app data mounts, Docker socket, ports, GPU or provider env.
        self.process = await asyncio.create_subprocess_exec(
            "docker",
            "--context",
            context,
            "run",
            "--rm",
            "--pull",
            "never",
            "-i",
            "--name",
            self.name,
            "--label",
            "autoscholar.evaluation=" + self.name,
            "--network",
            "none",
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "6442450944",
            "--cpus",
            "2",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=67108864",
            "--mount",
            f"type=bind,source={source},target=/app/src,readonly",
            "--mount",
            f"type=volume,source={cache_volume},target=/models,readonly",
            "--env",
            "HF_HUB_OFFLINE=1",
            "--env",
            "TRANSFORMERS_OFFLINE=1",
            "--env",
            "TOKENIZERS_PARALLELISM=false",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint",
            "python",
            image_id,
            "-u",
            "-m",
            "autoscholar.evaluation.neural_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_PACKET_BYTES + 1,
        )
        try:
            self.identity = NeuralIdentity.model_validate(
                await asyncio.wait_for(self.call("identity", {}), timeout=180)
            )
            if (
                not self.identity.cpu_only
                or not self.identity.local_files_only
                or not self.identity.isolation_verified
                or self.identity.models != [DENSE_MODEL, SPARSE_MODEL, RERANKER_MODEL]
                or set(self.identity.snapshot_revisions) != set(MODEL_REPOSITORIES)
                or set(self.identity.resources) != {*MODEL_REPOSITORIES, "inference_code"}
                or self.identity.setup_inference_calls
                != {"dense_query": 1, "sparse_query": 1, "rerank": 1}
                or self.identity.resources.get("inference_code") != inference_code_digest()
            ):
                raise ValueError("Inference identity is not cache-only CPU")
            return self.identity
        except BaseException:
            await self.close()
            raise

    async def call(self, method: Method, payload: dict[str, object]) -> object:
        async with self.lock:
            process = self.process
            if self.closed or process is None or process.stdin is None or process.stdout is None:
                raise ValueError("No active isolated neural worker; no host fallback")
            self.sequence += 1
            request = NeuralRequest(request_id=self.sequence, method=method, payload=payload)
            raw = request.model_dump_json().encode("utf-8") + b"\n"
            if len(raw) > MAX_PACKET_BYTES:
                raise ValueError("Inference request exceeds public payload limit")
            try:
                process.stdin.write(raw)
                await process.stdin.drain()
                response_raw = await process.stdout.readline()
                if not response_raw or len(response_raw) > MAX_PACKET_BYTES:
                    raise ValueError("Invalid bounded inference response")
                response = NeuralResponse.model_validate(decode_json(response_raw))
                if response.request_id != self.sequence:
                    raise ValueError("Inference response belongs to another operation")
                if not response.ok or response.error is not None:
                    raise ValueError("Cached neural inference failed; no download or fallback")
                self.counts = response.counts
                return response.data
            except BaseException:
                # Stop an in-flight model after timeout/cancel; do not reuse a shifted stream.
                await self.close()
                raise

    async def close(self) -> None:
        if self.cleanup_verified:
            return
        self.closed = True
        process = self.process
        if process and process.stdin:
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()
        if process and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=8)
            except TimeoutError:
                # Only this fresh UUID/ownership-labelled container may be stopped.
                assert self.context is not None
                remaining = await metadata(
                    "--context", self.context, "ps", "-aq", "--filter", "name=^/" + self.name + "$"
                )
                if remaining.strip():
                    try:
                        labels = decode_json(
                            await metadata(
                                "--context",
                                self.context,
                                "inspect",
                                self.name,
                                "--format",
                                "{{json .Config.Labels}}",
                            )
                        )
                    except ValueError:
                        # --rm can finish between ps and inspect; still require absence.
                        if (
                            await metadata(
                                "--context",
                                self.context,
                                "ps",
                                "-aq",
                                "--filter",
                                "name=^/" + self.name + "$",
                            )
                        ).strip():
                            raise
                    else:
                        if (
                            not isinstance(labels, dict)
                            or labels.get("autoscholar.evaluation") != self.name
                        ):
                            raise ValueError("Neural cleanup ownership mismatch") from None
                        await metadata("--context", self.context, "stop", "--time", "5", self.name)
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
        if self.context:
            remaining = await metadata(
                "--context",
                self.context,
                "ps",
                "-aq",
                "--filter",
                "label=autoscholar.evaluation=" + self.name,
            )
            if remaining.strip():
                raise ValueError("Neural inference container remains after joined cleanup")
        self.cleanup_verified = True
