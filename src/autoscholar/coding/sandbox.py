import asyncio
import io
import struct
import tarfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

SandboxAction = Literal["static_check", "run_python", "run_pytest", "run_shell"]


class SandboxError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SandboxRunRequest(BaseModel):
    task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    action: SandboxAction
    path: str | None = Field(default=None, max_length=500)
    args: list[str] = Field(default_factory=list, max_length=30)
    files: dict[str, str] = Field(max_length=100)
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
        return self

    @staticmethod
    def _validate_relative_path(path: str) -> None:
        normalized = path.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if (
            not normalized
            or pure.is_absolute()
            or normalized.startswith("./")
            or ":" in pure.parts[0]
            or any(part in {"", ".", ".."} for part in normalized.split("/"))
        ):
            raise ValueError("sandbox path must be a safe relative path")


class SandboxRunResult(BaseModel):
    status: Literal["succeeded", "failed", "timed_out"]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    truncated: bool = False


class SandboxHealth(BaseModel):
    status: Literal["ok", "error"]
    engine: bool
    image: bool
    mnist_dataset: bool


class SandboxExecutor(Protocol):
    async def run(self, request: SandboxRunRequest) -> SandboxRunResult: ...

    async def health(self) -> SandboxHealth: ...

    async def close(self) -> None: ...


class SandboxClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 310) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        try:
            response = await self._client.post("/internal/v1/run", json=request.model_dump())
            response.raise_for_status()
            return SandboxRunResult.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise SandboxError(
                "sandbox_unavailable", "The isolated code runner is unavailable"
            ) from exc

    async def health(self) -> SandboxHealth:
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
    ) -> None:
        transport = httpx.AsyncHTTPTransport(uds=docker_socket)
        self._client = httpx.AsyncClient(
            base_url="http://docker", transport=transport, timeout=20
        )
        self._image = image
        self._dataset_volume = dataset_volume
        self._dataset_ready_file = dataset_ready_file
        self._limits = limits or DockerLimits()

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        started = asyncio.get_running_loop().time()
        container_id: str | None = None
        timed_out = False
        try:
            response = await self._client.post(
                "/containers/create",
                params={"name": f"autoscholar-{request.task_id[:24]}-{uuid4().hex[:8]}"},
                json=self._container_config(request),
            )
            self._raise_engine_error(response)
            container_id = str(response.json()["Id"])
            start_response = await self._client.post(f"/containers/{container_id}/start")
            self._raise_engine_error(start_response)
            archive = self._source_archive(request.files)
            archive_response = await self._client.put(
                f"/containers/{container_id}/archive",
                params={"path": "/workspace"},
                content=archive,
                headers={"Content-Type": "application/x-tar"},
            )
            self._raise_engine_error(archive_response)
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
            return SandboxRunResult(
                status=status,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=round((asyncio.get_running_loop().time() - started) * 1_000, 3),
                truncated=truncated,
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise SandboxError(
                "sandbox_execution_failed", "The Docker sandbox could not execute the task"
            ) from exc
        finally:
            if container_id is not None:
                with suppress(httpx.HTTPError):
                    await self._client.delete(
                        f"/containers/{container_id}", params={"force": "true", "v": "true"}
                    )

    async def health(self) -> SandboxHealth:
        engine = image = False
        try:
            ping = await self._client.get("/_ping", timeout=3)
            engine = ping.status_code == 200
            inspected = await self._client.get(f"/images/{self._image}/json", timeout=3)
            image = inspected.status_code == 200
        except httpx.HTTPError:
            pass
        dataset = PurePosixPath(self._dataset_ready_file)
        return SandboxHealth(
            status="ok" if engine and image else "error",
            engine=engine,
            image=image,
            mnist_dataset=dataset.as_posix().startswith("/datasets/mnist/")
            and Path(self._dataset_ready_file).is_file(),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _container_config(self, request: SandboxRunRequest) -> dict[str, Any]:
        return {
            "Image": self._image,
            "Cmd": ["python", "/opt/autoscholar/launch.py", *self._command(request)],
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
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Memory": self._limits.memory_bytes,
                "NanoCpus": self._limits.nano_cpus,
                "PidsLimit": self._limits.pids_limit,
                "Tmpfs": {
                    "/tmp": "rw,noexec,nosuid,size=67108864,mode=1777",
                    "/workspace": "rw,nosuid,size=16777216,mode=1777",
                },
                "Mounts": [
                    {
                        "Type": "volume",
                        "Source": self._dataset_volume,
                        "Target": "/datasets/mnist",
                        "ReadOnly": True,
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
            marker = b"ready\n"
            info = tarfile.TarInfo(".autoscholar-source-ready")
            info.size = len(marker)
            info.mode = 0o644
            info.uid = info.gid = 65532
            archive.addfile(info, io.BytesIO(marker))
        return buffer.getvalue()

    async def _logs(self, container_id: str) -> tuple[str, str, bool]:
        response = await self._client.get(
            f"/containers/{container_id}/logs",
            params={"stdout": "true", "stderr": "true"},
        )
        self._raise_engine_error(response)
        stdout, stderr = self._decode_docker_stream(response.content)
        maximum = self._limits.max_output_bytes
        truncated = len(stdout) > maximum or len(stderr) > maximum
        return (
            stdout[:maximum].decode("utf-8", errors="replace"),
            stderr[:maximum].decode("utf-8", errors="replace"),
            truncated,
        )

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
            try:
                detail = str(response.json().get("message") or "Docker Engine request failed")
            except (ValueError, AttributeError):
                detail = "Docker Engine request failed"
            raise SandboxError("sandbox_engine_error", detail[:500])
