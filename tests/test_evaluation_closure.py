"""Free mock contracts; no live model acceptance inferred from synthetic histories."""

import argparse
import gzip
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.evaluation import phase10_closure as audit
from autoscholar.evaluation import provider_chinese as chinese
from autoscholar.evaluation import provider_closure as closure
from autoscholar.evaluation.provider_cases import CASES, SYSTEM, grade_research
from autoscholar.evaluation.provider_transport import GuardedTransport, approval_path
from tests import test_evaluation_providers as t

REVISION = t.REVISION
make_independent = t.test_continue_only_unattempted_independent_contracts_preserves_consumed_ledger
run_mock = t.test_full_cli_contract_with_explicit_mock_transport


async def history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    independent = await make_independent(tmp_path, monkeypatch)
    root = tmp_path / "data/evaluation"
    original = root / ("provider-" + "0" * 32)
    tools = await run_mock(
        tmp_path, monkeypatch, None, prior_run=original, confirm_from=independent
    )
    monkeypatch.setattr(closure, "same_grader_source", lambda *args: True)
    monkeypatch.setattr(chinese, "_git_state", lambda *args: (REVISION, False))
    monkeypatch.setattr(chinese, "same_grader_source", lambda *args: True)
    return {"original_run": original, "independent_run": independent, "tool_run": tools}


def answer() -> dict[str, Any]:
    return {
        "sorts_in_place": True,
        "return_value": None,
        "citations": [
            {"id": "E1", "quote": "Sort the list in place. Methods that modify lists return None."}
        ],
    }


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "string_none",
        "duplicate_id",
        "bad_fact",
        "fake_quote",
        "unsupported_claim",
        "extra_field",
    ],
)
def test_v2_frozen_grade(fault: str | None) -> None:
    payload = answer()
    evidence = [{"id": "E1", "content": payload["citations"][0]["quote"]}]
    if fault == "string_none":
        payload["return_value"] = "None"
    elif fault == "duplicate_id":
        payload["citations"].append(dict(payload["citations"][0]))
    elif fault == "bad_fact":
        payload["sorts_in_place"] = False
    elif fault == "fake_quote":
        payload["citations"][0]["quote"] = "Invented text, not in a source"
    elif fault == "unsupported_claim":
        evidence[0]["content"] = payload["citations"][0]["quote"] = (
            "This text explains something unrelated"
        )
    elif fault == "extra_field":
        payload["unasked_field"] = True
    checks = chinese.chinese_checks(payload, evidence)
    assert all(checks.values()) is (fault is None)
    assert chinese.CHINESE_CASE.prompt.startswith(CASES[8].prompt)
    assert chinese.ANSWER_SCHEMA["properties"]["return_value"]["type"] == "null"
    assert chinese.contract()["unique_source_ids"] is True
    if fault is None:
        assert all(grade_research(CASES[8], payload, evidence).values())


async def test_offline_prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = await history(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in (tmp_path / "data/evaluation").rglob("*.json")}

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "Offline preparation must not load settings or create a network client"
        )

    monkeypatch.setattr(chinese, "load_explicit_settings", forbidden)
    monkeypatch.setattr(chinese, "GuardedTransport", forbidden)
    directory, accepted = await chinese.run_chinese(
        argparse.Namespace(**paths, allow_paid=False, approval_id=None), repo_root=tmp_path
    )
    assert accepted is False
    summary = json.loads((directory / "chinese_acceptance.json").read_text(encoding="utf-8"))
    assert summary["state"] == "awaiting_approval" and summary["requests_this_run"] == 0
    assert summary["cumulative_requests_total"] == 14
    assert all(path.read_bytes() == raw for path, raw in before.items())


