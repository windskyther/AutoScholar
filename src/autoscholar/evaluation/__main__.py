"""Offline evaluation entry point. Never autoload application .env/settings."""

import argparse
import asyncio
import json
from pathlib import Path

from autoscholar.evaluation.datasets import load_suite
from autoscholar.evaluation.models import RunConfiguration

REPO_ROOT = Path(__file__).resolve().parents[3]


async def _run(args: argparse.Namespace) -> int:
    from autoscholar.evaluation.component_fixture import load_fixture
    from autoscholar.evaluation.rag_adapter import ReplayRAGAdapter, load_ranking_fixture
    from autoscholar.evaluation.research_adapter import FixtureResearchAdapter, ResearchFixture
    from autoscholar.evaluation.runner import EvaluationAdapter, run_evaluation
    from autoscholar.evaluation.tool_adapter import FixtureToolAdapter, ToolFixture

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
        f"Profile: offline {category} fixtures; external API calls: 0. "
        "These are scorer/local engineering results, NOT real retrieval or model performance. "
        "Expected refusals passing checks are not completed research/numerical tasks."
    )
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AutoScholar-Eval (offline by default)")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate a versioned benchmark dataset")
    validate.add_argument("--dataset", type=Path, required=True)
    run = commands.add_parser("run", help="Run controlled public fixtures (no provider requests)")
    run.add_argument("--category", choices=["rag", "research", "tool"])
    run.add_argument("--dataset", type=Path)
    run.add_argument("--fixture", type=Path)
    run.add_argument("--profile", choices=["offline"], default="offline")
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
    except (OSError, ValueError):
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
