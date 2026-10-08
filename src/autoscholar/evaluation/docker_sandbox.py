"""Windows-compatible injection of the real Docker executor via dedicated-controller stdio."""

import asyncio
import json
import re
from contextlib import suppress

from autoscholar.coding.sandbox import (
    SandboxError,
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.evaluation.datasets import decode_json

MAX_RESPONSE_BYTES = 90 * 1024 * 1024


class DockerEvaluationSandbox:
    def __init__(self, container: str) -> None:
        if not re.fullmatch(r"autoscholar-eval-[a-z0-9-]{1,45}-sandbox-manager-1", container):
            raise ValueError("Use a dedicated autoscholar-eval-* controller, not an app container")
        self.container = container
        self.verified = False
        self.context: str | None = None

    async def _metadata(self, *arguments: str) -> bytes:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
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

    async def _verify_controller(self) -> None:
        context = (await self._metadata("context", "show")).decode("utf-8").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", context):
            raise ValueError("Invalid Docker context name")
        endpoint = decode_json(
            await self._metadata(
                "context",
                "inspect",
                context,
                "--format",
                "{{json .Endpoints.docker.Host}}",
            )
        )
        if not isinstance(endpoint, str) or not endpoint.startswith(("npipe://", "unix://")):
            raise ValueError("Only a local Docker socket context is allowed")
        self.context = context
        process = await asyncio.create_subprocess_exec(
            "docker",
            "--context",
            context,
            "inspect",
            "--format",
            "{{json .Config.Labels}}",
            self.container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode != 0 or len(output) > 65536:
            raise ValueError("Evaluation controller does not exist")
        labels = decode_json(output)
        project = self.container.removesuffix("-sandbox-manager-1")
        if not isinstance(labels, dict) or labels.get("com.docker.compose.project") != project:
            raise ValueError("Controller ownership label differs")
        if labels.get("com.docker.compose.service") != "sandbox-manager":
            raise ValueError("Unexpected evaluation controller service")
        networks = decode_json(
            await self._metadata(
                "--context",
                context,
                "inspect",
                "--format",
                "{{json .NetworkSettings.Networks}}",
                self.container,
            )
        )
        if not isinstance(networks, dict) or set(networks) != {project + "_isolated"}:
            raise ValueError("Controller must use only its dedicated isolated network")
        internal = await self._metadata(
            "--context",
            context,
            "network",
            "inspect",
            project + "_isolated",
            "--format",
            "{{.Internal}}",
        )
        if internal.strip() != b"true":
            raise ValueError("Controller network must have external egress disabled")
        self.verified = True

    async def _request(self, payload: dict[str, object]) -> object:
        if not self.verified:
            await self._verify_controller()
        process = await asyncio.create_subprocess_exec(
            "docker",
            "--context",
            self.context or "",
            "exec",
            "-i",
            self.container,
            "python",
            "-m",
            "autoscholar.evaluation.sandbox_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdin is not None and process.stdout is not None
        output_reader = process.stdout
        try:
            process.stdin.write(
                json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() + b"\n"
            )
            await process.stdin.drain()
            output = bytearray()
            while block := await process.stdout.read(65536):
                output.extend(block)
                if len(output) > MAX_RESPONSE_BYTES:
                    raise ValueError("Worker response exceeds sandbox artifact bound")
            await process.wait()
            if process.returncode != 0:
                raise SandboxError("evaluation_worker_failed", "Dedicated sandbox worker failed")
            return decode_json(bytes(output))
        finally:
            # Keep stdin open while running; closing it on cancellation signals Linux EOF.
            # The worker cancels and JOINS the executor's container/volume cleanup.
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()
            if process.returncode is None:

                async def discard_stdout() -> None:
                    while await output_reader.read(65536):
                        pass

                draining = asyncio.create_task(discard_stdout())
                try:
                    await asyncio.wait_for(asyncio.gather(process.wait(), draining), timeout=25)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    raise SandboxError(
                        "evaluation_cleanup_timeout", "Controller cleanup did not finish"
                    ) from None
                finally:
                    if not draining.done():
                        draining.cancel()
                    await asyncio.gather(draining, return_exceptions=True)

    async def health(self) -> SandboxHealth:
        return SandboxHealth.model_validate(await self._request({"operation": "health"}))

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        return SandboxRunResult.model_validate(
            await self._request({"operation": "run", "request": request.model_dump()})
        )

    async def close(self) -> None:
        pass
