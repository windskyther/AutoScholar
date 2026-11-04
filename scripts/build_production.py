"""Build local images from a clean commit; does not publish or start services."""

import argparse
import hashlib
import os
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env.production.example")
    parser.add_argument("--development", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    def git(*values: str) -> str:
        return subprocess.run(
            ["git", *values], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()

    if git("status", "--porcelain") and not args.development:
        raise SystemExit(
            "Release builds require a clean checkout; "
            "use --development for an explicitly unsealed build"
        )
    revision = "working-tree" if args.development else git("rev-parse", "HEAD")
    env = {
        **os.environ,
        "AUTOSCHOLAR_REVISION": revision,
        "AUTOSCHOLAR_LOCK_SHA256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
    }
    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            args.env_file,
            "-f",
            "compose.production.yaml",
            "build",
            "api",
            "web",
        ],
        cwd=root,
        env=env,
        check=True,
    )


if __name__ == "__main__":
    main()