@pytest.mark.parametrize("fault", [None, "format", "rate_limit", "connection"])
async def test_one_llm_no_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> Path:
    paths = await history(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in (tmp_path / "data/evaluation").rglob("*.json")}
    sent = []

    def factory(**kwargs: Any) -> GuardedTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            assert kwargs["provider"] == "llm"
            assert sent[0]["max_tokens"] == 4096
            assert sent[0]["messages"][0] == {"role": "system", "content": SYSTEM}
            assert sent[0]["messages"][1]["content"] == chinese.CHINESE_CASE.prompt
            assert "Retrieved public excerpts" in sent[0]["messages"][2]["content"]
            if fault == "connection":
                raise httpx.ConnectError("mock connection fault", request=request)
            if fault == "rate_limit":
                return httpx.Response(429, json={"error": {"message": "no retry"}})
            payload = answer()
            if fault == "format":
                payload["return_value"] = "None"
            body = {
                "id": "mock-chinese",
                "object": "chat.completion",
                "created": 1,
                "model": "explicit-test-stub",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(payload)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            }
            return httpx.Response(
                200,
                content=gzip.compress(json.dumps(body).encode()),
                headers={"content-encoding": "gzip"},
            )

        return GuardedTransport(**kwargs, delegate=httpx.MockTransport(handler))

    monkeypatch.setattr(chinese, "GuardedTransport", factory)
    args = argparse.Namespace(**paths, allow_paid=True, approval_id="mock-single-chinese")
    directory, accepted = await chinese.run_chinese(args, repo_root=tmp_path)
    assert accepted is (fault is None)
    assert len(sent) == 1
    summary = json.loads((directory / "chinese_acceptance.json").read_text(encoding="utf-8"))
    assert summary["cumulative_requests_total"] == 15 and summary["requests_this_run"] == 1
    assert summary["phase10_overall_acceptance"] is False
    assert all(path.read_bytes() == raw for path, raw in before.items())
    with pytest.raises(ValueError, match="explicit approval"):
        await chinese.run_chinese(args, repo_root=tmp_path)
    args.approval_id = "different-id-cannot-retry"
    with pytest.raises(ValueError, match="explicit approval"):
        await chinese.run_chinese(args, repo_root=tmp_path)
    ledger = json.loads(
        approval_path(tmp_path / "data/evaluation", "mock-single-chinese").read_text(
            encoding="utf-8"
        )
    )
    assert ledger["limits"] == {"llm": 1, "tavily": 0}
    proof = await closure.load_provider_closure(repo_root=tmp_path, **paths)
    checked = chinese.inspect_chinese(directory, proof=proof)
    assert checked["accepted"] is accepted and len(checked["attempts"]) == 1
    return directory


async def test_changed_tool_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = await history(tmp_path, monkeypatch)
    proof = await closure.load_provider_closure(repo_root=tmp_path, **paths)
    assert proof.unchanged() and len(proof.attempts) == 14
    output = paths["tool_run"] / "native_tool_result-response.json"
    body = json.loads(output.read_text(encoding="utf-8"))
    body["text"] = '{"result":470}'
    output.write_text(json.dumps(body), encoding="utf-8")
    assert not proof.unchanged()
    with pytest.raises(ValueError, match="hash differs"):
        await closure.load_provider_closure(repo_root=tmp_path, **paths)


@pytest.mark.parametrize("mutation", ["response", "cached", "request", "grant", "score", "claim"])
async def test_chinese_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    directory = await test_one_llm_no_search(tmp_path, monkeypatch, None)
    root = tmp_path / "data/evaluation"
    tool_id = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))["history"][
        "tool_run"
    ]
    independent_id = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))[
        "history"
    ]["independent_run"]
    proof = await closure.load_provider_closure(
        repo_root=tmp_path,
        original_run=root / ("provider-" + "0" * 32),
        independent_run=root / independent_id,
        tool_run=root / tool_id,
    )
    path = {
        "response": directory / "research_chinese_v2-response.json",
        "cached": directory / "cached_evidence.json",
        "request": directory / "request-llm-research_chinese_v2.json",
        "grant": approval_path(root, "mock-single-chinese"),
        "score": directory / "cases.json",
        "claim": chinese.chinese_claim(root, proof),
    }[mutation]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "score":
        payload[0]["checks"] = {"fake": True}
    elif mutation == "response":
        payload["text"] = "{}"
    else:
        payload = {}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        chinese.inspect_chinese(directory, proof=proof)


