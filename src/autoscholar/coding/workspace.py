import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath


class WorkspaceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class WorkspaceFile:
    path: str
    size_bytes: int
    sha256: str
    updated_at: datetime


class WorkspaceManager:
    """Owns task-scoped text workspaces and prevents paths escaping their task root."""

    directories = ("source", "data", "outputs", "checkpoints", "reports", "logs")
    _task_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

    def __init__(
        self,
        root: Path,
        *,
        max_files: int = 100,
        max_file_bytes: int = 1_048_576,
        max_source_bytes: int = 10_485_760,
    ) -> None:
        self.root = root
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_source_bytes = max_source_bytes

    def initialize(self, task_id: str) -> Path:
        task_root = self._task_root(task_id)
        for directory in self.directories:
            (task_root / directory).mkdir(parents=True, exist_ok=True)
        return task_root

    def list_files(self, task_id: str) -> list[WorkspaceFile]:
        task_root = self._existing_task_root(task_id)
        files: list[WorkspaceFile] = []
        for path in sorted(task_root.rglob("*")):
            if path.is_symlink():
                raise WorkspaceError(
                    "workspace_path_invalid", "Symbolic links are not allowed in workspaces"
                )
            if not path.is_file():
                continue
            stat = path.stat()
            files.append(
                WorkspaceFile(
                    path=path.relative_to(task_root).as_posix(),
                    size_bytes=stat.st_size,
                    sha256=self._digest(path),
                    updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )
            )
        return files

    def list_source_files(self, task_id: str) -> list[WorkspaceFile]:
        return [item for item in self.list_files(task_id) if item.path.startswith("source/")]

    def read_text(self, task_id: str, relative_path: str, *, area: str = "source") -> str:
        path = self._resolve_file(task_id, relative_path, area=area, must_exist=True)
        if path.stat().st_size > self.max_file_bytes:
            raise WorkspaceError("workspace_file_too_large", "The requested file is too large")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(
                "workspace_file_not_text", "Only UTF-8 text files can be read"
            ) from exc

    def write_text(
        self,
        task_id: str,
        relative_path: str,
        content: str,
        *,
        area: str = "source",
        overwrite: bool = False,
    ) -> WorkspaceFile:
        if not isinstance(content, str):
            raise WorkspaceError("workspace_content_invalid", "File content must be text")
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise WorkspaceError("workspace_file_too_large", "File exceeds the size limit")
        path = self._resolve_file(task_id, relative_path, area=area)
        if path.exists() and not overwrite:
            raise WorkspaceError("workspace_file_exists", "File already exists")
        self._check_source_quota(task_id, path, len(encoded), area=area)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_no_symlinks(self._task_root(task_id), path)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".autoscholar-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return self._file_record(self._task_root(task_id), path)

    def edit_text(
        self, task_id: str, relative_path: str, old_text: str, new_text: str
    ) -> WorkspaceFile:
        if not old_text:
            raise WorkspaceError("workspace_edit_invalid", "old_text must not be empty")
        current = self.read_text(task_id, relative_path)
        if current.count(old_text) != 1:
            raise WorkspaceError(
                "workspace_edit_conflict",
                "old_text must match exactly once; read the file and retry",
            )
        return self.write_text(
            task_id,
            relative_path,
            current.replace(old_text, new_text, 1),
            overwrite=True,
        )

    def delete_file(self, task_id: str, relative_path: str) -> None:
        path = self._resolve_file(task_id, relative_path, must_exist=True)
        path.unlink()

    def search(self, task_id: str, query: str, *, limit: int = 50) -> list[dict[str, object]]:
        if not query or len(query) > 500:
            raise WorkspaceError(
                "workspace_search_invalid", "Search query must contain 1 to 500 characters"
            )
        matches: list[dict[str, object]] = []
        for item in self.list_source_files(task_id):
            try:
                content = self.read_text(task_id, item.path.removeprefix("source/"))
            except WorkspaceError as exc:
                if exc.code == "workspace_file_not_text":
                    continue
                raise
            for line_number, line in enumerate(content.splitlines(), start=1):
                if query in line:
                    matches.append(
                        {"path": item.path, "line": line_number, "text": line[:500]}
                    )
                    if len(matches) >= limit:
                        return matches
        return matches

    def source_snapshot(self, task_id: str) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for item in self.list_source_files(task_id):
            relative = item.path.removeprefix("source/")
            snapshot[relative] = self.read_text(task_id, relative)
        return snapshot

    def _task_root(self, task_id: str) -> Path:
        if not self._task_pattern.fullmatch(task_id):
            raise WorkspaceError("workspace_path_invalid", "Invalid task identifier")
        root = self.root.resolve()
        candidate = (root / task_id).resolve()
        if not candidate.is_relative_to(root):
            raise WorkspaceError("workspace_path_invalid", "Task path escapes workspace root")
        return candidate

    def _existing_task_root(self, task_id: str) -> Path:
        path = self._task_root(task_id)
        if not path.is_dir():
            raise WorkspaceError("workspace_not_found", "Task workspace was not found")
        return path

    def _resolve_file(
        self,
        task_id: str,
        relative_path: str,
        *,
        area: str = "source",
        must_exist: bool = False,
    ) -> Path:
        if area not in self.directories:
            raise WorkspaceError("workspace_path_invalid", "Unknown workspace area")
        normalized = relative_path.replace("\\", "/")
        pure = PurePosixPath(normalized)
        raw_parts = normalized.split("/")
        if (
            not normalized
            or pure.is_absolute()
            or re.match(r"^[A-Za-z]:", normalized)
            or any(part in {"", ".", ".."} for part in raw_parts)
        ):
            raise WorkspaceError("workspace_path_invalid", "A safe relative file path is required")
        task_root = self._existing_task_root(task_id)
        area_root = (task_root / area).resolve()
        candidate = (area_root / Path(*pure.parts)).resolve(strict=False)
        if not candidate.is_relative_to(area_root):
            raise WorkspaceError("workspace_path_invalid", "File path escapes its workspace area")
        self._ensure_no_symlinks(task_root, candidate)
        if must_exist and not candidate.is_file():
            raise WorkspaceError("workspace_file_not_found", "Workspace file was not found")
        if candidate.exists() and not candidate.is_file():
            raise WorkspaceError("workspace_path_invalid", "Path does not identify a file")
        return candidate

    def _check_source_quota(self, task_id: str, target: Path, new_size: int, *, area: str) -> None:
        if area != "source":
            return
        files = self.list_source_files(task_id)
        existing_size = target.stat().st_size if target.is_file() else 0
        if not target.exists() and len(files) >= self.max_files:
            raise WorkspaceError("workspace_file_limit", "Workspace file-count limit reached")
        total = sum(item.size_bytes for item in files) - existing_size + new_size
        if total > self.max_source_bytes:
            raise WorkspaceError("workspace_size_limit", "Workspace source-size limit reached")

    @staticmethod
    def _ensure_no_symlinks(task_root: Path, target: Path) -> None:
        current = target
        while current != task_root:
            if current.exists() and current.is_symlink():
                raise WorkspaceError(
                    "workspace_path_invalid", "Symbolic links are not allowed in workspaces"
                )
            current = current.parent
            if not current.is_relative_to(task_root):
                raise WorkspaceError("workspace_path_invalid", "Path escapes task workspace")

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(65_536), b""):
                digest.update(block)
        return digest.hexdigest()

    @classmethod
    def _file_record(cls, task_root: Path, path: Path) -> WorkspaceFile:
        stat = path.stat()
        return WorkspaceFile(
            path=path.relative_to(task_root).as_posix(),
            size_bytes=stat.st_size,
            sha256=cls._digest(path),
            updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )
