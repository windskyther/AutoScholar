"""Optional durable accounting around external operations, scoped to one worker task."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from uuid import uuid4

JournalHook = Callable[[str, str, bool], Awaitable[None]]
current_journal: ContextVar[JournalHook | None] = ContextVar("workflow_journal", default=None)


@asynccontextmanager
async def external_operation(kind: str) -> AsyncIterator[None]:
    hook = current_journal.get()
    operation_id = str(uuid4())
    if hook is not None:
        await hook(operation_id, kind, True)
    yield
    # An interrupted/failed request stays pending: its result cannot be assumed absent.
    if hook is not None:
        await hook(operation_id, kind, False)
