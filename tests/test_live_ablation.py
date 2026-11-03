"""Free contract tests. Fake providers/training here are not live measurements."""

import argparse
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.evaluation import live_ablation as live
from autoscholar.evaluation.component_fixture import load_fixture
from autoscholar.evaluation.live_ablation_contract import (
    OBJECTIVE_SUFFIX,
    SCENARIOS,
    TASKS,
    ClosedCorpusSearch,
    LiveAblationLedger,
    LiveWorkflowProvider,
)
from autoscholar.evaluation.models import MeasuredUsage
from autoscholar.evaluation.provider_transport import ApprovalLedger, GuardedTransport
from autoscholar.evaluation.workflow_ablation import WORKFLOW_VARIANTS, WorkflowVariant
from autoscholar.evaluation.workflow_fixture import ScriptedWorkflowProvider, WorkflowFixture
from autoscholar.evaluation.workflow_knowledge import PUBLIC_KNOWLEDGE
from autoscholar.llm.models import (
    ChatMessage,
    ConversationMessage,
    LLMResult,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from tests.test_evaluation_workflow import RESOURCES, ROOT, WorkflowReplaySandbox


def fixture() -> WorkflowFixture:
    return load_fixture(ROOT / "benchmarks/ablations/workflow_fixture_v1.json", WorkflowFixture)[0]


def test_request_limits_persist_and_cannot_reset(tmp_path: Path) -> None:
    path = tmp_path / "grant.json"
    ledger = LiveAblationLedger(path, run_id="test")
    for task in TASKS:
        for ordinal in range(1, 13):
            ledger.reserve("llm", f"{task}--{ordinal}", {})
        with pytest.raises(ValueError):
            ledger.reserve("llm", f"{task}--13", {})
    assert len(ledger.attempts) == 180
    assert len(json.loads(path.read_text(encoding="utf-8"))["attempts"]) == 180
    with pytest.raises(FileExistsError):
        LiveAblationLedger(path, run_id="different")
    with pytest.raises(ValueError):
        ApprovalLedger(tmp_path / "legacy.json", run_id="test", limits={"llm": 180, "tavily": 0})


@pytest.mark.parametrize(
    "identifier", ["normal-baseline--0", "normal-baseline--2", "other--1", "../x--1"]
)
def test_invalid_schedule(tmp_path: Path, identifier: str) -> None:
    ledger = LiveAblationLedger(tmp_path / "grant.json", run_id="test")
    with pytest.raises(ValueError):
        ledger.reserve("llm", identifier, {})
    with pytest.raises(ValueError):
        ledger.reserve("tavily", "normal-baseline--1", {})
    assert not ledger.attempts


async def test_http_guard_reserves_before_send(tmp_path: Path) -> None:
    ledger = LiveAblationLedger(tmp_path / "grant.json", run_id="test")
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert len(ledger.attempts) == 1
        assert json.loads(request.content)["max_tokens"] == 4096
        return httpx.Response(
            200, json={"usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}
        )

    transport = GuardedTransport(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=("secret-key",),
        delegate=httpx.MockTransport(respond),
    )
    transport.case_id = "normal-baseline--1"
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post("https://example.org/chat/completions", json={"messages": []})
        transport.case_id = "normal-baseline--2"
        with pytest.raises(ValueError):
            await client.post(
                "https://example.org/chat/completions", json={"messages": ["secret-key"]}
            )
        with pytest.raises(ValueError):
            await client.post("https://api.tavily.com/search", json={})
    assert len(seen) == len(ledger.attempts) == 1


async def test_closed_corpus_arbitrary_queries() -> None:
    search = ClosedCorpusSearch(fixture())
    response = await search.search("MNIST CPU subset limitations", limit=3)
    assert response.results[0].external_id == "mnist"
    assert len(response.results) == 1 and search.calls == 1
    assert all(item.provider == "closed-public-engineering-corpus" for item in response.results)


class SnapshotFixtureProvider(ScriptedWorkflowProvider):
    """Fake responses derive IDs from actual candidate order, only for offline tests."""

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        serialized = {
            "model": "configured-model-placeholder",
            "max_tokens": 4096,
            "messages": [OpenAICompatibleProvider._serialize_message(item) for item in messages],
            "tools": [OpenAICompatibleProvider._serialize_tool(item) for item in tools or []],
            "tool_choice": tool_choice,
            "parallel_tool_calls": False,
        }
        assert len(json.dumps(serialized, ensure_ascii=False).encode()) <= 32768
        result = await super().generate(messages, tools=tools, tool_choice=tool_choice)
        if result.tool_calls[0].name == "submit_evidence":
            message = messages[-1]
            assert isinstance(message, ChatMessage)
            candidates = json.loads(
                message.content.split("<untrusted_sources>\n", 1)[1].split(
                    "\n</untrusted_sources>", 1
                )[0]
            )
            arguments: dict[str, Any] = {
                "items": [
                    {
                        "candidate_id": item["candidate_id"],
                        "claim": item["content"],
                        "relevance": 1.0,
                    }
                    for item in candidates
                ]
            }
            self.claims = [item["content"] for item in candidates]
            return LLMResult(
                text="",
                model=result.model,
                usage=result.usage,
                tool_calls=(
                    ToolCall(
                        id=result.tool_calls[0].id, name="submit_evidence", arguments=arguments
                    ),
                ),
            )
        if result.tool_calls[0].name == "submit_research_report":
            arguments = {
                "answer": "\n".join(
                    f"{claim} [E{index}]" for index, claim in enumerate(self.claims, 1)
                ),
                "citations": [
                    {"claim": claim, "evidence_ids": [f"E{index}"]}
                    for index, claim in enumerate(self.claims, 1)
                ],
            }
            return LLMResult(
                text="",
                model=result.model,
                usage=result.usage,
                tool_calls=(
                    ToolCall(
                        id=result.tool_calls[0].id,
                        name="submit_research_report",
                        arguments=arguments,
                    ),
                ),
            )
        return result


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("variant", WORKFLOW_VARIANTS)
async def test_live_path_with_explicit_offline_fakes(
    tmp_path: Path, scenario: str, variant: WorkflowVariant
) -> None:
    data = fixture()
    script = data.scripts[scenario].model_copy(
        update={"prompt": data.scripts[scenario].prompt + OBJECTIVE_SUFFIX}
    )
    ledger = LiveAblationLedger(tmp_path / "grant.json", run_id="fake-only")
    transport = GuardedTransport(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        delegate=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    provider = LiveWorkflowProvider(
        SnapshotFixtureProvider(data, script, project_source=PUBLIC_KNOWLEDGE),
        transport,
        f"{scenario}-{variant}",
    )
    outcome = await live.execute_task(
        directory=tmp_path,
        fixture=data,
        script=script,
        variant=variant,
        provider=provider,
        sandbox=WorkflowReplaySandbox(),
        resources=RESOURCES,
    )
    assert outcome["status"] == "measured", outcome
    assert outcome["intervention_verified"]
    assert outcome["task_success"] is (
        scenario == "normal" or (scenario == "recover" and variant != "no_replanning")
    ), outcome
    assert not ledger.attempts  # This test used no physical model requests.


async def test_unapproved_does_not_load_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Unapproved path read settings")

    monkeypatch.setattr(live, "load_explicit_settings", forbidden)
    with pytest.raises(ValueError, match="allow-paid"):
        await live.run_live_ablations(argparse.Namespace(allow_paid=False), repo_root=tmp_path)


async def test_claim_blocks_new_approval_before_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data/evaluation"
    root.mkdir(parents=True)
    (root / "live-ablation-v1-claim.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(live, "_git_state", lambda root: ("a" * 40, False))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A new approval bypassed the existing pilot claim")

    monkeypatch.setattr(live, "load_explicit_settings", forbidden)
    with pytest.raises(ValueError, match="already claimed"):
        await live.run_live_ablations(
            argparse.Namespace(allow_paid=True, approval_id="different"),
            repo_root=tmp_path,
        )


async def test_provider_error_is_not_retried(tmp_path: Path) -> None:
    data = fixture()
    native = ScriptedWorkflowProvider(data, data.scripts["normal"])
    ledger = LiveAblationLedger(tmp_path / "grant.json", run_id="fake-only")
    transport = GuardedTransport(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        delegate=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    provider = LiveWorkflowProvider(native, transport, "normal-baseline")
    with pytest.raises(ValueError):
        await provider.generate([])  # Fake provider rejects missing tool definitions.
    assert provider.failed
    with pytest.raises(ValueError, match="stopped"):
        await provider.generate([])
    assert provider.calls == 1


def test_summary_missing_is_unknown(tmp_path: Path) -> None:
    ledger = LiveAblationLedger(tmp_path / "grant.json", run_id="fake-only")
    records: list[dict[str, Any]] = []
    for task in TASKS:
        index = ledger.reserve("llm", task + "--1", {})
        ledger.finish(index, state="returned", status=200, usage=MeasuredUsage(total_tokens=3))
        records.append(
            {
                "task": task,
                "status": "measured",
                "task_success": not task.startswith("deception"),
                "input_sha256": task.split("-")[0],
                "observed_models": ["fake"],
                "intervention_verified": True,
                "physical_accounting_verified": True,
            }
        )
    summary = live.pilot_summary(records, ledger, clean=True, unchanged=True)
    assert summary["live_pilot_measurement_complete"]
    assert not summary["phase10_overall_acceptance"] and not summary["main_merge_authorized"]
    assert all(item["holm_p"] == 1 for item in summary["contrasts"])
    records[0]["task_success"] = None
    partial = live.pilot_summary(records, ledger, clean=True, unchanged=True)
    assert not partial["live_pilot_measurement_complete"]
    assert all(item["paired"] == 2 and item["holm_p"] is None for item in partial["contrasts"])
