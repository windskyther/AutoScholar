import asyncio
import base64
import hashlib
import io
import json
import re
import struct
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

SandboxAction = Literal["static_check", "run_python", "run_pytest", "run_shell"]
logger = structlog.get_logger(__name__)


class SandboxError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SandboxRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    action: SandboxAction
    path: str | None = Field(default=None, max_length=500)
    args: list[str] = Field(default_factory=list, max_length=30)
    files: dict[str, str] = Field(max_length=100)
    collect_artifacts: list[str] = Field(default_factory=list, max_length=20)
    timeout_seconds: int = Field(default=300, ge=1, le=600)

    @field_validator("args")
    @classmethod
    def validate_args(cls, args: list[str]) -> list[str]:
        if any(len(arg) > 500 or "\x00" in arg for arg in args):
            raise ValueError("sandbox arguments are invalid")
        return args

    @field_validator("files")
    @classmethod
    def validate_files(cls, files: dict[str, str]) -> dict[str, str]:
        total = 0
        for path, content in files.items():
            cls._validate_relative_path(path)
            size = len(content.encode("utf-8"))
            if size > 1_048_576:
                raise ValueError("sandbox file exceeds 1 MiB")
            total += size
        if total > 10_485_760:
            raise ValueError("sandbox source snapshot exceeds 10 MiB")
        return files

    @field_validator("collect_artifacts")
    @classmethod
    def validate_artifacts(cls, paths: list[str]) -> list[str]:
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must not contain duplicates")
        for path in paths:
            cls._validate_relative_path(path)
        return paths

    @model_validator(mode="after")
    def validate_action(self) -> "SandboxRunRequest":
        if self.action in {"run_python", "run_shell"} and self.path is None:
            raise ValueError(f"path is required for {self.action}")
        if self.path is not None:
            if self.action == "run_shell":
                if self.path not in {"python", "pytest", "ruff"}:
                    raise ValueError("shell executable is not allowed")
            else:
                self._validate_relative_path(self.path)
        if self.collect_artifacts and self.action != "run_python":
            raise ValueError("artifacts can only be collected from Python experiment runs")
        return self

    @staticmethod
    def _validate_relative_path(path: str) -> None:
        normalized = path.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if (
            not normalized
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            or pure.is_absolute()
            or normalized.startswith("./")
            or ":" in pure.parts[0]
            or any(part in {"", ".", ".."} for part in normalized.split("/"))
        ):
            raise ValueError("sandbox path must be a safe relative path")


class SandboxArtifact(BaseModel):
    path: str
    size_bytes: int
    sha256: str
    data_base64: str


class SandboxRunResult(BaseModel):
    status: Literal["succeeded", "failed", "timed_out"]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    truncated: bool = False
    artifacts: list[SandboxArtifact] = Field(default_factory=list)


class SandboxHealth(BaseModel):
    status: Literal["ok", "error"]
    engine: bool
    image: bool
    mnist_dataset: bool
    dataset_id: str | None = None
    dataset_sha256: str | None = None
    image_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class SandboxExecutor(Protocol):
    async def run(self, request: SandboxRunRequest) -> SandboxRunResult: ...

    async def health(self) -> SandboxHealth: ...

    async def close(self) -> None: ...


class SandboxClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: SecretStr | None = None,
        timeout_seconds: float = 310,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or any(character.isspace() or character == "\\" for character in base_url)
        ):
            raise ValueError("Sandbox manager requires a service HTTP(S) origin")
        self._token = token.get_secret_value() if token else ""
        if self._token and (
            not 32 <= len(self._token) <= 4096
            or not self._token.isascii()
            or any(not 33 <= ord(char) <= 126 for char in self._token)
        ):
            raise ValueError("Sandbox manager requires a bounded service credential")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            headers={"Authorization": "Bearer " + self._token} if self._token else {},
        )

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        if not self._token:
            raise SandboxError(
                "sandbox_auth_not_configured", "Sandbox service authentication is required"
            )
        try:
            response = await self._client.post("/internal/v1/run", json=request.model_dump())
            response.raise_for_status()
            return SandboxRunResult.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise SandboxError(
                "sandbox_unavailable", "The isolated code runner is unavailable"
            ) from exc

    async def health(self) -> SandboxHealth:
        if not self._token:
            return SandboxHealth(status="error", engine=False, image=False, mnist_dataset=False)
        try:
            response = await self._client.get("/internal/health", timeout=3)
            response.raise_for_status()
            return SandboxHealth.model_validate(response.json())
        except (httpx.HTTPError, ValueError):
            return SandboxHealth(status="error", engine=False, image=False, mnist_dataset=False)

    async def close(self) -> None:
        await self._client.aclose()


