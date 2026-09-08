from pathlib import Path

import pytest
from pypdf import PdfWriter

from autoscholar.rag.chunking import StructureAwareChunker
from autoscholar.rag.parser import (
    DocumentProcessingError,
    ParsedDocument,
    ParsedPage,
    PDFParser,
)


def test_chunker_preserves_page_section_limits_and_stable_ids() -> None:
    document = ParsedDocument(
        pages=(
            ParsedPage(
                page=1,
                text="METHOD\n" + " ".join(f"token{index}" for index in range(25)),
            ),
            ParsedPage(page=2, text="RESULTS\naccuracy improves substantially"),
        ),
        page_count=2,
    )
    chunker = StructureAwareChunker(max_tokens=10, overlap_tokens=2)

    first = chunker.chunk(document, document_id="doc-1", project_id="project-1")
    second = chunker.chunk(document, document_id="doc-1", project_id="project-1")

    assert first
    assert [item.id for item in first] == [item.id for item in second]
    assert all(item.token_count <= 10 for item in first)
    assert {item.page for item in first} == {1, 2}
    assert first[0].section == "METHOD"
    assert first[-1].section == "RESULTS"
    assert "token8" in first[0].content
    assert "token8" in first[1].content


def test_pdf_parser_rejects_documents_without_extractable_text(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with path.open("wb") as output:
        writer.write(output)

    with pytest.raises(DocumentProcessingError) as exc_info:
        PDFParser().parse(path, max_pages=10)

    assert exc_info.value.code == "pdf_text_not_extractable"


def test_pdf_parser_rejects_encrypted_documents(tmp_path: Path) -> None:
    path = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    with path.open("wb") as output:
        writer.write(output)

    with pytest.raises(DocumentProcessingError) as exc_info:
        PDFParser().parse(path, max_pages=10)

    assert exc_info.value.code == "encrypted_pdf"