@pytest.mark.parametrize("state", ["pending", "passed", "failed_engineering", "failed_semantic"])
async def test_closure_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    chinese_run = await test_one_llm_no_search(tmp_path, monkeypatch, None)
    root = tmp_path / "data/evaluation"
    binding = json.loads((chinese_run / "manifest.json").read_text(encoding="utf-8"))["history"]
    monkeypatch.setattr(audit, "_git_state", lambda *args: (REVISION, False))
    monkeypatch.setattr(
        audit,
        "inspect_engineering",
        lambda *args, **kwargs: (
            {
                "engineering_acceptance": state != "failed_engineering",
                "passed": 155,
                "planned": 155,
            },
            {},
        ),
    )
    monkeypatch.setattr(
        audit,
        "inspect_semantic",
        lambda *args, **kwargs: ({"semantic_quality_acceptance": state != "failed_semantic"}, {}),
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Audit must never load .env or call providers")

    monkeypatch.setattr(chinese, "load_explicit_settings", forbidden)
    monkeypatch.setattr(chinese, "GuardedTransport", forbidden)
    args = argparse.Namespace(
        original_run=root / binding["original_run"],
        independent_run=root / binding["independent_run"],
        tool_run=root / binding["tool_run"],
        engineering_run=root,
        semantic_run=root,
        chinese_run=None if state == "pending" else chinese_run,
    )
    directory, accepted = await audit.run_closure(args, repo_root=tmp_path)
    assert accepted is (state == "passed")
    summary = json.loads((directory / "closure_acceptance.json").read_text(encoding="utf-8"))
    assert summary["phase10_overall_acceptance"] is False
    assert summary["main_merge_authorized"] is False
    assert summary["provider"]["covered"] == (9 if state == "pending" else 10)
    assert summary["external_requests_in_this_audit"] == 0
    assert summary["cumulative_requests_total"] == (14 if state == "pending" else 15)
    assert summary["cumulative_vendor_tokens"] is None


@pytest.mark.parametrize("mutation", ["sidecar", "cases"])
async def test_pack_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    from autoscholar.evaluation import __main__ as cli
    from autoscholar.evaluation import pack
    from tests import test_evaluation_pack as p

    shutil.copytree(p.SOURCE_ROOT / "benchmarks", tmp_path / "benchmarks")
    h = p.Harness(tmp_path)
    monkeypatch.setattr(cli, "_prepare", h.prepare)
    monkeypatch.setattr(cli, "_evaluate", h.evaluate)
    monkeypatch.setattr(pack, "_git_state", lambda *args: (REVISION, False))
    monkeypatch.setattr("autoscholar.evaluation.runner._git_state", lambda *args: (REVISION, False))
    directory, accepted = await pack.run_pack(p.arguments(), repo_root=tmp_path)
    assert accepted
    baseline, digests = audit.inspect_engineering(directory, repo_root=tmp_path)
    assert baseline["engineering_acceptance"] and len(digests) == 17
    if mutation == "sidecar":
        (directory / "summary.json").write_text(
            '{"engineering_acceptance":false}', encoding="utf-8"
        )
        checked, _ = audit.inspect_engineering(directory, repo_root=tmp_path)
        assert checked == baseline
    else:
        path = directory / baseline["slots"][0]["run"] / "cases.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        with pytest.raises(ValueError):
            audit.inspect_engineering(directory, repo_root=tmp_path)


@pytest.mark.parametrize("mutation", ["sidecar", "identity"])
async def test_neural_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    from autoscholar.evaluation import neural_validation as n
    from tests import test_evaluation_neural as t_neural

    shutil.copytree(t_neural.ROOT / "benchmarks", tmp_path / "benchmarks")
    monkeypatch.setattr(n, "_git_state", lambda *args: (REVISION, False))
    monkeypatch.setattr("autoscholar.evaluation.runner._git_state", lambda *args: (REVISION, False))
    monkeypatch.setattr(n, "DockerNeuralClient", t_neural.FakeInference)
    directory, accepted = await n.run_neural(
        argparse.Namespace(
            output_root=None, image="stub", cache_volume="stub", seed=42, repeats=1, timeout=60
        ),
        repo_root=tmp_path,
    )
    checked, _ = audit.inspect_semantic(directory, repo_root=tmp_path)
    assert checked["semantic_quality_acceptance"] is accepted
    path = directory / (
        "semantic_acceptance.json" if mutation == "sidecar" else "inference_identity.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "sidecar":
        payload["semantic_quality_acceptance"] = not accepted
    else:
        payload["resources"]["dense_model"] = "b" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        audit.inspect_semantic(directory, repo_root=tmp_path)
