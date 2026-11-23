import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol


class DocumentStorageError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class StoredDocument:
    key: str
    sha256: str
    size_bytes: int


class DocumentStorage(Protocol):
    def save(self, document_id: str, source: BinaryIO, *, max_bytes: int) -> StoredDocument: ...

    def path_for(self, key: str) -> Path: ...

    def delete(self, key: str) -> None: ...


class LocalDocumentStorage:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def save(self, document_id: str, source: BinaryIO, *, max_bytes: int) -> StoredDocument:
        key = f"{document_id}.pdf"
        target = self.path_for(key)
        digest = hashlib.sha256()
        size = 0
        header = b""
        try:
            with target.open("xb") as output:
                while block := source.read(1024 * 1024):
                    if not header:
                        header = block[:5]
                    size += len(block)
                    if size > max_bytes:
                        raise DocumentStorageError(
                            "document_too_large", "PDF exceeds the configured size limit"
                        )
                    digest.update(block)
                    output.write(block)
            if header != b"%PDF-":
                raise DocumentStorageError("invalid_pdf", "Uploaded file is not a PDF")
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return StoredDocument(key=key, sha256=digest.hexdigest(), size_bytes=size)

    def path_for(self, key: str) -> Path:
        if Path(key).name != key:
            raise DocumentStorageError("invalid_storage_key", "Invalid document storage key")
        target = (self._root / key).resolve()
        if target.parent != self._root:
            raise DocumentStorageError("invalid_storage_key", "Invalid document storage key")
        return target

    def delete(self, key: str) -> None:
        self.path_for(key).unlink(missing_ok=True)
