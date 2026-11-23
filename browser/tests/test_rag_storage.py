from io import BytesIO

import pytest

from autoscholar.rag.storage import DocumentStorageError, LocalDocumentStorage


def test_local_storage_validates_hash_size_and_path(tmp_path: object) -> None:
    from pathlib import Path

    storage = LocalDocumentStorage(Path(str(tmp_path)))
    stored = storage.save("doc-1", BytesIO(b"%PDF-1.4\nbody"), max_bytes=100)

    assert stored.key == "doc-1.pdf"
    assert stored.size_bytes == 13
    assert len(stored.sha256) == 64
    assert storage.path_for(stored.key).read_bytes() == b"%PDF-1.4\nbody"

    with pytest.raises(DocumentStorageError, match="Invalid document storage key"):
        storage.path_for("../secret.pdf")


def test_local_storage_rejects_invalid_or_oversized_files(tmp_path: object) -> None:
    from pathlib import Path

    storage = LocalDocumentStorage(Path(str(tmp_path)))

    with pytest.raises(DocumentStorageError) as invalid:
        storage.save("invalid", BytesIO(b"not a pdf"), max_bytes=100)
    assert invalid.value.code == "invalid_pdf"
    assert not storage.path_for("invalid.pdf").exists()

    with pytest.raises(DocumentStorageError) as oversized:
        storage.save("large", BytesIO(b"%PDF-" + b"x" * 20), max_bytes=10)
    assert oversized.value.code == "document_too_large"
    assert not storage.path_for("large.pdf").exists()
