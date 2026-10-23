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
from autoscholar.llm.errors import LLMRequestRejected

Provider = Literal["llm", "tavily"]
LIMITS: dict[Provider, int] = {"llm": 10, "tavily": 2}
MAX_REQUEST_BYTES = 32768
MAX_RESPONSE_BYTES = 131072
MAX_OUTPUT_TOKENS = 4096


class ApprovalLedger:
    """Persist reservations before dispatch; never reset or implicitly reuse an allowance."""

    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        limits: dict[Provider, int] | None = None,
        blocked_case_ids: tuple[str, ...] = (),
    ) -> None:
        self.limits = dict(LIMITS if limits is None else limits)
        if set(self.limits) != set(LIMITS) or any(
            type(value) is not int or not 0 <= value <= LIMITS[key]
            for key, value in self.limits.items()
        ):
            raise ValueError("Paid allowance must not exceed the explicit default ceiling")
        self.blocked_case_ids = blocked_case_ids
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "limits": self.limits,
            "blocked_case_ids": list(blocked_case_ids),
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
        if case_id in self.blocked_case_ids:
            raise ValueError("Verified prior case must not be dispatched again")
        if any(row["provider"] == provider and row["case_id"] == case_id for row in attempts):
            raise ValueError("Approval forbids retries or duplicate case requests")
        if sum(row["provider"] == provider for row in attempts) >= self.limits[provider] or len(
            attempts
        ) >= sum(self.limits.values()):
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

    @classmethod
    def continue_unused(cls, path: Path, *, expected: dict[str, Any]) -> "ApprovalLedger":
        """Explicit continuation preserves all consumed attempts; no new allowance."""
        with path.open("rb") as stream:
            raw = stream.read(131073)
        if len(raw) > 131072 or decode_json(raw) != expected:
            raise ValueError("Existing approval changed; do not reset or resume it")
        if expected.get("limits") != {"llm": 9, "tavily": 2} or expected.get(
            "blocked_case_ids"
        ) != ["planning"]:
            raise ValueError("Only unused, bounded manual-follow-up allowance may continue")
        instance = cls.__new__(cls)
        instance.path = path
        instance.limits = dict(expected["limits"])
        instance.blocked_case_ids = ("planning",)
        instance.data = expected
        return instance

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
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
    ) -> None:
        versioned_pilot = (
            ledger.data.get("protocol_version") == 2 and ledger.data.get("cumulative_limit") == 180
        )
        repair_pilot = (ledger.data.get("protocol_version"), ledger.data.get("scope")) in {
            (3, "phase10_targeted_repairs_v3"),
            (4, "phase10_reviewer_retry_acceptance_v4"),
        } and ledger.limits == {"llm": 36, "tavily": 0}
        if max_output_tokens != MAX_OUTPUT_TOKENS and not (
            max_output_tokens == 8192 and (versioned_pilot or repair_pilot)
        ):
            raise ValueError("Expanded output cap requires the versioned remaining-budget pilot")
        self.max_output_tokens = max_output_tokens
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

    def validate_payload(self, payload: dict[str, Any], reserve_bytes: int = 0) -> dict[str, Any]:
        """Pure admission, shared by SDK previews and dispatch. Never reserve or write."""
        if type(reserve_bytes) is not int or reserve_bytes < 0:
            raise ValueError("Request preview reserve must be a nonnegative integer")
        try:
            incoming_size = len(httpx.Request("POST", self.endpoint, json=payload).content)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise LLMRequestRejected(
                code="llm_request_invalid", message="Provider request cannot be serialized safely"
            ) from exc
        try:
            no_secrets(payload, self.secrets)
        except ValueError as exc:
            raise LLMRequestRejected(
                code="llm_request_credentials_rejected", message="Credential content rejected"
            ) from exc
        capped = dict(payload)
        if self.provider == "llm":
            capped["max_tokens"] = self.max_output_tokens
        elif capped.get("search_depth") != "basic" or capped.get("max_results") != 3:
            raise LLMRequestRejected(
                code="llm_request_policy_rejected",
                message="Only three-result basic Tavily searches are authorized",
            )
        outgoing_size = len(json.dumps(capped, ensure_ascii=False, allow_nan=False).encode())
        size = max(incoming_size, outgoing_size)
        if size + reserve_bytes > MAX_REQUEST_BYTES:
            raise LLMRequestRejected(
                message=(
                    f"Provider request needs {size} bytes plus {reserve_bytes} reserved; "
                    f"limit {MAX_REQUEST_BYTES}; not sent"
                ),
                request_bytes=size,
                limit_bytes=MAX_REQUEST_BYTES,
            )
        return capped

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async with self.lock:
            if request.method != "POST" or request.url != self.endpoint or self.case_id is None:
                raise LLMRequestRejected(
                    code="llm_request_not_admitted",
                    message="Unexpected evaluation endpoint or request; no redirects",
                )
            raw = await request.aread()
            if len(raw) > MAX_REQUEST_BYTES:
                raise LLMRequestRejected(
                    message="Provider request exceeds the public payload bound",
                    request_bytes=len(raw),
                    limit_bytes=MAX_REQUEST_BYTES,
                )
            try:
                payload = decode_json(raw)
            except ValueError as exc:
                raise LLMRequestRejected(
                    code="llm_request_invalid", message="Invalid public request JSON; not sent"
                ) from exc
            if not isinstance(payload, dict):
                raise LLMRequestRejected(
                    code="llm_request_invalid", message="Expected bounded public request JSON"
                )
            payload = self.validate_payload(payload)
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            # Save only public JSON, not URLs, auth/cookie headers or dotenv values.
            request_path = self.output / f"request-{self.provider}-{self.case_id}.json"
            if request_path.exists():
                raise LLMRequestRejected(
                    code="llm_request_replay_forbidden", message="No implicit request replay"
                )
            _write_json(request_path, payload)
            try:
                index = self.ledger.reserve(self.provider, self.case_id, payload)
            except ValueError as exc:
                raise LLMRequestRejected(
                    code="llm_request_not_admitted",
                    message="Physical request admission rejected; not sent",
                ) from exc
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
                if response.status_code == 200:
                    captured = decode_json(bytes(content))
                    no_secrets(captured, self.secrets)
                    _write_json(
                        self.output / f"response-{self.provider}-{self.case_id}.json", captured
                    )
                self.ledger.finish(
                    index, state="returned", status=response.status_code, usage=usage
                )
                # aiter_bytes() already decompressed the wire body. Forwarding the old
                # encoding/length headers would make httpx decode it a second time.
                response_headers = dict(response.headers)
                for header in ("content-encoding", "content-length", "transfer-encoding"):
                    response_headers.pop(header, None)
                return httpx.Response(
                    response.status_code,
                    headers=response_headers,
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
