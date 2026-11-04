"""Shared admission contract. A lease never authorizes retrying a business operation."""

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field


class RequestLimit(BaseModel):
    per_minute: int = Field(ge=1, le=100_000)
    concurrent: int = Field(ge=1, le=1_000)
    deadline_seconds: float = Field(gt=0, le=7_200)


def default_request_limits() -> dict[str, RequestLimit]:
    return {
        name: RequestLimit(per_minute=rate, concurrent=concurrent, deadline_seconds=deadline)
        for name, rate, concurrent, deadline in (
            ("read", 120, 16, 30),
            ("write", 30, 4, 30),
            ("llm", 10, 2, 90),
            ("agent", 6, 1, 1800),
            ("upload", 10, 2, 180),
            ("submit", 20, 4, 30),
            ("sse", 30, 4, 300),
        )
    }


@dataclass(frozen=True)
class Admission:
    accepted: bool
    retry_after: int = 1


class AdmissionStore(Protocol):
    async def acquire(
        self, category: str, lease: str, limit: RequestLimit, ttl_seconds: float
    ) -> Admission: ...

    async def renew(self, category: str, lease: str, ttl_seconds: float) -> bool: ...

    async def release(self, category: str, lease: str) -> None: ...


class UnavailableAdmissionStore:
    async def acquire(
        self, category: str, lease: str, limit: RequestLimit, ttl_seconds: float
    ) -> Admission:
        raise RuntimeError("Admission storage unavailable")

    async def renew(self, category: str, lease: str, ttl_seconds: float) -> bool:
        return False

    async def release(self, category: str, lease: str) -> None:
        pass
