import json
from pathlib import Path

import httpx
import pytest

from autoscholar.evaluation import provider_transport as transport_module
from autoscholar.evaluation.provider_diagnostics import error_diagnostic
from autoscholar.evaluation.provider_transport import ApprovalLedger, GuardedTransport
from autoscholar.evaluation.runner import _write_json


@pytest.mark.parametrize("status", [400, 401, 429, 500])
@pytest.mark.parametrize(
    "body",
    [
        b'{"error":{"type":"invalid_request_error","param":"tool_choice",'
        b'"message":"tool_choice required not supported in thinking mode; key=private-secret",'
        b'"request":"private-secret"}}',
        b"<html>private-secret</html>",
        b'{"error": ["private-secret"]}',
        b'{"error":{"message":{"secret":"private-secret"},"param":[],"type":{}}}',
    ],
)
async def test_non_200_diagnostics_preserve_response_and_unknown_usage(
    tmp_path: Path, status: int, body: bytes
) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, content=body, headers={"secret-header": "private-secret"})

    ledger = ApprovalLedger(tmp_path / "grant.json", run_id="offline")
    guard = GuardedTransport(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=("private-secret",),
        delegate=httpx.MockTransport(respond),
    )
    guard.case_id = "offline-case"
    async with httpx.AsyncClient(transport=guard) as client:
        response = await client.post(str(guard.endpoint), json={"messages": []})
    assert response.status_code == status and response.content == body
    assert len(calls) == len(ledger.attempts) == 1
    assert ledger.attempts[0]["state"] == "returned"
    assert ledger.attempts[0]["usage"]["total_tokens"] is None
    raw = (tmp_path / "error-llm-offline-case.json").read_text()
    assert "private-secret" not in raw and "secret-header" not in raw
    record = json.loads(raw)
    assert record["http_status"] == status and record["raw_body_omitted"]
    if b'"param":"tool_choice"' in body:
        assert record["parameter"] == "tool_choice"
        assert {"tool_choice", "required", "thinking"} <= set(record["reported_terms"])


async def test_diagnostic_disk_error_does_not_turn_received_400_into_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _write_json

    def write(path: Path, value: object) -> None:
        if path.name.startswith("error-"):
            raise OSError("Full diagnostic disk")
        original(path, value)

    monkeypatch.setattr(transport_module, "_write_json", write)
    ledger = ApprovalLedger(tmp_path / "grant.json", run_id="offline")
    guard = GuardedTransport(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        delegate=httpx.MockTransport(lambda request: httpx.Response(400, json={"error": {}})),
    )
    guard.case_id = "offline-case"
    async with httpx.AsyncClient(transport=guard) as client:
        assert (await client.post(str(guard.endpoint), json={})).status_code == 400
    assert ledger.attempts[0]["state"] == "returned"


def test_schema_error_diagnostics_and_unknown_fields() -> None:
    result = error_diagnostic(
        json.dumps(
            {
                "error": {
                    "message": "Unsupported strict schema: minLength and maxLength",
                    "type": "private-secret",
                    "param": "private-secret",
                }
            }
        ).encode(),
        400,
    )
    assert {"schema", "minlength", "maxlength"} <= set(result["reported_terms"])
    assert result["error_type"] is result["parameter"] is None
