"""Explicit offline HTTP composition: real limits, deterministic stores, no services."""

import threading
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from autoscholar.api.governance import RequestGovernance
from autoscholar.core.admission import Admission, RequestLimit
from autoscholar.core.audit import AuditEvent
from autoscholar.core.config import Settings
from autoscholar.main import create_app


class MemoryAdmission:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.rates: dict[str, dict[str, float]] = {}
        self.leases: dict[str, dict[str, float]] = {}

    async def acquire(
        self, category: str, lease: str, limit: RequestLimit, ttl_seconds: float
    ) -> Admission:
        with self.lock:
            now = self.clock()
            rates = self.rates.setdefault(category, {})
            leases = self.leases.setdefault(category, {})
            for key, timestamp in list(rates.items()):
                if timestamp <= now - 60:
                    del rates[key]
            for key, timestamp in list(leases.items()):
                if timestamp <= now:
                    del leases[key]
            if len(rates) >= limit.per_minute or len(leases) >= limit.concurrent:
                return Admission(False, 1)
            rates[lease], leases[lease] = now, now + ttl_seconds
            return Admission(True)

    async def renew(self, category: str, lease: str, ttl_seconds: float) -> bool:
        with self.lock:
            leases = self.leases.setdefault(category, {})
            if leases.get(lease, 0) <= self.clock():
                return False
            leases[lease] = self.clock() + ttl_seconds
            return True

    async def release(self, category: str, lease: str) -> None:
        with self.lock:
            self.leases.setdefault(category, {}).pop(lease, None)


class MemoryAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def offline_governance() -> RequestGovernance:
    return RequestGovernance(MemoryAdmission(), MemoryAudit())


def offline_app(settings: Settings | None = None, **kwargs: Any) -> FastAPI:
    return create_app(settings, governance=offline_governance(), **kwargs)
