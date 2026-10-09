"""Bounded evaluation entry point. Never autoload application .env/settings."""

import argparse
import asyncio
import json
from pathlib import Path

from autoscholar.coding.sandbox import SandboxError
from autoscholar.evaluation.datasets import load_suite
from autoscholar.evaluation.models import RunConfiguration

REPO_ROOT = Path(__file__).resolve().parents[3]


async def _run(args: argparse.Namespace) -> int:
    from autoscholar.evaluation.coding_adapter import (
        CodingFixture,
        CodingOracleFixture,
        InjectedCodingAdapter,
    )
    from autoscholar.evaluation.component_fixture import load_fixture
    from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
    from autoscholar.evaluation.e2e_adapter import InjectedWorkflowAdapter
    from autoscholar.evaluation.experiment_adapter import (
        ExperimentFixture,
        InjectedExperimentAdapter,
    )
    from autoscholar.evaluation.isolated_components import sandbox_resources
    from autoscholar.evaluation.rag_adapter import ReplayRAGAdapter, load_ranking_fixture
    from autoscholar.evaluation.research_adapter import FixtureResearchAdapter, ResearchFixture
    from autoscholar.evaluation.runner import EvaluationAdapter, run_evaluation
    from autoscholar.evaluation.tool_adapter import FixtureToolAdapter, ToolFixture
    from autoscholar.evaluation.workflow_fixture import WorkflowFixture

    category = args.category or "rag"
    dataset = args.dataset or REPO_ROOT / f"benchmarks/{category}/public_v1.json"
    suite, digest = load_suite(dataset)
    if args.category and suite.category != args.category:
        raise ValueError("Dataset category differs from requested category")
    category = suite.category
    fixture_name = {
        "rag": "ranking_fixture_v1.json",
        "research": "provider_fixture_v1.json",
        "tool": "call_fixture_v1.json",
        "coding": "source_fixture_v1.json",
        "experiment": "experiment_fixture_v1.json",
        "end_to_end": "workflow_fixture_v1.json",
    }.get(category)
    if fixture_name is None:
        raise ValueError("Evaluation category is not implemented")
    fixture_path = args.fixture or REPO_ROOT / f"benchmarks/{category}/{fixture_name}"
    root = (REPO_ROOT / "data/evaluation").resolve()
    destination = args.output_root.resolve() if args.output_root else root
    if not root.is_relative_to(REPO_ROOT.resolve()) or not destination.is_relative_to(root):
        raise ValueError("Output must stay in this repository's data/evaluation directory")
    configuration = RunConfiguration(
        profile=args.profile,
        seed=args.seed,
        repeats=args.repeats,
        case_timeout_seconds=args.timeout,
    )
    adapters: list[EvaluationAdapter]
    if category in ("coding", "experiment", "end_to_end"):
        if args.profile != "injected" or not args.sandbox_container:
            raise ValueError(
                "Coding/Experiment/E2E require explicit injected Docker controller; "
                "no host fallback"
            )
        if args.ks is not None or args.modes not in (None, ["baseline"]):
            raise ValueError("Isolated component evaluations use baseline without K")
    elif args.profile != "offline" or args.sandbox_container or args.oracle:
        raise ValueError("Only Coding/Experiment/E2E expose sandbox injection")
    if category == "rag":
        fixture, fixture_digest = load_ranking_fixture(fixture_path)
        modes = args.modes or ["dense", "sparse", "hybrid", "hybrid_rerank"]
        adapters = [
            ReplayRAGAdapter(
                fixture, fixture_sha256=fixture_digest, mode=mode, ks=args.ks or [5, 10]
            )
            for mode in modes
        ]
        fixture_suite = fixture.suite_id
    elif category == "coding":
        coding, fixture_digest = load_fixture(fixture_path, CodingFixture)
        oracle_path = args.oracle or REPO_ROOT / "benchmarks/coding/oracle_fixture_v1.json"
        oracle, oracle_digest = load_fixture(oracle_path, CodingOracleFixture)
        sandbox = DockerEvaluationSandbox(args.sandbox_container)
        resources = sandbox_resources(await sandbox.health(), dataset=False)
        workspace_root = (root / "component-workspaces").resolve()
        if not workspace_root.is_relative_to(root):
            raise ValueError("Component workspace escaped evaluation output scope")
        adapters = [
            InjectedCodingAdapter(
                coding,
                oracle,
                fixture_sha256=fixture_digest,
                oracle_sha256=oracle_digest,
                sandbox=sandbox,
                resources=resources,
                workspace_root=workspace_root,
            )
        ]
        fixture_suite = coding.suite_id
    elif category == "experiment":
        if args.oracle:
            raise ValueError(
                "Experiment uses a fixed checkpoint oracle, not arbitrary oracle files"
            )
        experiment, fixture_digest = load_fixture(fixture_path, ExperimentFixture)
        sandbox = DockerEvaluationSandbox(args.sandbox_container)
        resources = sandbox_resources(await sandbox.health(), dataset=True)
        workspace_root = (root / "component-workspaces").resolve()
        if not workspace_root.is_relative_to(root):
            raise ValueError("Component workspace escaped evaluation output scope")
        adapters = [
            InjectedExperimentAdapter(
                experiment,
                fixture_sha256=fixture_digest,
                sandbox=sandbox,
                resources=resources,
                workspace_root=workspace_root,
            )
        ]
        fixture_suite = experiment.suite_id
    elif category == "end_to_end":
        if args.oracle:
            raise ValueError("E2E uses a fixed checkpoint oracle, not arbitrary oracle files")
        workflow, fixture_digest = load_fixture(fixture_path, WorkflowFixture)
        sandbox = DockerEvaluationSandbox(args.sandbox_container)
        resources = sandbox_resources(await sandbox.health(), dataset=True)
        workspace_root = (root / "workflow-workspaces").resolve()
        if not workspace_root.is_relative_to(root):
            raise ValueError("Workflow workspace escaped evaluation output scope")
        adapters = [
            InjectedWorkflowAdapter(
                workflow,
                fixture_sha256=fixture_digest,
                sandbox=sandbox,
                resources=resources,
                workspace_root=workspace_root,
            )
        ]
        fixture_suite = workflow.suite_id
    else:
        if args.ks is not None or args.modes not in (None, ["baseline"]):
            raise ValueError("Research/Tool only support baseline; K is a RAG option")
        if category == "research":
            research, fixture_digest = load_fixture(fixture_path, ResearchFixture)
            adapters = [FixtureResearchAdapter(research, fixture_sha256=fixture_digest)]
            fixture_suite = research.suite_id
        else:
            tool, fixture_digest = load_fixture(fixture_path, ToolFixture)
            adapters = [FixtureToolAdapter(tool, fixture_sha256=fixture_digest)]
            fixture_suite = tool.suite_id
    if fixture_suite != suite.id:
        raise ValueError("Fixture and suite IDs differ")
    directory = await run_evaluation(
        suite,
        dataset_sha256=digest,
        adapters=adapters,
        configuration=configuration,
        output_root=destination,
        repo_root=REPO_ROOT,
    )
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    failed = any(
        group["status_counts"].get(status, 0)
        for group in summary["groups"]
        for status in ("failed", "error", "timeout")
    )
    print(f"Evaluation {'checks failed' if failed else 'checks passed'}: {directory}")
    print(
        f"Profile: {args.profile} {category}; external API calls: 0. "
        "Scripted responses do NOT measure real LLM ability; RAG replay is NOT real retrieval. "
        "Expected refusals passing checks are not completed research/numerical tasks."
    )
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AutoScholar-Eval (offline by default)")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate a versioned benchmark dataset")
    validate.add_argument("--dataset", type=Path, required=True)
    run = commands.add_parser("run", help="Run controlled public fixtures (no provider requests)")
    run.add_argument(
        "--category", choices=["rag", "research", "tool", "coding", "experiment", "end_to_end"]
    )
    run.add_argument("--dataset", type=Path)
    run.add_argument("--fixture", type=Path)
    run.add_argument("--profile", choices=["offline", "injected"], default="offline")
    run.add_argument("--sandbox-container", help="Explicit autoscholar-eval-* dedicated controller")
    run.add_argument("--oracle", type=Path, help="Separate held-out Coding oracle fixture")
    run.add_argument(
        "--modes",
        nargs="+",
        choices=["dense", "sparse", "hybrid", "hybrid_rerank", "baseline"],
    )
    run.add_argument("--ks", nargs="+", type=int)
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--timeout", type=float, default=30.0)
    run.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "run":
            return asyncio.run(_run(args))
        suite, digest = load_suite(args.dataset)
    except (OSError, ValueError, SandboxError):
        print(
            "Evaluation configuration/IO invalid. Check schema, case/query bindings, "
            "supported categories, K values and data/evaluation output scope."
        )
        return 2
    except KeyboardInterrupt:
        print("Evaluation cancelled; any started run retains partial records in data/evaluation.")
        return 130
    print(
        f"Valid suite: {suite.id}; category: {suite.category}; cases: {len(suite.cases)}; "
        f"sha256: {digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
