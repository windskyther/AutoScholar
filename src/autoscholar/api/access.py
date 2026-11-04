"""Request boundaries run before routing, body parsing or business dispatch."""

from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from autoscholar.api.experiment_auth import require_experiment_token
from autoscholar.core.config import Settings
from autoscholar.core.errors import AppError
from autoscholar.core.responses import UTF8JSONResponse


def _origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(char.isspace() or char == "\\" for char in value)
    ):
        raise ValueError("Invalid origin")
    return (
        parsed.scheme,
        parsed.hostname.lower(),
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


async def _reject(request: Request, exc: AppError, receive: Receive, send: Send) -> None:
    await request.app.state.governance.denial(request, exc.code, exc.status_code)
    await UTF8JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {"code": exc.code, "message": exc.message},
            "request_id": getattr(request.state, "request_id", "unknown"),
        },
        headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
    )(request.scope, receive, send)


class APIBoundaryMiddleware:
    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app, self.settings = app, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            hosts = request.headers.getlist("host")
            if len(hosts) != 1:
                raise ValueError
            scheme, hostname, port = _origin(f"{scope['scheme']}://{hosts[0]}")
            if (
                "*" not in self.settings.api_allowed_hosts
                and hostname not in self.settings.api_allowed_hosts
            ):
                raise ValueError
        except ValueError:
            await _reject(
                request,
                AppError(status_code=400, code="invalid_host", message="Invalid Host header"),
                receive,
                send,
            )
            return
        origins = request.headers.getlist("origin")
        if origins:
            try:
                if len(origins) != 1:
                    raise ValueError
                origin = _origin(origins[0])
                allowed = {_origin(value) for value in self.settings.api_allowed_origins}
                if origin != (scheme, hostname, port) and origin not in allowed:
                    raise ValueError
            except ValueError:
                await _reject(
                    request,
                    AppError(
                        status_code=403, code="origin_not_allowed", message="Origin is not allowed"
                    ),
                    receive,
                    send,
                )
                return
        # Forwarded/X-Forwarded-* never replace Host or Origin at this boundary.
        await self.app(scope, receive, send)


class APIAuthenticationMiddleware:
    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app, self.settings = app, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        public = scope["path"] == "/health/live" and scope["method"] in {"GET", "HEAD"}
        if public:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            if len(request.headers.getlist("authorization")) != 1:
                # Preserve the existing unconfigured-service diagnostic.
                require_experiment_token(request)
                raise AppError(
                    status_code=401,
                    code="experiment_auth_required",
                    message="A single Bearer token is required",
                )
            require_experiment_token(request)
        except AppError as exc:
            await _reject(request, exc, receive, send)
            return
        await self.app(scope, receive, send)
