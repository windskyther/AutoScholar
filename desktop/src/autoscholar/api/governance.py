"""Authenticated admission, bounded streaming input, deadlines and durable audit."""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable
from dataclasses import replace
from tempfile import SpooledTemporaryFile
from typing import Any, TypeVar
from uuid import UUID, uuid4

import structlog
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from autoscholar.core.admission import AdmissionStore
from autoscholar.core.audit import AuditEvent, AuditSink, current_security_audit
from autoscholar.core.config import Settings
from autoscholar.core.responses import UTF8JSONResponse

logger = structlog.get_logger(__name__)
T = TypeVar("T")


class GovernanceError(Exception):
    def __init__(self, status: int, code: str, retry_after: int | None = None) -> None:
        self.status, self.code, self.retry_after = status, code, retry_after


def category_for(scope: Scope) -> str:
    path, method = scope["path"].removeprefix("/workbench"), scope["method"]
    if method in {"GET", "HEAD", "OPTIONS"}:
        return "sse" if path.endswith("/stream") else "read"
    if path == "/agent/run":
        return "agent"  # Includes research, coding and training; deliberately conservative.
    if path == "/chat" or path.endswith("/rag/query"):
        return "llm"
    if path == "/agent/tasks":
        return "submit"
    if path.endswith("/documents") and method == "POST":
        return "upload"
    return "write"


def safe_id(value: object) -> str | None:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def action_for(path: str, category: str) -> str:
    if path.endswith("/decision"):
        return "approval_decision"
    for action in ("pause", "resume", "cancel", "reconcile", "retry", "reindex"):
        if path.endswith("/" + action):
            return action
    return category


class RequestGovernance:
    def __init__(self, admission: AdmissionStore, audit: AuditSink) -> None:
        self.admission, self.audit = admission, audit

    async def denial(self, request: Request, code: str, status: int) -> None:
        """Denials never dispatch work, even when the audit database is unavailable."""
        try:
            async with asyncio.timeout(request.app.state.settings.api_control_timeout_seconds):
                await self.audit.write(
                    AuditEvent(
                        id=str(uuid4()),
                        request_id=getattr(request.state, "request_id", "unknown"),
                        actor="unauthenticated",
                        category="boundary",
                        method=request.method,
                        phase="denied",
                        outcome=code,
                        status=status,
                    )
                )
        except Exception:
            logger.error("security_audit_unavailable", outcome=code)


