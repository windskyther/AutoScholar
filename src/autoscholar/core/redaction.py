"""Last-mile sanitization for application, SDK and HTTP-client logging."""

import re
from collections.abc import Mapping, MutableMapping
from typing import Any
from urllib.parse import quote

from pydantic import SecretStr
from sqlalchemy.engine import make_url

from autoscholar.core.config import Settings

_secrets: tuple[str, ...] = ()
_sensitive = re.compile(
    r"authorization|cookie|token|password|secret|api.?key|dsn|body|prompt|document|content", re.I
)
_url = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+")
_bearer = re.compile(r"\bBearer\s+[^\s,;\"']+", re.I)


def configure_redaction(settings: Settings) -> None:
    global _secrets
    values = [
        value.get_secret_value()
        for name in Settings.model_fields
        if isinstance(value := getattr(settings, name), SecretStr)
    ]
    for field in ("database_url", "redis_url"):
        try:
            password = make_url(getattr(settings, field)).password
            if password:
                values.append(password)
        except Exception:
            pass
    _secrets = tuple(
        sorted(
            set(_secrets)
            | {item for value in values if value for item in (value, quote(value, safe=""))},
            key=len,
            reverse=True,
        )
    )


def redact_text(value: str) -> str:
    for secret in _secrets:
        value = value.replace(secret, "[redacted]")
    value = _bearer.sub("Bearer [redacted]", value)
    # Paths and query strings can contain user text or credentials; remove URLs in full.
    value = _url.sub("[redacted-url]", value)
    return value[:4096]


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[redacted]"
            if _sensitive.search(str(key))
            and not (
                str(key) in {"input_tokens", "output_tokens", "total_tokens"} and type(item) is int
            )
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value[:100]]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return "[redacted-object]"


def safe_log_event(_: object, __: str, event: MutableMapping[str, Any]) -> dict[str, Any]:
    # Never render arbitrary exception messages, stack locals or third-party traceback text.
    for key in ("exc_info", "exception", "stack", "stack_info"):
        if event.pop(key, None):
            event["exception_redacted"] = True
    event.pop("_record", None)
    event.pop("_from_structlog", None)
    return dict(redact(event))
