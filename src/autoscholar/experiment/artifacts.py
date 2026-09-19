import base64
import binascii
import hashlib
import json
import struct
import zlib
from typing import ClassVar, Protocol

from autoscholar.agent.records import ArtifactRecord, ArtifactType, ExperimentRecord
from autoscholar.coding.sandbox import SandboxArtifact
from autoscholar.coding.workspace import WorkspaceError, WorkspaceManager


class ArtifactError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ArtifactRepository(Protocol):
    async def get_experiment(self, experiment_id: str) -> ExperimentRecord | None: ...

    async def add_artifact(
        self,
        *,
        task_id: str,
        experiment_id: str,
        artifact_type: ArtifactType,
        path: str,
        media_type: str,
        size_bytes: int,
        sha256: str,
    ) -> ArtifactRecord: ...


class ArtifactManager:
    """Validates sandbox outputs before storing task-scoped binary artifacts."""

    allowed: ClassVar[dict[str, tuple[ArtifactType, str]]] = {
        "outputs/raw_metrics.json": ("metrics", "application/json"),
        "outputs/metrics.json": ("metrics", "application/json"),
        "outputs/experiment.json": ("experiment", "application/json"),
        "outputs/loss.png": ("plot", "image/png"),
        "outputs/accuracy.png": ("plot", "image/png"),
        "reports/report.md": ("report", "text/markdown; charset=utf-8"),
        "checkpoints/mlp.pt": ("checkpoint", "application/octet-stream"),
        "checkpoints/cnn.pt": ("checkpoint", "application/octet-stream"),
        "logs/stdout.log": ("stdout", "text/plain; charset=utf-8"),
        "logs/stderr.log": ("stderr", "text/plain; charset=utf-8"),
    }

    def __init__(self, workspace: WorkspaceManager, repository: ArtifactRepository) -> None:
        self._workspace = workspace
        self._repository = repository

    async def save_collected(
        self, task_id: str, experiment_id: str, artifact: SandboxArtifact
    ) -> ArtifactRecord:
        try:
            content = base64.b64decode(artifact.data_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ArtifactError(
                "artifact_encoding_invalid", "Artifact is not valid base64"
            ) from exc
        if len(content) != artifact.size_bytes:
            raise ArtifactError(
                "artifact_size_invalid", "Artifact size does not match its manifest"
            )
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ArtifactError("artifact_digest_invalid", "Artifact digest does not match")
        return await self.save(task_id, experiment_id, artifact.path, content)

    async def save(
        self, task_id: str, experiment_id: str, path: str, content: bytes
    ) -> ArtifactRecord:
        experiment = await self._repository.get_experiment(experiment_id)
        if experiment is None or experiment.task_id != task_id:
            raise ArtifactError("artifact_scope_invalid", "Experiment does not belong to this task")
        kind = self.allowed.get(path)
        if kind is None:
            raise ArtifactError("artifact_path_invalid", "Artifact path is not registered")
        artifact_type, media_type = kind
        self._validate_content(path, content)
        area, relative = path.split("/", 1)
        try:
            file = self._workspace.write_bytes(task_id, relative, content, area=area)
        except WorkspaceError as exc:
            raise ArtifactError(exc.code, exc.message) from exc
        return await self._repository.add_artifact(
            task_id=task_id,
            experiment_id=experiment_id,
            artifact_type=artifact_type,
            path=file.path,
            media_type=media_type,
            size_bytes=file.size_bytes,
            sha256=file.sha256,
        )

    def read_verified(self, task_id: str, artifact: ArtifactRecord) -> bytes:
        if artifact.task_id != task_id or artifact.path not in self.allowed:
            raise ArtifactError("artifact_scope_invalid", "Artifact does not belong to this task")
        area, relative = artifact.path.split("/", 1)
        try:
            content = self._workspace.read_bytes(task_id, relative, area=area)
        except WorkspaceError as exc:
            raise ArtifactError(exc.code, exc.message) from exc
        if (
            len(content) != artifact.size_bytes
            or hashlib.sha256(content).hexdigest() != artifact.sha256
        ):
            raise ArtifactError(
                "artifact_integrity_failed", "Artifact content changed after storage"
            )
        return content

    @staticmethod
    def _validate_content(path: str, content: bytes) -> None:
        if not content:
            raise ArtifactError("artifact_empty", "Artifact must not be empty")
        if path.endswith(".json"):
            try:
                value = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ArtifactError("artifact_json_invalid", "Artifact JSON is invalid") from exc
            if not isinstance(value, dict):
                raise ArtifactError("artifact_json_invalid", "Artifact JSON must be an object")
        elif path.endswith(".png"):
            if len(content) < 33 or content[:8] != b"\x89PNG\r\n\x1a\n":
                raise ArtifactError("artifact_png_invalid", "Artifact is not a PNG")
            if content[12:16] != b"IHDR" or struct.unpack(">I", content[8:12])[0] != 13:
                raise ArtifactError("artifact_png_invalid", "PNG header is invalid")
            width, height = struct.unpack(">II", content[16:24])
            expected_crc = struct.unpack(">I", content[29:33])[0]
            actual_crc = zlib.crc32(content[12:29]) & 0xFFFFFFFF
            if not (1 <= width <= 4096 and 1 <= height <= 4096) or expected_crc != actual_crc:
                raise ArtifactError(
                    "artifact_png_invalid", "PNG dimensions or header checksum invalid"
                )
        elif path.endswith(".pt"):
            if not content.startswith(b"PK\x03\x04"):
                raise ArtifactError(
                    "artifact_checkpoint_invalid", "Checkpoint is not a torch archive"
                )
        else:
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ArtifactError("artifact_text_invalid", "Text artifact is not UTF-8") from exc
