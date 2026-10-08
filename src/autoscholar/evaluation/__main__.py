"""Offline evaluation entry point. Never autoload application .env/settings."""

import argparse
from pathlib import Path

from autoscholar.evaluation.datasets import load_suite


def main() -> int:
    parser = argparse.ArgumentParser(description="AutoScholar-Eval (offline by default)")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate a versioned benchmark dataset")
    validate.add_argument("--dataset", type=Path, required=True)
    args = parser.parse_args()
    try:
        suite, digest = load_suite(args.dataset)
    except (OSError, ValueError):
        print("Benchmark validation failed; check UTF-8 JSON, schema, IDs and file size.")
        return 2
    print(
        f"Valid suite: {suite.id}; category: {suite.category}; cases: {len(suite.cases)}; "
        f"sha256: {digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
