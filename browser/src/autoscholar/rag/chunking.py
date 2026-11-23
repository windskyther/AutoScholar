import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from autoscholar.rag.models import DocumentChunkRecord
from autoscholar.rag.parser import ParsedDocument

_TOKEN_PATTERN = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]", re.UNICODE)
_NUMBERED_HEADING = re.compile(r"^(?:\d+(?:\.\d+)*[.)]?|[IVX]+[.)])\s+\S+", re.IGNORECASE)
_KNOWN_HEADINGS = {
    "abstract",
    "introduction",
    "background",
    "related work",
    "method",
    "methods",
    "methodology",
    "experiments",
    "results",
    "discussion",
    "limitations",
    "conclusion",
    "conclusions",
    "references",
    "摘要",
    "引言",
    "背景",
    "相关工作",
    "方法",
    "实验",
    "结果",
    "讨论",
    "局限性",
    "结论",
    "参考文献",
}


@dataclass(frozen=True, slots=True)
class _Section:
    name: str | None
    content: str


class StructureAwareChunker:
    def __init__(self, *, max_tokens: int, overlap_tokens: int) -> None:
        if overlap_tokens >= max_tokens:
            raise ValueError("Chunk overlap must be smaller than chunk size")
        self._max_tokens = max_tokens
        self._overlap_tokens = overlap_tokens

    def chunk(
        self, document: ParsedDocument, *, document_id: str, project_id: str
    ) -> list[DocumentChunkRecord]:
        chunks: list[DocumentChunkRecord] = []
        ordinal = 0
        for page in document.pages:
            for section in self._sections(page.text):
                spans = list(_TOKEN_PATTERN.finditer(section.content))
                if not spans:
                    continue
                step = self._max_tokens - self._overlap_tokens
                for start_index in range(0, len(spans), step):
                    window = spans[start_index : start_index + self._max_tokens]
                    if not window:
                        continue
                    content = section.content[window[0].start() : window[-1].end()].strip()
                    if not content:
                        continue
                    chunk_id = str(
                        uuid5(
                            NAMESPACE_URL,
                            f"autoscholar:{document_id}:{page.page}:{ordinal}:{content}",
                        )
                    )
                    chunks.append(
                        DocumentChunkRecord(
                            id=chunk_id,
                            document_id=document_id,
                            project_id=project_id,
                            ordinal=ordinal,
                            page=page.page,
                            section=section.name,
                            content=content,
                            token_count=len(window),
                            created_at=datetime.now(UTC),
                        )
                    )
                    ordinal += 1
                    if start_index + self._max_tokens >= len(spans):
                        break
        return chunks

    @classmethod
    def _sections(cls, text: str) -> list[_Section]:
        sections: list[_Section] = []
        current_name: str | None = None
        body: list[str] = []
        for line in text.splitlines():
            if cls._is_heading(line):
                if body:
                    sections.append(_Section(current_name, "\n".join(body).strip()))
                current_name = line[:500]
                body = []
            else:
                body.append(line)
        if body:
            sections.append(_Section(current_name, "\n".join(body).strip()))
        if not sections and text.strip():
            sections.append(_Section(None, text.strip()))
        return [section for section in sections if section.content]

    @staticmethod
    def _is_heading(line: str) -> bool:
        value = line.strip()
        if not value or len(value) > 120:
            return False
        normalized = value.rstrip(":.\uFF1A").casefold()
        if normalized in _KNOWN_HEADINGS or _NUMBERED_HEADING.match(value):
            return True
        words = value.split()
        return 1 <= len(words) <= 10 and value.isupper() and any(char.isalpha() for char in value)
