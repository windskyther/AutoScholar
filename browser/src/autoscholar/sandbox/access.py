"""The Docker control plane accepts a separate credential from trusted service callers."""

import asyncio
import hmac
import time
from pathlib import Path

import structlog
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from autoscholar.coding.sandbox import SandboxAction
from autoscholar.core.responses import UTF8JSONResponse
from autoscholar.core.service_credentials import sandbox_credential

logger = structlog.get_logger(__name__)


class ManagerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    sandbox_manager_token: SecretStr | None = None
    sandbox_manager_token_file: Path | None = None
    sandbox_manager_max_concurrent: int = Field(default=2, ge=1, le=16)
    sandbox_manager_max_body_bytes: int = Field(default=16_777_216, ge=1024, le=67_108_864)
    sandbox_allowed_actions: list[SandboxAction] = Field(
        default=["static_check", "run_python", "run_pytest", "run_shell"],
        min_length=1,
    )

    @field_validator("sandbox_manager_token_file", mode="before")
    @classmethod
    def empty_secret_file(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def credential(self) -> "ManagerSettings":
        self.sandbox_manager_token = sandbox_credential(
            self.sandbox_manager_token, self.sandbox_manager_token_file
        )
        return self


class ManagerBoundary:
    def __init__(self, app: ASGIApp, *, settings: ManagerSettings) -> None:
        self.app, self.settings = app, settings
        self.active = 0  # The deployment runs one manager process; never multiple workers.

    async def reject(
        self, scope: Scope, receive: Receive, send: Send, status: int, code: str
    ) -> None:
        logger.warning("sandbox_request_denied", outcome=code)
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
        if status == 429:
            headers["Retry-After"] = "1"
        async with asyncio.timeout(5):
            await UTF8JSONResponse(
                {"error": {"code": code, "message": code}},
                status_code=status,
                headers={**headers, "Cache-Control": "private, no-store"},
            )(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (
            scope["path"] == "/health/live" and scope["method"] in {"GET", "HEAD"}
        ):
            await self.app(scope, receive, send)
            return
        token = self.settings.sandbox_manager_token
        if token is None:
            await self.reject(scope, receive, send, 503, "sandbox_auth_not_configured")
            return
        request = Request(scope)
        headers = request.headers.getlist("authorization")
        expected = ("Bearer " + token.get_secret_value()).encode("ascii")
        if len(headers) != 1 or not hmac.compare_digest(headers[0].encode("utf-8"), expected):
            await self.reject(scope, receive, send, 401, "sandbox_unauthorized")
            return
        if request.headers.getlist("origin"):
            await self.reject(scope, receive, send, 403, "sandbox_browser_forbidden")
            return
        if self.active >= self.settings.sandbox_manager_max_concurrent:
            await self.reject(scope, receive, send, 429, "sandbox_busy")
            return
        self.active += 1
        try:
            body = bytearray()
            lengths = request.headers.getlist("content-length")
            if lengths and (
                len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
            ):
                await self.reject(scope, receive, send, 400, "sandbox_length_invalid")
                return
            cap = self.settings.sandbox_manager_max_body_bytes
            if lengths and int(lengths[0]) > cap:
                await self.reject(scope, receive, send, 413, "sandbox_body_too_large")
                return
            try:
                async with asyncio.timeout(60):
                    while True:
                        async with asyncio.timeout(15):
                            message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > cap:
                            await self.reject(scope, receive, send, 413, "sandbox_body_too_large")
                            return
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await self.reject(scope, receive, send, 408, "sandbox_body_timeout")
                return
            if lengths and len(body) != int(lengths[0]):
                await self.reject(scope, receive, send, 400, "sandbox_length_invalid")
                return
            sent = False

            async def replay() -> Message:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            started = time.monotonic()
            try:
                await self.app(scope, replay, send)
            finally:
                logger.info(
                    "sandbox_request_finished", duration_ms=int((time.monotonic() - started) * 1000)
                )
        finally:
            self.active -= 1
