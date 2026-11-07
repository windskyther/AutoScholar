"""Bounded, JSON-only research quality checkpoints and model response contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ResearchDepth = Literal["quick", "standard", "deep"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Material(Strict):
    id: str
    identity: str
    kind: Literal["web", "paper", "document"]
    title: str
    url: str
    text: str
    topics: list[str] = Field(default_factory=list)
    published_at: str | None = None
    fulltext_url: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None
    page: int | None = None
    availability: Literal["summary_only", "extracted_text", "document_excerpt"] = "summary_only"
    truncated: bool = False
    read_attempted: bool = False


class Assessment(Strict):
    source_id: str = Field(pattern=r"^E[1-9][0-9]*$")
    verdict: Literal["keep", "background", "reject"]
    relevance: float = Field(ge=0, le=1)
    time_fit: Literal["within_scope", "background", "unknown"]
    reason: str = Field(min_length=1, max_length=1200)
    quote: str = Field(max_length=1200)


class Citation(Strict):
    source_id: str = Field(pattern=r"^E[1-9][0-9]*$")
    quote: str = Field(min_length=1, max_length=1200)


class Claim(Strict):
    text: str = Field(min_length=1, max_length=2400)
    citations: list[Citation] = Field(min_length=1, max_length=6)


class ReportSection(Strict):
    heading: str = Field(min_length=1, max_length=160)
    claims: list[Claim] = Field(min_length=1, max_length=12)


class QualityIssue(Strict):
    draft_quote: str = Field(min_length=1, max_length=1200)
    reason: str = Field(min_length=1, max_length=1600)
    action: Literal["revise", "search"]
    search_query: str | None = Field(max_length=400)
    source_type: Literal["web", "paper", "document"]


class QualityResponse(Strict):
    # Keep the same tool schema for every pass so stage changes need not change
    # the reusable request prefix. Empty stage-irrelevant arrays are required.
    assessments: list[Assessment] = Field(max_length=40)
    sections: list[ReportSection] = Field(max_length=12)
    issues: list[QualityIssue] = Field(max_length=16)
    decision: Literal["continue", "accept", "revise", "insufficient"]


class QualityState(Strict):
    schema_version: Literal[1] = 1
    phase: Literal[
        "collect", "screen", "read", "refine", "write", "check", "revise", "supplement", "done"
    ] = "collect"
    materials: list[Material] = Field(default_factory=list, max_length=40)
    bundle: str = ""
    bundle_sha256: str = ""
    material_versions: list[str] = Field(default_factory=list)
    initial: list[Assessment] = Field(default_factory=list)
    assessments: list[Assessment] = Field(default_factory=list)
    read_queue: list[str] = Field(default_factory=list)
    sections: list[ReportSection] = Field(default_factory=list)
    issues: list[QualityIssue] = Field(default_factory=list)
    rounds: int = Field(default=0, ge=0, le=2)
    reads: int = Field(default=0, ge=0)
    supplemental_queries: int = Field(default=0, ge=0, le=4)
    accepted: bool = False
    review_history: list[dict[str, object]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
