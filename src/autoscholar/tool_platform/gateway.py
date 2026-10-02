"""A bounded, non-retrying gateway shared by native and MCP tool backends."""

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from jsonschema import Draft202012Validator

from autoscholar.core.budget import BudgetExceeded, current_budget, current_parent
from autoscholar.core.journal import external_operation
from autoscholar.tool_platform.context import current_tool_scope

PROTOCOL_VERSION = "2026-07-28"
MAX_MESSAGE_BYTES = 1_048_576


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def argument_digest(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(arguments).encode()).hexdigest()


class ToolGatewayError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


@dataclass(frozen=True)
class ToolContract:
    name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolReply:
    payload: dict[str, Any]
    is_error: bool = False


class ToolConnection(Protocol):
    async def discover(self) -> dict[str, dict[str, Any]]: ...

    async def call(
        self, name: str, arguments: dict[str, Any], context: dict[str, Any]
    ) -> ToolReply: ...


class ToolBackend(Protocol):
    def connect(self) -> AbstractAsyncContextManager[ToolConnection]: ...


class NativeBackend:
    """Explicit native mode, not an implicit fallback after a remote call fails."""

    def __init__(
        self,
        contracts: list[ToolContract],
        handlers: Mapping[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]],
    ) -> None:
        self.contracts = contracts
        self.handlers = handlers

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[ToolConnection]:
        yield self

    async def discover(self) -> dict[str, dict[str, Any]]:
        return {contract.name: contract.input_schema for contract in self.contracts}

    async def call(
        self, name: str, arguments: dict[str, Any], context: dict[str, Any]
    ) -> ToolReply:
        return ToolReply(await self.handlers[name](arguments))


def validate(schema: dict[str, Any], value: Any, code: str) -> None:
    # Never resolve remote schema references (including file:// and HTTP URLs).
    encoded = canonical(schema)
    if len(encoded.encode()) > 65536 or '"$ref"' in encoded or '"$dynamicRef"' in encoded:
        raise ToolGatewayError("tool_contract_invalid")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
    except Exception as exc:
        raise ToolGatewayError(code) from exc


class ToolGateway:
    def __init__(
        self, backend: ToolBackend, contracts: list[ToolContract], *, timeout_seconds: float = 25
    ) -> None:
        self.backend = backend
        self.contracts = {contract.name: contract for contract in contracts}
        if len(self.contracts) != len(contracts):
            raise ValueError("Duplicate tool contract")
        self.timeout_seconds = timeout_seconds
        self._fingerprints: dict[str, str] = {}

    async def available(self) -> bool:
        try:
            async with (
                asyncio.timeout(min(3, self.timeout_seconds)),
                self.backend.connect() as conn,
            ):
                advertised = await conn.discover()
                return self.contracts.keys() <= advertised.keys()
        except Exception:
            return False

    async def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        contract = self.contracts.get(name)
        if contract is None:
            raise ToolGatewayError("tool_not_allowed")
        if len(canonical(arguments).encode()) > MAX_MESSAGE_BYTES:
            raise ToolGatewayError("tool_input_too_large")
        validate(contract.input_schema, arguments, "tool_arguments_invalid")
        budget = current_budget.get()
        timeout = self.timeout_seconds
        if budget is not None:
            budget.check()
            timeout = min(timeout, budget.limits.wall_seconds - (time.monotonic() - budget.started))
        operation_id = str(uuid4())
        deadline = time.time() + timeout
        dispatched = False
        try:
            async with asyncio.timeout(timeout), self.backend.connect() as connection:
                advertised = await connection.discover()
                schema = advertised.get(name)
                if schema is None:
                    raise ToolGatewayError("tool_unavailable")
                validate(schema, arguments, "tool_contract_mismatch")
                fingerprint = argument_digest(schema)
                previous = self._fingerprints.setdefault(name, fingerprint)
                if previous != fingerprint:
                    raise ToolGatewayError("tool_contract_changed")
                scope = current_tool_scope.get()
                context = {
                    "operation_id": operation_id,
                    "parent_task_id": current_parent.get(),
                    "tool": name,
                    "arguments_sha256": argument_digest(arguments),
                    "deadline": deadline,
                    "scope": scope.payload() if scope is not None else None,
                }
                # Runner owns budget consumption. Gateway owns the one durable transport
                # boundary; service processes never inherit the Core's ContextVars.
                async with external_operation("mcp:" + name, operation_id=operation_id):
                    if budget is not None:
                        budget.check()
                    dispatched = True
                    reply = await connection.call(name, arguments, context)
                    if reply.is_error:
                        raise ToolGatewayError("tool_remote_error", uncertain=True)
                    if len(canonical(reply.payload).encode()) > MAX_MESSAGE_BYTES:
                        raise ToolGatewayError("tool_output_too_large", uncertain=True)
                    validate(contract.output_schema, reply.payload, "tool_result_invalid")
                    if reply.payload.get("uncertain") is True:
                        raise ToolGatewayError("tool_result_uncertain", uncertain=True)
                return reply.payload
        except ToolGatewayError as exc:
            if dispatched:
                exc.uncertain = True
            raise
        except TimeoutError as exc:
            raise ToolGatewayError("tool_timeout", uncertain=dispatched) from exc
        except Exception as exc:
            # SDK transport task groups may wrap a caller exception during exit.
            cause: BaseException = exc
            while isinstance(cause, BaseExceptionGroup) and len(cause.exceptions) == 1:
                cause = cause.exceptions[0]
            if isinstance(cause, BudgetExceeded):
                raise cause from None
            if isinstance(cause, ToolGatewayError):
                cause.uncertain = cause.uncertain or dispatched
                raise cause from None
            # Do not expose headers, arguments, upstream exception messages or credentials.
            raise ToolGatewayError("tool_unavailable", uncertain=dispatched) from exc
