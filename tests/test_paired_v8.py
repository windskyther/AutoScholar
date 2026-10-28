"""Free v8 history, allowance, pairing and mock-wire full-schedule checks."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.evaluation import paired_validation as paired
from autoscholar.evaluation.live_ablation import execute_task
from autoscholar.evaluation.live_ablation_contract import TASKS
from autoscholar.evaluation.paired_protocol import (
    CLAIM,
    HISTORICAL_CLAIMS,
    PairedLedger,
    baseline_passed,
    history_snapshot,
    paired_contract,
    paired_summary,
    write_history,
)
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.workflow_knowledge import PUBLIC_KNOWLEDGE
from autoscholar.llm.models import ChatMessage, ConversationMessage, ToolDefinition
from tests.test_evaluation_workflow import RESOURCES, WorkflowReplaySandbox
from tests.test_live_ablation import SnapshotFixtureProvider
from tests.test_live_ablation_v2 import RenamedFixtureProvider, fixture
from tests.test_llm_provider import configured_settings
from tests.test_repair_validation import wire_root as wire_root


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def history(root: Path) -> Path:
    base = root / "data/evaluation"
    rows = {}
    for name in (*HISTORICAL_CLAIMS, "old-v6/acceptance.json"):
        target = base / name
        write(target, {"old_failed_result": False})
        rows[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    return write_history(base, rows)


def prepare(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(paired, "_git_state", lambda root: ("a" * 40, False))
    monkeypatch.setattr(paired, "load_fixture", lambda *args: (fixture(), "b" * 64))
    return paired.prepare_paired(root, history(root)) / "plan.json"


@pytest.mark.parametrize("changed", ["source", "fixture", "old_result", "claim", "shard", "index"])
def test_changed_frozen_input_blocks_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    path = prepare(tmp_path, monkeypatch)
    base = tmp_path / "data/evaluation"
    plan = paired.load_paired_plan(path, tmp_path)
    index = base / plan["history_index"]["path"]
    if changed == "source":
        monkeypatch.setattr(paired, "_git_state", lambda root: ("c" * 40, False))
    elif changed == "fixture":
        monkeypatch.setattr(paired, "load_fixture", lambda *args: (fixture(), "c" * 64))
    elif changed == "old_result":
        write(base / "old-v6/acceptance.json", {"regraded": True})
    elif changed == "claim":
        write(base / HISTORICAL_CLAIMS[-1], {"reset": True})
    elif changed == "shard":
        write(base / json.loads(index.read_text())["shards"][0]["path"], {})
    else:
        index.write_text(index.read_text() + " ")
    with pytest.raises(ValueError):
        paired.load_paired_plan(path, tmp_path)
    assert not (base / CLAIM).exists()
    assert not (base / "provider-approvals").exists()


def test_history_sharding_preserves_all_files_and_public_read_bounds(tmp_path: Path) -> None:
    rows = {}
    for ordinal in range(800):
        name = f"old-records/{ordinal:04}-" + "x" * 90 + ".json"
        target = tmp_path / name
        write(target, {"value": ordinal})
        rows[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    for name in HISTORICAL_CLAIMS:
        write(tmp_path / name, {})
        rows[name] = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
    index = write_history(tmp_path, rows)
    assert history_snapshot(index, tmp_path)[0] == rows
    shards = json.loads(index.read_text())["shards"]
    assert len(shards) > 1
    assert all((tmp_path / shard["path"]).stat().st_size <= 131072 for shard in shards)
    write(tmp_path / ("old-records/0000-" + "x" * 90 + ".json"), {})
    with pytest.raises(ValueError, match="Historical evidence"):
        history_snapshot(index, tmp_path)


@pytest.mark.parametrize("issue", ["missing_claim", "duplicate", "escape"])
def test_invalid_history_is_never_admitted(tmp_path: Path, issue: str) -> None:
    root = tmp_path / "repo"
    index = history(root)
    base = root / "data/evaluation"
    value = json.loads(index.read_text())
    shard = base / value["shards"][0]["path"]
    rows = json.loads(shard.read_text())
    if issue == "missing_claim":
        del rows[HISTORICAL_CLAIMS[0]]
    elif issue == "escape":
        outside = tmp_path / "outside.json"
        write(outside, {})
        rows["../../../outside.json"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    write(shard, rows)
    value["shards"][0]["sha256"] = hashlib.sha256(shard.read_bytes()).hexdigest()
    if issue == "duplicate":
        value["shards"].append(value["shards"][0])
    write(index, value)
    with pytest.raises(ValueError):
        history_snapshot(index, base)


def test_one_shot_180_ceiling_and_only_approved_output_cap(tmp_path: Path) -> None:
    ledger = PairedLedger(tmp_path / "grant.json", run_id="mock")
    for task in TASKS:
        for ordinal in range(1, 13):
            ledger.reserve("llm", f"{task}--{ordinal}", {})
        with pytest.raises(ValueError):
            ledger.reserve("llm", task + "--13", {})
    with pytest.raises(ValueError):
        ledger.reserve("tavily", TASKS[0] + "--1", {})
    with pytest.raises(FileExistsError):
        PairedLedger(tmp_path / "grant.json", run_id="reset")
    assert len(ledger.attempts) == 180
    for changed in (False, True):
        if changed:
            ledger.limits["llm"] = 181
        if changed:
            with pytest.raises(ValueError):
                GuardedTransport(
                    provider="llm",
                    endpoint="https://example.org/chat/completions",
                    ledger=ledger,
                    output=tmp_path,
                    secrets=(),
                    max_output_tokens=8192,
                )
        else:
            GuardedTransport(
                provider="llm",
                endpoint="https://example.org/chat/completions",
                ledger=ledger,
                output=tmp_path,
                secrets=(),
                max_output_tokens=8192,
            )


@pytest.mark.parametrize("stopping", [None, "baseline", "accounting", "resource", "identity"])
async def test_mock_wire_full_schedule_or_first_baseline_stop(
    wire_root: Path, monkeypatch: pytest.MonkeyPatch, stopping: str | None
) -> None:
    tmp_path = wire_root
    path = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        paired, "load_explicit_settings", lambda *args, **kwargs: (configured_settings(), None)
    )
    monkeypatch.setattr(paired, "DockerEvaluationSandbox", lambda *args: WorkflowReplaySandbox())
    monkeypatch.setattr(paired, "sandbox_resources", lambda *args, **kwargs: RESOURCES)

    cleanup_checks = 0

    async def clean(sandbox: Any) -> bool:
        nonlocal cleanup_checks
        cleanup_checks += 1
        return not (stopping == "resource" and cleanup_checks > 1)

    monkeypatch.setattr(paired, "no_owned_children", clean)
    if stopping == "baseline":
        monkeypatch.setattr(paired, "baseline_passed", lambda row: False)
    if stopping == "accounting":
        monkeypatch.setattr(paired, "physical_accounting", lambda *args: False)
    if stopping == "identity":

        async def identity_drift(**kwargs: Any) -> dict[str, Any]:
            row = await execute_task(**kwargs)
            row["observed_models"] = ["observed-model-a", "observed-model-b"]
            return row

        monkeypatch.setattr(paired, "execute_task", identity_drift)
    transport_type = GuardedTransport
    providers: dict[str, SnapshotFixtureProvider] = {}
    holder: dict[str, GuardedTransport] = {}

    async def respond(request: httpx.Request) -> httpx.Response:
        task = str(holder["transport"].case_id).rpartition("--")[0]
        scenario, _, variant = task.partition("-")
        if task not in providers:
            data = fixture()
            cls = SnapshotFixtureProvider if variant == "no_planner" else RenamedFixtureProvider
            providers[task] = cls(data, data.scripts[scenario], project_source=PUBLIC_KNOWLEDGE)
        payload = json.loads(request.content)
        messages: list[ConversationMessage] = [
            ChatMessage(role=m["role"], content=m["content"]) for m in payload["messages"]
        ]
        tools = [
            ToolDefinition(
                name=t["function"]["name"],
                description=t["function"]["description"],
                parameters=t["function"]["parameters"],
                strict=t["function"]["strict"],
            )
            for t in payload["tools"]
        ]
        result = await providers[task].generate(messages, tools=tools, tool_choice="auto")
        call = result.tool_calls[0]
        return httpx.Response(
            200,
            json={
                "id": "mock",
                "object": "chat.completion",
                "created": 1,
                "model": result.model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": call.id,
                                    "type": "function",
                                    "function": {
                                        "name": call.name,
                                        "arguments": json.dumps(call.arguments),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    def transport(**kwargs: Any) -> GuardedTransport:
        result = transport_type(**kwargs, delegate=httpx.MockTransport(respond))
        holder["transport"] = result
        return result

    monkeypatch.setattr(paired, "GuardedTransport", transport)
    args = argparse.Namespace(
        allow_paid=True, plan=path, approval_id="mock-v8-only", sandbox_container="fake"
    )
    directory, accepted = await paired.run_paired(args, repo_root=tmp_path)
    rows = json.loads((directory / "cases.json").read_text())
    summary = json.loads((directory / "acceptance.json").read_text())
    assert [r["task"] for r in rows] == list(TASKS[:1] if stopping else TASKS)
    assert accepted is (not stopping)
    assert summary["paired_measurement_complete"] is (not stopping)
    assert summary["phase10_overall_acceptance"] is False
    assert summary["statistical_benefit_claim"] is False
    assert summary["physical_requests"]["llm"] <= 180
    if not stopping:
        assert all(c["paired"] == 3 for c in summary["contrasts"])
        assert all(
            c["exact_p"] is not None and c["holm_p"] is not None for c in summary["contrasts"]
        )
        assert all(baseline_passed(row) for row in rows)
        ledger = object.__new__(PairedLedger)
        ledger.data = json.loads((directory / "request_ledger.json").read_text())
        ledger.limits = ledger.data["limits"]
        for broken in ("input", "model", "missing"):
            copy = json.loads(json.dumps(rows))
            if broken == "input":
                copy[1]["input_sha256"] = "f" * 64
            elif broken == "model":
                copy[1]["observed_models"] = ["another-model"]
            else:
                copy.pop()
            assert (
                paired_summary(copy, ledger, clean=True, unchanged=True)["paired_pilot_acceptance"]
                is False
            )
        copy = json.loads(json.dumps(rows))
        copy[0]["task_success"] = False
        assert not baseline_passed(copy[0])
        assert not paired_summary(copy, ledger, clean=True, unchanged=True)[
            "paired_pilot_acceptance"
        ]
    args.approval_id = "reset-with-other-id"
    with pytest.raises(ValueError, match="already claimed"):
        await paired.run_paired(args, repo_root=tmp_path)


def test_free_preparation_never_loads_secrets_or_creates_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("No dotenv/provider access in free preparation")

    monkeypatch.setattr(paired, "load_explicit_settings", forbidden)
    path = prepare(tmp_path, monkeypatch)
    value = paired.load_paired_plan(path, tmp_path)
    assert value["contract"] == paired_contract()
    assert value["paid_execution_approved"] is False
    assert not (tmp_path / "data/evaluation" / CLAIM).exists()
