"""Bounded, immutable selections embedded in task checkpoints."""

import hashlib
import json
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from autoscholar.research.quality_models import Material


def content_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class BankSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: str
    material: Material
    document_sha256: str | None = None
    fetched_at: datetime | None = None
    fresh: bool = False


class BankClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    task_id: str
    objective: str
    text: str
    supports: list[dict[str, str]] = Field(max_length=6)
    dependencies: list[str] = Field(default_factory=list)


class EvidenceContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str | None = None
    claims: list[BankClaim] = Field(default_factory=list, max_length=12)
    sources: list[BankSource] = Field(default_factory=list, max_length=40)
    snapshot_hash: str = ""
    note: str | None = None


current_evidence: ContextVar[EvidenceContext | None] = ContextVar("project_evidence", default=None)
