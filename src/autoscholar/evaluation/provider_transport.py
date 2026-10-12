"""One-shot approval ledger and physical-request guard for explicit paid evaluation."""

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from autoscholar.evaluation.datasets import decode_json, payload_digest
from autoscholar.evaluation.models import MeasuredUsage
from autoscholar.evaluation.runner import _write_json

Provider = Literal["llm", "tavily"]
LIMITS: dict[Provider, int] = {"llm": 10, "tavily": 2}
MAX_REQUEST_BYTES = 32768
MAX_RESPONSE_BYTES = 131072
MAX_OUTPUT_TOKENS = 4096


class ApprovalLedger:
    """Reservations are persisted BEFORE network dispatch. Never resume/reuse a grant."""

    def __init__(self, path: Path, *, run_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "limits": LIMITS,
            "attempts": [],
            "automatic_retries": False,
        }
        # A fixed caller-supplied approval ID cannot reset its allowance on a new CLI run.
        with path.open("x", encoding="utf-8") as stream:
            json.dump(self.data, stream, allow_nan=False)

    @property
    def attempts(self) -> list[dict[str, Any]]:
        return self.data["attempts"]  # type: ignore[no-any-return]

    def reserve(self, provider: Provider, case_id: str, payload: dict[str, Any]) -> int:
        attempts = self.attempts
        if any(row["provider"] == provider and row["case_id"] == case_id for row in attempts):
            raise ValueError("Approval forbids retries or duplicate case requests")
        if (
            sum(row["provider"] == provider for row in attempts) >= LIMITS[provider]
            or len(attempts) >= 12
        ):
            raise ValueError("Explicit paid request allowance exhausted")
        attempts.append(
            {
                "provider": provider,
                "case_id": case_id,
                "state": "reserved",
                "started_at": datetime.now(UTC).isoformat(),
                "request_sha256": payload_digest(payload),
                "http_status": None,
                "usage": MeasuredUsage().model_dump(),
            }
        )
        _write_json(self.path, self.data)
        return len(attempts) - 1

    def finish(
        self,
        index: int,
        *,
        state: str,
        status: int | None = None,
        usage: MeasuredUsage | None = None,
    ) -> None:
        row = self.attempts[index]
        row.update(state=state, http_status=status, completed_at=datetime.now(UTC).isoformat())
        if usage is not None:
            row["usage"] = usage.model_dump()
        _write_json(self.path, self.data)


def approval_path(root: Path, identifier: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", identifier):
        raise ValueError("Use a public approval ID, never a key or path")
    target = (root / "provider-approvals" / (identifier + ".json")).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Approval ledger escaped evaluation output")
    return target


def no_secrets(payload: object, secrets: tuple[str, ...]) -> None:
    def strings(item: object) -> list[str]:
        if isinstance(item, str):
            return [item]
        if isinstance(item, dict):
            return [text for key, value in item.items() for text in strings(key) + strings(value)]
        if isinstance(item, list | tuple):
            return [text for value in item for text in strings(value)]
        return []

    if any(secret and secret in text for text in strings(payload) for secret in secrets):
        raise ValueError("Credential content rejected; only authentication headers may carry keys")


class GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        provider: Provider,
        endpoint: str,
        ledger: ApprovalLedger,
        output: Path,
        secrets: tuple[str, ...],
        delegate: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider, self.endpoint, self.ledger, self.output, self.secrets = (
            provider,
            httpx.URL(endpoint),
            ledger,
            output,
            secrets,
        )
        if (
            self.endpoint.scheme != "https"
            or self.endpoint.userinfo
            or self.endpoint.query
            or self.endpoint.fragment
        ):
            raise ValueError("Only explicit credential-free HTTPS provider endpoints are allowed")
        self.delegate = delegate or httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self.case_id: str | None = None
        self.lock = asyncio.Lock()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async with self.lock:
            if request.method != "POST" or request.url != self.endpoint or self.case_id is None:
                raise ValueError("Unexpected evaluation endpoint or request; no redirects")
            raw = await request.aread()
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("Provider request exceeds the public payload bound")
            payload = decode_json(raw)
            if not isinstance(payload, dict):
                raise ValueError("Expected bounded public request JSON")
            no_secrets(payload, self.secrets)
            if self.provider == "llm":
                payload["max_tokens"] = MAX_OUTPUT_TOKENS
            elif payload.get("search_depth") != "basic" or payload.get("max_results") != 3:
                raise ValueError("Only three-result basic Tavily searches are authorized")
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("Capped provider request exceeds size limit")
            # Save only public JSON, not URLs, auth/cookie headers or dotenv values.
            request_path = self.output / f"request-{self.provider}-{self.case_id}.json"
            if request_path.exists():
                raise ValueError("No implicit request replay")
            _write_json(request_path, payload)
            index = self.ledger.reserve(self.provider, self.case_id, payload)
            headers = dict(request.headers)
            headers.pop("content-length", None)
            forwarded = httpx.Request(
                "POST", self.endpoint, headers=headers, content=raw, extensions=request.extensions
            )
            response = None
            try:
                response = await self.delegate.handle_async_request(forwarded)
                content = bytearray()
                async for block in response.aiter_bytes():
                    content.extend(block)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise ValueError("Provider response exceeds bounded record size")
                usage = MeasuredUsage(external_api_calls=1, model_calls=int(self.provider == "llm"))
                if self.provider == "tavily":
                    usage = MeasuredUsage(
                        external_api_calls=1,
                        model_calls=0,
                        input_tokens=0,
                        output_tokens=0,
                        total_tokens=0,
                    )
                else:
                    try:
                        data = decode_json(bytes(content))
                        raw_usage = data.get("usage") if isinstance(data, dict) else None
                        if isinstance(raw_usage, dict):
                            usage = MeasuredUsage(
                                external_api_calls=1,
                                model_calls=1,
                                input_tokens=raw_usage.get("prompt_tokens"),
                                output_tokens=raw_usage.get("completion_tokens"),
                                total_tokens=raw_usage.get("total_tokens"),
                            )
                    except ValueError:
                        pass  # Missing/invalid vendor accounting is unknown, never zero.
                self.ledger.finish(
                    index, state="returned", status=response.status_code, usage=usage
                )
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=bytes(content),
                    request=request,
                )
            except BaseException:
                self.ledger.finish(
                    index, state="unknown", status=response.status_code if response else None
                )
                raise
            finally:
                if response is not None:
                    await response.aclose()

    async def aclose(self) -> None:
        await self.delegate.aclose()
