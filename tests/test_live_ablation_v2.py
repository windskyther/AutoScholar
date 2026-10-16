"""Versioned allowance, semantic DAG grading and fake-provider workflow regression."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.evaluation import live_ablation as live
from autoscholar.evaluation.component_fixture import load_fixture
from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.live_ablation_contract import SCENARIOS, TASKS, LiveAblationLedger
from autoscholar.evaluation.live_ablation_v2 import (
    PRIOR_APPROVAL,
    SUFFIX,
    DiverseCorpusSearch,
    ExtractiveWorkflowProvider,
    PriorPilot,
    RemainingPilotLedger,
    plan_roles,
)
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.workflow_ablation import WORKFLOW_VARIANTS, WorkflowVariant
from autoscholar.evaluation.workflow_fixture import WorkflowFixture, workflow_plan
from autoscholar.evaluation.workflow_knowledge import PUBLIC_KNOWLEDGE
from autoscholar.llm.models import ConversationMessage, LLMResult, ToolChoice, ToolDefinition
from autoscholar.orchestration.durable import DurableService
from tests.test_evaluation_workflow import RESOURCES, ROOT, WorkflowReplaySandbox
from tests.test_live_ablation import SnapshotFixtureProvider


def fixture() -> WorkflowFixture:
    return load_fixture(ROOT / "benchmarks/ablations/workflow_fixture_v2.json", WorkflowFixture)[0]


def fake_prior(root: Path) -> PriorPilot:
    folder = root / ("live-ablation-" + "a" * 32)
    folder.mkdir(parents=True)

    def write(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    manifest = {"configured_model": "fake-model"}
    write(folder / "manifest.json", manifest)
    ledger: dict[str, Any] = {
        "run_id": folder.name,
        "limits": {"llm": 180, "tavily": 0},
        "tasks": list(TASKS),
        "attempts": [],
    }
    for i in range(21):
        task = TASKS[i // 12]
        identifier = f"{task}--{i % 12 + 1}"
        request = {"messages": [], "model": "fake-model"}
        ledger["attempts"].append(
            {
                "case_id": identifier,
                "provider": "llm",
                "state": "returned",
                "http_status": 200,
                "request_sha256": payload_digest(request),
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            }
        )
        write(folder / f"request-llm-{identifier}.json", request)
        write(
            folder / f"response-llm-{identifier}.json",
            {"usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}},
        )
    write(
        root / "live-ablation-v1-claim.json",
        {
            "run_id": folder.name,
            "approval_id": PRIOR_APPROVAL,
            "manifest_sha256": payload_digest(manifest),
        },
    )
    write(root / "provider-approvals" / (PRIOR_APPROVAL + ".json"), ledger)
    write(folder / "request_ledger.json", ledger)
    write(folder / "cases.json", [])
    write(folder / "acceptance.json", {})
    return PriorPilot(folder, root=root)


def test_remaining_budget_and_single_v2_claim(tmp_path: Path) -> None:
    prior = fake_prior(tmp_path)
    ledger = RemainingPilotLedger(tmp_path / "v2.json", run_id="fake", prior=prior)
    for index in range(159):
        ledger.reserve("llm", f"{TASKS[index // 12]}--{index % 12 + 1}", {})
    assert len(ledger.attempts) + len(prior.ledger["attempts"]) == 180
    with pytest.raises(ValueError, match="exhausted"):
        ledger.reserve("llm", f"{TASKS[159 // 12]}--{159 % 12 + 1}", {})
    with pytest.raises(FileExistsError):
        RemainingPilotLedger(tmp_path / "reset.json", run_id="other", prior=prior)
    assert prior.unchanged()


@pytest.mark.parametrize(
    "target", ["manifest.json", "request_ledger.json", "response-llm-normal-baseline--1.json"]
)
def test_prior_tampering_blocks_requests(tmp_path: Path, target: str) -> None:
    prior = fake_prior(tmp_path)
    ledger = RemainingPilotLedger(tmp_path / "v2.json", run_id="fake", prior=prior)
    (prior.directory / target).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        ledger.reserve("llm", TASKS[0] + "--1", {})
    assert not ledger.attempts


async def test_output_cap_requires_versioned_budget(tmp_path: Path) -> None:
    prior = fake_prior(tmp_path)
    ledger = RemainingPilotLedger(tmp_path / "v2.json", run_id="fake", prior=prior)

    def response(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["max_tokens"] == 8192
        return httpx.Response(
            200, json={"usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}
        )

    transport = GuardedTransport(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        max_output_tokens=8192,
        delegate=httpx.MockTransport(response),
    )
    transport.case_id = TASKS[0] + "--1"
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post("https://example.org/chat/completions", json={"messages": []})
    assert len(ledger.attempts) == 1
    old = LiveAblationLedger(tmp_path / "old.json", run_id="fake")
    with pytest.raises(ValueError, match="Expanded"):
        GuardedTransport(
            provider="llm",
            endpoint="https://example.org/chat/completions",
            ledger=old,
            output=tmp_path,
            secrets=(),
            max_output_tokens=8192,
        )


async def test_query_coverage_preserves_public_sources() -> None:
    data = fixture()
    search = DiverseCorpusSearch(data)
    topics = []
    for query in (
        "MNIST comparison CPU subset",
        "sandbox PyTorch isolation",
        "training recovery checkpoint artifacts MNIST",
    ):
        response = await search.search(query)
        item = response.results[0]
        assert item.content == data.sources[item.external_id or ""].content
        topics.append(item.external_id)
    assert len(set(topics)) == 3 and search.calls == 3


def test_dag_roles_not_literal_ids() -> None:
    plan = workflow_plan(fixture().scripts["normal"])
    plan.steps[0].id = "sources"
    plan.steps[1].dependencies = ["sources"]
    assert plan_roles(plan) == {"research": "sources", "code": "code", "train": "train"}
    plan.steps[2].dependencies.append("sources")
    assert not plan_roles(plan)


class RenamedFixtureProvider(SnapshotFixtureProvider):
    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        result = await super().generate(messages, tools=tools, tool_choice=tool_choice)
        call = result.tool_calls[0]
        if call.name not in {"submit_task_plan", "submit_plan_revision"}:
            return result
        args = json.loads(json.dumps(call.arguments))
        plan = args if call.name == "submit_task_plan" else args["plan"]
        for step in plan["steps"]:
            step["id"] = "x-" + step["id"]
            step["dependencies"] = ["x-" + key for key in step["dependencies"]]
        if call.name == "submit_plan_revision":
            args["rerun_steps"] = ["x-" + key for key in args["rerun_steps"]]
        return replace(result, tool_calls=(replace(call, arguments=args),))


@pytest.mark.parametrize("variant", WORKFLOW_VARIANTS)
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_v2_workflow_with_fake_provider_and_training(
    tmp_path: Path, scenario: str, variant: WorkflowVariant
) -> None:
    data = fixture()
    script = data.scripts[scenario].model_copy(
        update={"prompt": data.scripts[scenario].prompt + SUFFIX}
    )
    ledger = LiveAblationLedger(tmp_path / "fake.json", run_id="fake-only")
    transport = GuardedTransport(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        delegate=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    # No Planner uses the static original IDs, including any subsequent revision.
    provider_type = SnapshotFixtureProvider if variant == "no_planner" else RenamedFixtureProvider
    provider = ExtractiveWorkflowProvider(
        provider_type(data, script, project_source=PUBLIC_KNOWLEDGE),
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
        protocol_version=2,
    )
    assert outcome["status"] == "measured", outcome
    assert outcome["intervention_verified"]
    assert outcome["task_success"] is (
        scenario == "normal" or (scenario == "recover" and variant != "no_replanning")
    ), outcome
    assert not ledger.attempts


async def test_unfinished_claim_is_not_a_measured_model_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def interrupted_tick(self: DurableService, task_id: str | None = None) -> bool:
        assert await self.repository.claim(self.owner, self.lease_seconds, task_id)
        return True

    monkeypatch.setattr(DurableService, "tick", interrupted_tick)
    data = fixture()
    script = data.scripts["normal"]
    ledger = LiveAblationLedger(tmp_path / "fake.json", run_id="fake-only")
    transport = GuardedTransport(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        delegate=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    provider = ExtractiveWorkflowProvider(
        RenamedFixtureProvider(data, script, project_source=PUBLIC_KNOWLEDGE),
        transport,
        "normal-baseline",
    )
    outcome = await live.execute_task(
        directory=tmp_path,
        fixture=data,
        script=script,
        variant="baseline",
        provider=provider,
        sandbox=WorkflowReplaySandbox(),
        resources=RESOURCES,
        protocol_version=2,
    )
    assert outcome["status"] == "infrastructure_incomplete"
    assert outcome["task_success"] is None
    assert not ledger.attempts