class RequestGovernanceMiddleware:
    def __init__(self, app: ASGIApp, *, settings: Settings, governance: RequestGovernance) -> None:
        self.app, self.settings, self.governance = app, settings, governance

    async def control(self, operation: Awaitable[T]) -> T:
        async with asyncio.timeout(self.settings.api_control_timeout_seconds):
            return await operation

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (
            scope["path"] == "/health/live" and scope["method"] in {"GET", "HEAD"}
        ):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        category = category_for(scope)
        limit = self.settings.api_request_limits[category]
        lease = str(uuid4())
        actor = "local"
        key = request.headers.get("idempotency-key")
        event = AuditEvent(
            id=str(uuid4()),
            request_id=getattr(request.state, "request_id", "unknown"),
            actor=actor,
            category=category,
            method=request.method,
            phase="admitted",
            outcome="dispatch_pending",
            operation_id=hashlib.sha256(key.encode()).hexdigest() if key else lease,
            action=action_for(scope["path"], category),
        )
        started = False
        acquired = False
        keeper: asyncio.Task[None] | None = None
        body = SpooledTemporaryFile[bytes](max_size=1_048_576)
        status, response_bytes = 500, 0
        buffered: list[Message] = []
        critical = request.method not in {"GET", "HEAD", "OPTIONS"}
        last_output = time.monotonic()
        response_json = bytearray()
        replayed_empty = False

        async def governed_send(message: Message) -> None:
            nonlocal started, status, response_bytes, last_output
            last_output = time.monotonic()
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                response_bytes += len(chunk)
                cap = self.settings.api_response_max_bytes
                if not critical and category != "sse":
                    cap = max(
                        cap,
                        self.settings.document_max_bytes,
                        self.settings.workspace_max_artifact_file_bytes,
                    )
                if response_bytes > cap:
                    raise GovernanceError(502, "response_too_large")
                if critical:
                    response_json.extend(chunk)
            if critical:
                buffered.append(message)
            else:
                started = True
                async with asyncio.timeout(self.settings.api_send_timeout_seconds):
                    await send(message)

        async def keep_lease() -> None:
            while True:
                await asyncio.sleep(min(self.settings.api_lease_seconds / 3, 1))
                try:
                    async with asyncio.timeout(
                        min(
                            self.settings.api_control_timeout_seconds,
                            self.settings.api_lease_seconds / 6,
                        )
                    ):
                        valid = await self.governance.admission.renew(
                            category, lease, self.settings.api_lease_seconds
                        )
                except Exception:
                    raise GovernanceError(503, "admission_lease_lost") from None
                if not valid:
                    raise GovernanceError(503, "admission_lease_lost")
                if category == "sse" and (
                    time.monotonic() - last_output > self.settings.api_sse_idle_seconds
                ):
                    raise GovernanceError(504, "stream_idle_timeout")

        async def dispatch() -> None:
            async def audit_tool(operation_id: str, kind: str, starting: bool) -> None:
                try:
                    await self.control(
                        self.governance.audit.write(
                            replace(
                                event,
                                id=str(uuid4()),
                                operation_id=operation_id,
                                phase="tool_dispatch" if starting else "tool_completed",
                                outcome="pending" if starting else "known_result",
                                action=kind
                                if kind
                                in {
                                    "llm",
                                    "search:tavily",
                                    "search:semantic_scholar",
                                    "sandbox:run",
                                    "sandbox:train",
                                    "sandbox:check",
                                }
                                else "external_tool",
                            )
                        )
                    )
                except Exception:
                    raise GovernanceError(
                        503, "audit_unavailable" if starting else "audit_outcome_unknown"
                    ) from None

            audit_token = current_security_audit.set(audit_tool)
            try:
                async with asyncio.timeout(limit.deadline_seconds):
                    await self.app(scope, replay, governed_send)
            except TimeoutError:
                raise GovernanceError(504, "request_deadline_exceeded") from None
            finally:
                current_security_audit.reset(audit_token)

        async def replay() -> Message:
            nonlocal replayed_empty
            chunk = body.read(65_536)
            if chunk:
                return {"type": "http.request", "body": chunk, "more_body": body.tell() < body_size}
            if body_size == 0 and not replayed_empty:
                replayed_empty = True
                return {"type": "http.request", "body": b"", "more_body": False}
            # After consuming the body, preserve actual client disconnects for SSE.
            return await receive()

        dispatched = False
        try:
            try:
                decision = await self.control(
                    self.governance.admission.acquire(
                        category, lease, limit, self.settings.api_lease_seconds
                    )
                )
            except Exception:
                raise GovernanceError(503, "admission_unavailable") from None
            if not decision.accepted:
                raise GovernanceError(429, "request_limit_exceeded", decision.retry_after)
            acquired = True
            keeper = asyncio.create_task(keep_lease())
            try:
                await self.control(self.governance.audit.write(event))
            except Exception:
                raise GovernanceError(503, "audit_unavailable") from None
            body_size = await self.read_body(request, receive, body, category)
            body.seek(0)
            if keeper.done():
                await keeper
            dispatched = True
            task = asyncio.create_task(dispatch())
            try:
                done, _ = await asyncio.wait({task, keeper}, return_when=asyncio.FIRST_COMPLETED)
                if keeper in done:
                    await keeper
                await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            identifiers: dict[str, Any] = {}
            if critical:
                try:
                    payload = json.loads(response_json)
                    if isinstance(payload, dict):
                        identifiers = {
                            "task_id": safe_id(payload.get("task_id")),
                            "project_id": safe_id(payload.get("project_id")),
                        }
                        usage = payload.get("usage")
                        if isinstance(usage, dict):
                            identifiers["usage"] = {
                                name: value
                                for name, value in usage.items()
                                if name in {"input_tokens", "output_tokens", "total_tokens"}
                                and type(value) is int
                                and value >= 0
                            }
                except (ValueError, UnicodeError):
                    pass
            params = scope.get("path_params", {})
            identifiers["task_id"] = identifiers.get("task_id") or safe_id(params.get("task_id"))
            identifiers["project_id"] = identifiers.get("project_id") or safe_id(
                params.get("project_id")
            )
            try:
                await self.control(
                    self.governance.audit.write(
                        replace(
                            event,
                            id=str(uuid4()),
                            phase="completed",
                            outcome="http_response",
                            status=status,
                            **identifiers,
                        )
                    )
                )
            except Exception:
                raise GovernanceError(503, "audit_outcome_unknown") from None
            for message in buffered:
                started = True
                async with asyncio.timeout(self.settings.api_send_timeout_seconds):
                    await send(message)
        except GovernanceError as exc:
            try:
                await self.control(
                    self.governance.audit.write(
                        replace(
                            event,
                            id=str(uuid4()),
                            phase="interrupted" if dispatched else "denied",
                            outcome=exc.code,
                            status=exc.status,
                        )
                    )
                )
            except Exception:
                logger.error("security_audit_unavailable", outcome=exc.code)
            if not started:
                await UTF8JSONResponse(
                    status_code=exc.status,
                    content={
                        "error": {"code": exc.code, "message": exc.code},
                        "request_id": event.request_id,
                    },
                    headers={"Retry-After": str(exc.retry_after)} if exc.retry_after else None,
                )(scope, receive, send)
            else:
                # Headers may already be sent for a stream: close it, never send a second status.
                logger.warning("response_interrupted", outcome=exc.code)
                raise
        except asyncio.CancelledError:
            try:
                await asyncio.shield(
                    self.control(
                        self.governance.audit.write(
                            replace(
                                event,
                                id=str(uuid4()),
                                phase="interrupted",
                                outcome="request_cancelled",
                            )
                        )
                    )
                )
            except Exception:
                logger.error("security_audit_unavailable", outcome="request_cancelled")
            raise
        except Exception as exc:
            logger.error("request_dispatch_failed", exception_type=type(exc).__name__)
            try:
                await self.control(
                    self.governance.audit.write(
                        replace(
                            event,
                            id=str(uuid4()),
                            phase="interrupted",
                            outcome="internal_error",
                            status=500,
                        )
                    )
                )
            except Exception:
                logger.error("security_audit_unavailable", outcome="internal_error")
            if started:
                raise
            await UTF8JSONResponse(
                status_code=500,
                content={
                    "error": {"code": "internal_error", "message": "Internal server error"},
                    "request_id": event.request_id,
                },
            )(scope, receive, send)
        finally:
            body.close()
            if keeper is not None:
                keeper.cancel()
                await asyncio.gather(keeper, return_exceptions=True)
            if acquired:
                try:
                    await asyncio.shield(
                        self.control(self.governance.admission.release(category, lease))
                    )
                except Exception:
                    logger.error("admission_release_failed")

    async def read_body(
        self, request: Request, receive: Receive, target: SpooledTemporaryFile[bytes], category: str
    ) -> int:
        cap = self.settings.api_json_max_bytes
        if category == "upload":
            cap = self.settings.document_max_bytes + self.settings.api_multipart_overhead_bytes
        lengths = request.headers.getlist("content-length")
        if lengths:
            try:
                if len(lengths) != 1 or int(lengths[0]) < 0:
                    raise ValueError
                if int(lengths[0]) > cap:
                    raise GovernanceError(413, "request_body_too_large")
            except ValueError:
                raise GovernanceError(400, "invalid_content_length") from None
        count = 0
        try:
            async with asyncio.timeout(self.settings.api_body_timeout_seconds):
                while True:
                    async with asyncio.timeout(self.settings.api_body_idle_seconds):
                        message = await receive()
                    if message["type"] == "http.disconnect":
                        raise GovernanceError(400, "request_disconnected")
                    chunk = message.get("body", b"")
                    count += len(chunk)
                    if count > cap:
                        raise GovernanceError(413, "request_body_too_large")
                    target.write(chunk)
                    if not message.get("more_body", False):
                        if lengths and count != int(lengths[0]):
                            raise GovernanceError(400, "content_length_mismatch")
                        return count
        except TimeoutError:
            raise GovernanceError(408, "request_body_timeout") from None
