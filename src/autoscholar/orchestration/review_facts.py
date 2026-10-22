"""Read-only positive checks for the Reviewer; never a model-selected pass oracle."""

import json
from typing import Any

from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.experiment.analysis import analyze_experiment
from autoscholar.experiment.artifacts import ArtifactManager
from autoscholar.experiment.models import ExperimentSpecification, RawExperimentMetrics
from autoscholar.orchestration.models import ReviewIssue

# Compatibility for already recorded responses. New responses use the typed check field.
_LEGACY_CHECKS = {
    "missing_artifact_confirmation": "artifacts",
    "raw_metrics_schema_risk": "raw_metrics",
    "coding_validation_evidence_missing": "coding_validation",
    "evidence_claim_not_verbatim": "verbatim_citation",
}


def contradicted_issue(issue: ReviewIssue, facts: dict[str, Any]) -> bool:
    check = _LEGACY_CHECKS.get(issue.code.lower(), issue.check)
    return facts.get("checks", {}).get(check) is True


def unsupported_source_issue(issue: ReviewIssue, facts: dict[str, Any]) -> bool:
    """A collection failure is not itself a demonstrated source defect."""
    if issue.check != "source_defect" and issue.code.lower() != "coding_handoff_schema_unverified":
        return False
    excerpt = facts.get("source_excerpts", {}).get(issue.source_path)
    return not (
        isinstance(excerpt, str)
        and issue.source_quote
        and issue.source_quote.strip()
        and issue.source_quote in excerpt
    )


async def step_facts(
    *,
    step_type: str,
    result: dict[str, Any],
    tasks: AgentTaskRepository,
    workspace: WorkspaceManager,
    artifacts: ArtifactManager,
    specification: ExperimentSpecification,
) -> dict[str, Any]:
    """Facts come from durable records and verified bytes, not model claims in result."""
    facts: dict[str, Any] = {"checks": {}, "scope": "local checks; no checkpoint inference"}
    child_id = result["child_task_id"]
    child = await tasks.get_task(child_id)
    if child is None:
        return facts
    if step_type in {"research", "knowledge"}:
        evidence = [
            {"reference": f"{child_id}:{e.citation_key}", "claim_in_excerpt": e.claim in e.excerpt}
            for e in child.evidence
        ]
        facts["citations"] = evidence
        facts["checks"]["verbatim_citation"] = bool(evidence) and all(
            e["claim_in_excerpt"] for e in evidence
        )
    elif step_type == "coding":
        from autoscholar.orchestration.service import source_digest

        try:
            source = workspace.source_snapshot(child_id)
        except ValueError:
            source = {}
        source_bound = bool(source) and source_digest(source) == result.get("source_sha256")
        facts["source_excerpts"] = {key: source[key][:4000] for key in sorted(source)[:8]}
        facts["checks"]["source_handoff"] = source_bound
        traces = []
        for trace in child.tool_calls:
            if trace.tool_name not in {"static_check", "run_pytest"}:
                continue
            try:
                output = json.loads(trace.output)
            except (ValueError, TypeError):
                continue
            if not isinstance(output, dict):
                continue
            traces.append(
                {
                    "tool": trace.tool_name,
                    "status": trace.status,
                    "exit_code": output.get("exit_code"),
                    "result_status": output.get("status"),
                }
            )
        facts["validation_traces"] = traces
        latest = {trace["tool"]: trace for trace in traces}
        facts["checks"]["coding_validation"] = (
            source_bound
            and child.status == "succeeded"
            and all(
                name in latest
                and latest[name]["status"] == "succeeded"
                and latest[name]["result_status"] == "succeeded"
                and latest[name]["exit_code"] == 0
                for name in ("static_check", "run_pytest")
            )
        )
    elif step_type == "experiment" and result.get("status") == "succeeded":
        records = await tasks.list_artifacts(child_id)
        facts["artifact_manifest"] = [
            {"path": a.path, "size_bytes": a.size_bytes, "sha256": a.sha256} for a in records
        ]
        try:
            contents = {a.path: artifacts.read_verified(child_id, a) for a in records}
            facts["checks"]["artifacts"] = len(records) == len(artifacts.allowed) and set(
                contents
            ) == set(artifacts.allowed)
            raw = RawExperimentMetrics.model_validate_json(contents["outputs/raw_metrics.json"])
            summary = json.loads(contents["outputs/metrics.json"])
            facts["checks"]["raw_metrics"] = (
                analyze_experiment(specification, raw).metrics == summary == result.get("metrics")
            )
            facts["raw_metric_keys"] = sorted(raw.model_dump())
            facts["summary_only_keys"] = sorted(set(summary) - set(raw.model_dump()))
        except (KeyError, ValueError):
            facts["checks"].setdefault("artifacts", False)
            facts["checks"]["raw_metrics"] = False
        facts["metrics_role"] = "results.metrics is platform-derived summary, NOT raw JSON"
        # Import locally: service imports this module; use the same canonical digest.
        from autoscholar.orchestration.service import source_digest

        try:
            digest = source_digest(workspace.source_snapshot(child_id))
        except ValueError:
            digest = None
        facts["checks"]["source_handoff"] = bool(
            digest and digest == result.get("source_sha256") == result.get("expected_source_sha256")
        )
    return facts
