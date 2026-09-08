from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

TaskStatus = Literal["running", "succeeded", "partial", "failed", "budget_exceeded"]
ToolCallStatus = Literal["succeeded", "failed"]
AgentMode = Literal["auto", "research", "compute", "knowledge"]
ResolvedAgentMode = Literal["research", "compute", "knowledge"]


@dataclass(frozen=True, slots=True)
class ToolTraceRecord:
    id: str
    task_id: str
    sequence: int
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    output: str
    status: ToolCallStatus
    duration_ms: float
    created_at: datetime
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    id: str
    task_id: str
    citation_key: str
    source_type: Literal["web", "paper", "document"]
    provider: str
    title: str
    url: str
    authors: tuple[str, ...]
    year: int | None
    external_id: str | None
    query: str
    topic: str
    claim: str
    excerpt: str
    relevance: float
    created_at: datetime
    document_id: str | None = None
    chunk_id: str | None = None
    page: int | None = None
    section: str | None = None


@dataclass(frozen=True, slots=True)
class CitationRecord:
    claim: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchWarningRecord:
    code: str
    message: str
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTaskRecord:
    id: str
    status: TaskStatus
    objective: str
    plan: list[str]
    answer: str | None
    metrics: dict[str, int]
    created_at: datetime
    updated_at: datetime
    error_code: str | None = None
    error_message: str | None = None
    mode: ResolvedAgentMode = "compute"
    citations: list[CitationRecord] = field(default_factory=list)
    warnings: list[ResearchWarningRecord] = field(default_factory=list)
    evidence: list[EvidenceRecord] = field(default_factory=list)
    tool_calls: list[ToolTraceRecord] = field(default_factory=list)
    project_id: str | None = None
