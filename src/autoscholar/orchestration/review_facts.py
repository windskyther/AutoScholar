"""Read-only positive checks for the Reviewer; never a model-selected pass oracle."""

import json
from typing import Any

from sqlalchemy import select

from autoscholar.agent.records import AgentTaskRecord, EvidenceRecord
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.experiment.analysis import analyze_experiment
from autoscholar.experiment.artifacts import ArtifactManager
from autoscholar.experiment.models import ExperimentSpecification, RawExperimentMetrics
from autoscholar.orchestration.contracts import REQUIREMENTS
from autoscholar.orchestration.models import ReviewIssue
from autoscholar.rag.database_models import DocumentChunkRow


async def source_quote_checks(
    child: AgentTaskRecord, tasks: AgentTaskRepository
) -> list[dict[str, Any]]:
    """Bind quotations to persisted search receipts or same-project PDF chunks.

    A claim can paraphrase. Verbatim checks apply to quoted source text, never to
    the wording of the claim. Semantic support remains the Reviewer's obligation.
    """
    sources: list[dict[str, Any]] = []
    for trace in child.tool_calls:
        if trace.status != "succeeded" or trace.tool_name not in {"web_search", "paper_search"}:
            continue
        try:
            payload = json.loads(trace.output)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
            sources.extend(
                item
                for item in payload["results"]
                if isinstance(item, dict) and trace.tool_name == f"{item.get('source_type')}_search"
            )
    chunk_ids = {e.chunk_id for e in child.evidence if e.source_type == "document" and e.chunk_id}
    chunks = {}
    if chunk_ids:
        async with tasks.session_factory() as session:
            chunks = {
                row.id: row
                for row in await session.scalars(
                    select(DocumentChunkRow).where(
                        DocumentChunkRow.id.in_(chunk_ids),
                        DocumentChunkRow.project_id == child.project_id,
                    )
                )
            }

    def verified(evidence: EvidenceRecord) -> bool:
        if not evidence.excerpt.strip():
            return False
        if evidence.source_type == "document":
            chunk = chunks.get(evidence.chunk_id or "")
            return bool(
                chunk
                and chunk.document_id == evidence.document_id
                and chunk.page == evidence.page
                and evidence.excerpt in chunk.content
                and evidence.url
                == (
                    f"/projects/{child.project_id}/documents/{chunk.document_id}/content#page={chunk.page}"
                )
            )
        return any(
            item.get("source_type") == evidence.source_type
            and item.get("provider") == evidence.provider
            and item.get("url") == evidence.url
            and isinstance(item.get("content"), str)
            and evidence.excerpt in item["content"]
            for item in sources
        )

    return [
        {
            "reference": f"{child.id}:{e.citation_key}",
            "excerpt_in_source": verified(e),
        }
        for e in child.evidence
    ]


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


def requirement_issue_error(
    issue: ReviewIssue, facts: dict[str, Any], objective: str
) -> str | None:
    """Validate owner and evidence independently of the model's descriptive code/label."""
    kind = facts.get("step_type")
    if issue.requirement_id is not None:
        requirement = REQUIREMENTS.get(issue.requirement_id)
        if requirement is None or requirement[0] != kind:
            return "Requirement ID must belong to this step's authoritative stage contract"
        if requirement[1] and facts.get("checks", {}).get(requirement[1]) is True:
            return "Review requirement contradicts verified facts; the referenced check passed"
    if kind != "coding" or facts.get("checks", {}).get("coding_validation") is not True:
        return None
    if issue.check in {"artifacts", "raw_metrics", "verbatim_citation"}:
        return "Collected artifacts/metrics and citations are not coding-stage deliverables"
    # Reclassifying a source claim as 'requirement' must not bypass source evidence.
    excerpt = facts.get("source_excerpts", {}).get(issue.source_path)
    if not (
        isinstance(excerpt, str)
        and issue.source_quote
        and issue.source_quote.strip()
        and issue.source_quote in excerpt
    ):
        return (
            "A coding requirement/source defect requires source_path and an exact nonblank "
            "source_quote from verified_checks.source_excerpts"
        )
    quote = issue.requirement_quote
    objective_bound = bool(
        quote
        and quote.strip()
        and len(quote.strip()) >= min(12, len(objective.strip()))
        and quote in objective
    )
    if issue.requirement_id is None and not objective_bound:
        return (
            "A coding issue must cite a stage requirement_id or an exact requirement_quote "
            "from the objective; Planner expected_output cannot introduce extra obligations"
        )
    return None


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
    facts: dict[str, Any] = {
        "step_type": step_type,
        "checks": {},
        "scope": "local checks; no checkpoint inference",
    }
    child_id = result["child_task_id"]
    child = await tasks.get_task(child_id)
    if child is None:
        return facts
    if step_type in {"research", "knowledge"}:
        evidence = await source_quote_checks(child, tasks)
        facts["citations"] = evidence
        facts["checks"]["verbatim_citation"] = bool(evidence) and all(
            e["excerpt_in_source"] for e in evidence
        )
        facts["citation_policy"] = (
            "verbatim_citation checks excerpts against persisted sources, not claim wording. "
            "Claims may paraphrase; assess semantic support separately."
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
        for tool, check in (("static_check", "static_check"), ("run_pytest", "pytest")):
            validation_trace = latest.get(tool, {})
            facts["checks"][check] = (
                validation_trace.get("status") == "succeeded"
                and validation_trace.get("result_status") == "succeeded"
                and validation_trace.get("exit_code") == 0
            )
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
