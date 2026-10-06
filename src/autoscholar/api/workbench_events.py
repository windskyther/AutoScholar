"""Browser-safe event projection and bounded, read-only SSE polling."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from anyio import CancelScope
from fastapi import Request

from autoscholar.api.workbench_models import WorkflowEventPage, WorkflowEventSummary
from autoscholar.orchestration.durable_models import WorkflowEventRow

if TYPE_CHECKING:
    from autoscholar.api.workbench_repository import WorkbenchRepository

MAX_CURSOR = 9007199254740991
TERMINAL = {"succeeded", "partial", "failed", "budget_exceeded", "cancelled"}
STATUSES = TERMINAL | {
    "running",
    "queued",
    "paused",
    "pause_requested",
    "cancel_requested",
    "awaiting_approval",
    "recovery_required",
}
KINDS = {
    "submitted",
    "claimed",
    "call_reconciled",
    "recovery_verified",
    "checkpoint_rejected",
    "approval_consumed",
    "unit_started",
    "budget_saved",
    "call_started",
    "call_finished",
    "checkpoint_saved",
    "pause",
    "resume",
    "cancel",
    "memory_retrieved",
    "memory_enabled_changed",
    "experience_recorded",
    "approval_requested",
    "approval_decided",
}
STAGES = {"planner", "executor", "reviewer", "replanner", "writer", "done"}
CALL_KINDS = {"llm", "search", "training", "sandbox", "tool", "repair", "embedding"}


def public_event(row: WorkflowEventRow) -> WorkflowEventSummary:
    kind = row.kind if row.kind in KINDS else "other"
    payload: dict[str, str | int | bool] = {}
    # Deliberately exclude raw inputs, prompts, reasons, hashes, operation IDs,
    # worker identities, memory contents and arbitrary/unknown payload fields.
    allowed: dict[str, set[str]] = {
        "checkpoint_saved": {"status", "stage", "sequence"},
        "unit_started": {"stage", "version"},
        "claimed": {"previous"},
        "pause": {"status"},
        "resume": {"status"},
        "cancel": {"status"},
        "call_started": {"kind_name"},
        "call_finished": {"kind_name"},
        "approval_decided": {"decision"},
        "memory_enabled_changed": {"enabled"},
        "memory_retrieved": {"project_version"},
    }
    enums = {
        "status": STATUSES,
        "previous": STATUSES,
        "stage": STAGES,
        "kind_name": CALL_KINDS,
        "decision": {"approve", "reject", "modify"},
    }
    for key in allowed.get(kind, set()):
        value = row.payload.get(key)
        if key == "kind_name" and isinstance(value, str) and value.startswith("mcp:"):
            payload[key] = "mcp"  # No internal tool/operation identity.
            continue
        if (
            (isinstance(value, str) and value in enums.get(key, set()))
            or (key == "enabled" and isinstance(value, bool))
            or (
                key in {"sequence", "version", "project_version"}
                and type(value) is int
                and 0 <= value <= MAX_CURSOR
            )
        ):
            payload[key] = value
    return WorkflowEventSummary(
        task_id=row.task_id,
        sequence=row.sequence,
        kind=kind,
        payload=payload,
        created_at=row.created_at,
    )


def frame(kind: str, payload: dict[str, object], sequence: int | None = None) -> str:
    prefix = f"id: {sequence}\n" if sequence is not None else ""
    return (
        prefix
        + f"event: {kind}\ndata: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    )


async def event_stream(
    repository: "WorkbenchRepository",
    request: Request,
    first: WorkflowEventPage,
    *,
    poll_seconds: float = 1,
    heartbeat_seconds: float = 10,
    duration_seconds: float = 60,
) -> AsyncIterator[str]:
    started = time.monotonic()
    heartbeat = started
    page = first
    cursor = page.next_cursor if not page.items else page.items[0].sequence - 1
    yield frame(
        "ready", {"task_id": first.task_id, "status": first.status, "durable": first.durable}
    )
    while not await request.is_disconnected():
        for item in page.items:
            if await request.is_disconnected():
                return
            yield frame("workflow", item.model_dump(mode="json"), item.sequence)
            cursor = item.sequence
        if not page.has_more and (page.status in TERMINAL or not page.durable):
            yield frame(
                "end",
                {
                    "task_id": page.task_id,
                    "status": page.status,
                    "reason": "terminal" if page.durable else "unsupported",
                },
            )
            return
        if time.monotonic() - started >= duration_seconds:
            yield frame("end", {"task_id": page.task_id, "status": page.status, "reason": "rotate"})
            return
        if not page.has_more:
            if time.monotonic() - heartbeat >= heartbeat_seconds:
                yield ": heartbeat\n\n"
                heartbeat = time.monotonic()
            await asyncio.sleep(poll_seconds)
        try:
            # Every query closes its session before yielding. Never retain a SQL
            # transaction/connection or lock for the lifetime of a browser stream.
            # ASGI disconnects use level cancellation. Protect just this bounded
            # read and its session finalization, not the stream or polling wait.
            # Otherwise cancellation can interrupt rollback/connection check-in.
            with CancelScope(shield=True):
                page = await repository.events(page.task_id, after=cursor, limit=100)
        except Exception:
            yield frame("error", {"code": "workbench_stream_unavailable"})
            return