@dataclass(frozen=True, slots=True)
class DockerLimits:
    memory_bytes: int = 4 * 1024 * 1024 * 1024
    nano_cpus: int = 2_000_000_000
    pids_limit: int = 256
    max_output_bytes: int = 65_536
    max_artifact_file_bytes: int = 16_777_216
    max_artifact_bytes: int = 67_108_864


class DockerSandboxExecutor:
    """Narrow Docker Engine client used only by the internal sandbox manager."""

    def __init__(
        self,
        *,
        image: str,
        dataset_volume: str,
        dataset_ready_file: str,
        docker_socket: str = "/var/run/docker.sock",
        limits: DockerLimits | None = None,
        evaluation_owner: str | None = None,
    ) -> None:
        transport = httpx.AsyncHTTPTransport(uds=docker_socket)
        self._client = httpx.AsyncClient(
            base_url="http://docker", transport=transport, timeout=20, trust_env=False
        )
        self._image = image
        self._dataset_volume = dataset_volume
        self._dataset_ready_file = dataset_ready_file
        self._limits = limits or DockerLimits()
        self._resource_labels = {
            "autoscholar.sandbox": "true",
            "autoscholar.instance": uuid4().hex,
        } | ({"autoscholar.evaluation": evaluation_owner} if evaluation_owner else {})

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        started = asyncio.get_running_loop().time()
        container_id: str | None = None
        loader_id: str | None = None
        volume_name = f"autoscholar-workspace-{uuid4().hex}"
        loader_name = f"autoscholar-loader-{uuid4().hex[:16]}"
        runner_name = f"autoscholar-{request.task_id[:24]}-{uuid4().hex[:16]}"
        labels = {**self._resource_labels, "autoscholar.operation": uuid4().hex}
        loader_ref: str | None = None
        runner_ref: str | None = None
        timed_out = False
        try:
            volume_response = await self._client.post(
                "/volumes/create",
                json={
                    "Name": volume_name,
                    "Labels": {"autoscholar.temporary": "true", **labels},
                },
            )
            self._raise_engine_error(volume_response)
            if not await self._owned("volume", volume_name, labels):
                raise SandboxError(
                    "sandbox_ownership_missing", "Workspace ownership was not verified"
                )
            loader_ref = loader_name
            loader_response = await self._client.post(
                "/containers/create",
                params={"name": loader_name},
                json={**self._loader_config(volume_name), "Labels": labels},
            )
            self._raise_engine_error(loader_response)
            loader_id = self._container_id(loader_response.json()["Id"])
            loader_ref = loader_id
            if not await self._owned("container", loader_id, labels):
                raise SandboxError("sandbox_ownership_missing", "Loader ownership was not verified")
            loader_start = await self._client.post(f"/containers/{loader_id}/start")
            self._raise_engine_error(loader_start)
            archive = self._source_archive(request.files)
            archive_response = await self._client.put(
                f"/containers/{loader_id}/archive",
                params={"path": "/workspace"},
                content=archive,
                headers={"Content-Type": "application/x-tar"},
            )
            self._raise_engine_error(archive_response)
            await self._remove_owned("container", loader_id, labels)
            loader_id = None
            loader_ref = None
            runner_ref = runner_name
            response = await self._client.post(
                "/containers/create",
                params={"name": runner_name},
                json={**self._container_config(request, volume_name), "Labels": labels},
            )
            self._raise_engine_error(response)
            container_id = self._container_id(response.json()["Id"])
            runner_ref = container_id
            if not await self._owned("container", container_id, labels):
                raise SandboxError("sandbox_ownership_missing", "Runner ownership was not verified")
            start_response = await self._client.post(f"/containers/{container_id}/start")
            self._raise_engine_error(start_response)
            try:
                wait_response = await asyncio.wait_for(
                    self._client.post(
                        f"/containers/{container_id}/wait",
                        params={"condition": "not-running"},
                        timeout=request.timeout_seconds + 10,
                    ),
                    timeout=request.timeout_seconds,
                )
                self._raise_engine_error(wait_response)
                exit_code = int(wait_response.json()["StatusCode"])
            except TimeoutError:
                timed_out = True
                exit_code = None
                await self._client.post(f"/containers/{container_id}/kill")
            stdout, stderr, truncated = await self._logs(container_id)
            status: Literal["succeeded", "failed", "timed_out"] = (
                "timed_out" if timed_out else ("succeeded" if exit_code == 0 else "failed")
            )
            artifacts: list[SandboxArtifact] = []
            if status == "succeeded" and request.collect_artifacts:
                artifacts, missing = await self._collect_artifacts(
                    container_id, request.collect_artifacts
                )
                if missing:
                    status = "failed"
                    missing_text = ", ".join(missing)
                    stderr = f"{stderr}\nMissing required artifacts: {missing_text}".strip()
            return SandboxRunResult(
                status=status,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=round((asyncio.get_running_loop().time() - started) * 1_000, 3),
                truncated=truncated,
                artifacts=artifacts,
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise SandboxError(
                "sandbox_execution_failed", "The Docker sandbox could not execute the task"
            ) from exc
        finally:
            cleanup = asyncio.create_task(
                self._cleanup(loader_ref, runner_ref, volume_name, labels)
            )
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            await cleanup
            if cancelled:
                raise asyncio.CancelledError

    @staticmethod
    def _container_id(value: object) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
            raise SandboxError("sandbox_object_invalid", "Invalid Docker object identity")
        return value

    async def _owned(self, kind: str, name: str, labels: dict[str, str]) -> bool:
        path = f"/containers/{name}/json" if kind == "container" else f"/volumes/{name}"
        response = await self._client.get(path, timeout=3)
        if response.status_code == 404:
            return False
        self._raise_engine_error(response)
        record = response.json()
        if not isinstance(record, dict):
            raise SandboxError("sandbox_ownership_mismatch", "Invalid Docker ownership record")
        config = record.get("Config")
        observed = (
            config.get("Labels")
            if kind == "container" and isinstance(config, dict)
            else (record.get("Labels") if kind == "volume" else None)
        )
        if not isinstance(observed, dict) or any(
            observed.get(key) != value for key, value in labels.items()
        ):
            raise SandboxError(
                "sandbox_ownership_mismatch", "Docker object ownership was not verified"
            )
        return True

    async def _remove_owned(self, kind: str, name: str, labels: dict[str, str]) -> None:
        if not await self._owned(kind, name, labels):
            return
        path = f"/containers/{name}" if kind == "container" else f"/volumes/{name}"
        response = await self._client.delete(
            path, params={"force": "true", "v": "false"} if kind == "container" else {}, timeout=3
        )
        if response.status_code != 404:
            self._raise_engine_error(response)

    async def _cleanup(
        self, loader: str | None, runner: str | None, volume: str, labels: dict[str, str]
    ) -> None:
        for kind, name in (("container", loader), ("container", runner), ("volume", volume)):
            if name is None:
                continue
            try:
                async with asyncio.timeout(7):
                    await self._remove_owned(kind, name, labels)
            except (httpx.HTTPError, SandboxError, TimeoutError, ValueError):
                logger.error(
                    "sandbox_cleanup_incomplete",
                    resource_type=kind,
                    operation_id=labels["autoscholar.operation"],
                )

    async def health(self) -> SandboxHealth:
        engine = image = False
        image_sha256: str | None = None
        try:
            ping = await self._client.get("/_ping", timeout=3)
            engine = ping.status_code == 200
            inspected = await self._client.get(f"/images/{self._image}/json", timeout=3)
            image = inspected.status_code == 200
            if image:
                identifier = str(inspected.json().get("Id", ""))
                candidate = identifier.removeprefix("sha256:")
                if len(candidate) == 64 and all(
                    character in "0123456789abcdef" for character in candidate
                ):
                    image_sha256 = candidate
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
        dataset = PurePosixPath(self._dataset_ready_file)
        dataset_ready = (
            dataset.as_posix().startswith("/datasets/mnist/")
            and Path(self._dataset_ready_file).is_file()
        )
        dataset_id: str | None = None
        dataset_sha256: str | None = None
        if dataset_ready:
            try:
                manifest = json.loads(Path(self._dataset_ready_file).read_text(encoding="utf-8"))
                candidate_id = manifest.get("dataset_id")
                candidate_sha = manifest.get("dataset_sha256")
                if (
                    candidate_id == "mnist"
                    and isinstance(candidate_sha, str)
                    and len(candidate_sha) == 64
                ):
                    dataset_id = candidate_id
                    dataset_sha256 = candidate_sha
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                pass
        return SandboxHealth(
            status="ok" if engine and image else "error",
            engine=engine,
            image=image,
            mnist_dataset=dataset_ready,
            dataset_id=dataset_id,
            dataset_sha256=dataset_sha256,
            image_sha256=image_sha256,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _container_config(
        self, request: SandboxRunRequest, workspace_volume: str = "autoscholar-test-workspace"
    ) -> dict[str, Any]:
        return {
            "Image": self._image,
            "Labels": self._resource_labels,
            "Cmd": self._command(request),
            "WorkingDir": "/workspace/source",
            "User": "65532:65532",
            "Env": [
                "HOME=/tmp",
                "PYTHONIOENCODING=utf-8",
                "PYTHONDONTWRITEBYTECODE=1",
                "MNIST_ROOT=/datasets/mnist",
            ],
            "NetworkDisabled": True,
            "HostConfig": {
                "NetworkMode": "none",
                "LogConfig": {
                    "Type": "local",
                    "Config": {"max-size": "1m", "max-file": "1", "compress": "false"},
                },
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Memory": self._limits.memory_bytes,
                "NanoCpus": self._limits.nano_cpus,
                "PidsLimit": self._limits.pids_limit,
                "Tmpfs": {
                    "/tmp": "rw,noexec,nosuid,size=67108864,mode=1777",
                },
                "Mounts": [
                    {
                        "Type": "volume",
                        "Source": workspace_volume,
                        "Target": "/workspace",
                        "ReadOnly": False,
                    },
                    {
                        "Type": "volume",
                        "Source": self._dataset_volume,
                        "Target": "/datasets/mnist",
                        "ReadOnly": True,
                    },
                ],
            },
        }

    def _loader_config(self, workspace_volume: str) -> dict[str, Any]:
        return {
            "Image": self._image,
            "Labels": self._resource_labels,
            "Cmd": ["python", "-c", "import time; time.sleep(30)"],
            "WorkingDir": "/",
            "User": "65532:65532",
            "Env": ["HOME=/tmp", "PYTHONIOENCODING=utf-8"],
            "NetworkDisabled": True,
            "HostConfig": {
                "NetworkMode": "none",
                "LogConfig": {
                    "Type": "local",
                    "Config": {"max-size": "1m", "max-file": "1", "compress": "false"},
                },
                "ReadonlyRootfs": False,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Memory": 268_435_456,
                "NanoCpus": 500_000_000,
                "PidsLimit": 32,
                "Mounts": [
                    {
                        "Type": "volume",
                        "Source": workspace_volume,
                        "Target": "/workspace",
                        "ReadOnly": False,
                    }
                ],
            },
        }

    @staticmethod
    def _command(request: SandboxRunRequest) -> list[str]:
        if request.action == "static_check":
            return ["python", "/opt/autoscholar/static_check.py"]
        if request.action == "run_python":
            assert request.path is not None
            return ["python", request.path, *request.args]
        if request.action == "run_pytest":
            return ["python", "-m", "pytest", "-q", *(request.args or ["."])]
        assert request.path is not None
        command = {
            "python": ["python"],
            "pytest": ["python", "-m", "pytest"],
            "ruff": ["ruff"],
        }[request.path]
        return [*command, *request.args]

    @staticmethod
    def _source_archive(files: dict[str, str]) -> bytes:
        for path in files:
            SandboxRunRequest._validate_relative_path(path)
        buffer = io.BytesIO()
        directories: set[PurePosixPath] = {PurePosixPath("source")}
        for path in files:
            pure = PurePosixPath(path)
            for parent in (PurePosixPath("source") / pure).parents:
                if parent != PurePosixPath("."):
                    directories.add(parent)
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for directory in sorted(directories, key=lambda item: (len(item.parts), str(item))):
                info = tarfile.TarInfo(directory.as_posix())
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.uid = info.gid = 65532
                archive.addfile(info)
            for path, content in sorted(files.items()):
                data = content.encode("utf-8")
                info = tarfile.TarInfo((PurePosixPath("source") / path).as_posix())
                info.size = len(data)
                info.mode = 0o644
                info.uid = info.gid = 65532
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    async def _logs(self, container_id: str) -> tuple[str, str, bool]:
        maximum = self._limits.max_output_bytes
        raw_limit = 2 * maximum + 65_536
        data = bytearray()
        capped = False
        async with self._client.stream(
            "GET",
            f"/containers/{container_id}/logs",
            params={"stdout": "true", "stderr": "true"},
        ) as response:
            self._raise_engine_error(response)
            async for chunk in response.aiter_bytes(chunk_size=65_536):
                available = raw_limit - len(data)
                data.extend(chunk[:available])
                if len(chunk) >= available:
                    capped = True
                    break
        stdout, stderr = self._decode_docker_stream(bytes(data))
        truncated = capped or len(stdout) > maximum or len(stderr) > maximum
        return (
            stdout[:maximum].decode("utf-8", errors="replace"),
            stderr[:maximum].decode("utf-8", errors="replace"),
            truncated,
        )

    async def _collect_artifacts(
        self, container_id: str, paths: list[str]
    ) -> tuple[list[SandboxArtifact], list[str]]:
        artifacts: list[SandboxArtifact] = []
        missing: list[str] = []
        total = 0
        for path in paths:
            async with self._client.stream(
                "GET",
                f"/containers/{container_id}/archive",
                params={"path": f"/workspace/source/{path}"},
                timeout=30,
            ) as response:
                if response.status_code == 404:
                    missing.append(path)
                    continue
                if response.status_code >= 400:
                    raise SandboxError(
                        "sandbox_engine_error", "Docker could not return an artifact"
                    )
                archive_buffer = bytearray()
                archive_limit = self._limits.max_artifact_file_bytes + 1_048_576
                async for chunk in response.aiter_bytes(chunk_size=65_536):
                    archive_buffer.extend(chunk)
                    if len(archive_buffer) > archive_limit:
                        raise SandboxError(
                            "sandbox_artifact_too_large",
                            f"Artifact archive exceeds size limit: {path}",
                        )
            data = self._read_archive_file(
                bytes(archive_buffer), path, self._limits.max_artifact_file_bytes
            )
            if len(data) > self._limits.max_artifact_file_bytes:
                raise SandboxError(
                    "sandbox_artifact_too_large", f"Artifact exceeds size limit: {path}"
                )
            total += len(data)
            if total > self._limits.max_artifact_bytes:
                raise SandboxError(
                    "sandbox_artifacts_too_large", "Collected artifacts exceed total size limit"
                )
            artifacts.append(
                SandboxArtifact(
                    path=path,
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    data_base64=base64.b64encode(data).decode("ascii"),
                )
            )
        return artifacts, missing

    @staticmethod
    def _read_archive_file(
        archive_data: bytes, expected_path: str, max_bytes: int = 16_777_216
    ) -> bytes:
        try:
            if len(archive_data) > max_bytes + 1_048_576:
                raise SandboxError(
                    "sandbox_artifact_too_large", "Artifact archive exceeds size limit"
                )
            with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:") as archive:
                members: list[tarfile.TarInfo] = []
                for member in archive:
                    if len(members) >= 32:
                        raise SandboxError("sandbox_artifact_invalid", "Excessive archive members")
                    name = member.name.rstrip("/")
                    SandboxRunRequest._validate_relative_path(name)
                    if not (member.isdir() or member.isfile()) or member.sparse is not None:
                        raise SandboxError("sandbox_artifact_invalid", "Unsupported archive member")
                    members.append(member)
                regular = [member for member in members if member.isfile()]
                if len(regular) != 1 or any(member.issym() or member.islnk() for member in members):
                    raise SandboxError(
                        "sandbox_artifact_invalid", f"Invalid artifact archive: {expected_path}"
                    )
                if regular[0].name not in {expected_path, PurePosixPath(expected_path).name}:
                    raise SandboxError(
                        "sandbox_artifact_invalid",
                        f"Artifact archive path mismatch: {expected_path}",
                    )
                if regular[0].size > max_bytes:
                    raise SandboxError(
                        "sandbox_artifact_too_large",
                        f"Artifact exceeds size limit: {expected_path}",
                    )
                extracted = archive.extractfile(regular[0])
                if extracted is None:
                    raise SandboxError(
                        "sandbox_artifact_invalid", f"Artifact could not be read: {expected_path}"
                    )
                return extracted.read(max_bytes + 1)
        except (tarfile.TarError, ValueError) as exc:
            raise SandboxError(
                "sandbox_artifact_invalid", f"Invalid artifact archive: {expected_path}"
            ) from exc

    @staticmethod
    def _decode_docker_stream(data: bytes) -> tuple[bytes, bytes]:
        stdout = bytearray()
        stderr = bytearray()
        position = 0
        while position + 8 <= len(data):
            stream_type = data[position]
            length = struct.unpack(">I", data[position + 4 : position + 8])[0]
            position += 8
            payload = data[position : position + length]
            position += length
            (stderr if stream_type == 2 else stdout).extend(payload)
        if position == 0 and data:
            stdout.extend(data)
        return bytes(stdout), bytes(stderr)

    @staticmethod
    def _raise_engine_error(response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise SandboxError("sandbox_engine_error", "Docker Engine request failed")
