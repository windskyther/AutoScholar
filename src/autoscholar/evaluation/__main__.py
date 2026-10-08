"""Offline evaluation entry point. Never autoload application .env/settings."""

import argparse
import asyncio
import json
from pathlib import Path

from autoscholar.evaluation.datasets import load_suite
from autoscholar.evaluation.models import RunConfiguration

REPO_ROOT = Path(__file__).resolve().parents[3]


async def _run(args: argparse.Namespace) -> int:
    from autoscholar.evaluation.rag_adapter import ReplayRAGAdapter, load_ranking_fixture
    from autoscholar.evaluation.runner import run_evaluation

    suite, digest = load_suite(args.dataset)
    if suite.category != "rag":
        raise ValueError("Only RAG adapters are implemented; other categories are not silently run")
    fixture, fixture_digest = load_ranking_fixture(args.fixture)
    if fixture.suite_id != suite.id:
        raise ValueError("Fixture and suite IDs differ")
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
    adapters = [
        ReplayRAGAdapter(fixture, fixture_sha256=fixture_digest, mode=mode, ks=args.ks)
        for mode in args.modes
    ]
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
        "Profile: offline ranking fixtures; external API calls: 0. "
        "These are scorer/engineering results, NOT real retrieval or model performance."
    )
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AutoScholar-Eval (offline by default)")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate a versioned benchmark dataset")
    validate.add_argument("--dataset", type=Path, required=True)
    run = commands.add_parser("run", help="Run public RAG ranking replay (no provider requests)")
    run.add_argument("--dataset", type=Path, default=REPO_ROOT / "benchmarks/rag/public_v1.json")
    run.add_argument(
        "--fixture", type=Path, default=REPO_ROOT / "benchmarks/rag/ranking_fixture_v1.json"
    )
    run.add_argument("--profile", choices=["offline"], default="offline")
    run.add_argument(
        "--modes",
        nargs="+",
        choices=["dense", "sparse", "hybrid", "hybrid_rerank"],
        default=["dense", "sparse", "hybrid", "hybrid_rerank"],
    )
    run.add_argument("--ks", nargs="+", type=int, default=[5, 10])
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
