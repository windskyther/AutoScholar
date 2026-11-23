import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


class DocumentProcessingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ParsedPage:
    page: int
    text: str


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    pages: tuple[ParsedPage, ...]
    page_count: int
    metadata_title: str | None = None


class PDFParser:
    def parse(self, path: Path, *, max_pages: int) -> ParsedDocument:
        try:
            reader = PdfReader(path)
        except Exception as exc:
            raise DocumentProcessingError("invalid_pdf", "PDF could not be parsed") from exc
        if reader.is_encrypted:
            raise DocumentProcessingError(
                "encrypted_pdf", "Encrypted PDF files are not supported"
            )
        if len(reader.pages) > max_pages:
            raise DocumentProcessingError(
                "document_too_many_pages", "PDF exceeds the configured page limit"
            )
        pages: list[ParsedPage] = []
        try:
            for number, page in enumerate(reader.pages, start=1):
                extracted = (
                    page.extract_text(
                        extraction_mode="layout",
                        layout_mode_space_vertically=False,
                    )
                    if page.get_contents() is not None
                    else ""
                )
                text = self._normalize(extracted or "")
                if text:
                    pages.append(ParsedPage(page=number, text=text))
        except Exception as exc:
            raise DocumentProcessingError(
                "pdf_text_extraction_failed", "PDF text extraction failed"
            ) from exc
        if sum(len(page.text) for page in pages) < 200:
            raise DocumentProcessingError(
                "pdf_text_not_extractable",
                "PDF does not contain enough extractable text; scanned PDFs require OCR",
            )
        metadata_title = None
        if reader.metadata and reader.metadata.title:
            metadata_title = self._normalize(str(reader.metadata.title))[:500] or None
        return ParsedDocument(
            pages=tuple(pages),
            page_count=len(reader.pages),
            metadata_title=metadata_title,
        )

    @staticmethod
    def _normalize(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text).replace("\x00", "")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.splitlines()]
        output: list[str] = []
        blank = False
        for line in lines:
            if not line:
                if output and not blank:
                    output.append("")
                blank = True
                continue
            output.append(line)
            blank = False
        return "\n".join(output).strip()
