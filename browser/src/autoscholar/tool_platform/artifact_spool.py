"""Immutable, bounded artifact transport on an internal shared volume (not MCP JSON)."""

import base64
import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from autoscholar.coding.sandbox import SandboxArtifact, SandboxRunResult
from autoscholar.coding.workspace import WorkspaceManager


class ArtifactSpool:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def path(self, operation_id: str, artifact_id: str) -> Path:
        operation_id, artifact_id = str(UUID(operation_id)), str(UUID(artifact_id))
        path = self.root / operation_id / artifact_id
        # Check every ancestor, including a volume root alias/junction.
        for part in (path, *path.parents):
            if part.is_symlink() or part.is_junction():
                raise ValueError("artifact_spool_path_invalid")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("artifact_spool_path_invalid")
        return path

    def save(
        self, operation_id: str, result: SandboxRunResult, expected: list[str]
    ) -> dict[str, Any]:
        if len(result.artifacts) > 20 or len({a.path for a in result.artifacts}) != len(
            result.artifacts
        ):
            raise ValueError("artifact_manifest_invalid")
        manifest = result.model_dump(exclude={"artifacts"})
        # UTF-8 response bytes, not just character counts, stay below the wire envelope.
        for name in ("stdout", "stderr"):
            text = manifest[name].encode("utf-8")
            # The SDK duplicates dict results in structured JSON and text. Keep
            # even worst-case escaped control characters below the 1 MiB wire limit.
            manifest[name] = text[:24576].decode("utf-8", errors="ignore")
            manifest["truncated"] = manifest["truncated"] or len(text) > 24576
        manifest["artifacts"] = []
        total = 0
        for artifact in result.artifacts:
            if artifact.path not in expected:
                raise ValueError("artifact_manifest_invalid")
            if (
                artifact.size_bytes < 0
                or artifact.size_bytes > 16777216
                or len(artifact.data_base64) > 22369624
            ):
                raise ValueError("artifact_spool_quota")
            data = base64.b64decode(artifact.data_base64, validate=True)
            total += len(data)
            if len(data) > 16777216 or total > 67108864:
                raise ValueError("artifact_spool_quota")
            if (
                len(data) != artifact.size_bytes
                or hashlib.sha256(data).hexdigest() != artifact.sha256
            ):
                raise ValueError("artifact_integrity_failed")
            artifact_id = str(uuid4())
            target = self.path(operation_id, artifact_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            WorkspaceManager._write_atomic(target, data)
            manifest["artifacts"].append(
                {
                    "id": artifact_id,
                    "path": artifact.path,
                    "size_bytes": len(data),
                    "sha256": artifact.sha256,
                }
            )
        return manifest

    def load(
        self, operation_id: str, manifest: dict[str, Any], expected: list[str]
    ) -> SandboxRunResult:
        artifacts: list[SandboxArtifact] = []
        total = 0
        for item in manifest["artifacts"]:
            if item["path"] not in expected or item["path"] in {a.path for a in artifacts}:
                raise ValueError("artifact_manifest_invalid")
            target = self.path(operation_id, item["id"])
            size = target.stat().st_size
            total += size
            if size > 16777216 or total > 67108864 or size != item["size_bytes"]:
                raise ValueError("artifact_integrity_failed")
            data = target.read_bytes()
            if len(data) != size or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("artifact_integrity_failed")
            artifacts.append(
                SandboxArtifact(
                    path=item["path"],
                    size_bytes=size,
                    sha256=item["sha256"],
                    data_base64=base64.b64encode(data).decode("ascii"),
                )
            )
        return SandboxRunResult.model_validate({**manifest, "artifacts": artifacts})

    def metrics(self, operation_id: str, manifest: dict[str, Any]) -> dict[str, Any] | None:
        from autoscholar.experiment.models import RawExperimentMetrics

        item = next(
            (a for a in manifest["artifacts"] if a["path"] == "outputs/raw_metrics.json"), None
        )
        if item is None:
            return None
        path = self.path(operation_id, item["id"])
        if path.stat().st_size != item["size_bytes"] or item["size_bytes"] > 65536:
            raise ValueError("metric_artifact_invalid")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("metric_artifact_invalid")
        return RawExperimentMetrics.model_validate_json(data).model_dump()
