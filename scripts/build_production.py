"""Build local images from a clean commit; does not publish or start services."""

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env.production.example")
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--sandbox", action="store_true", help="Also build the locked CPU image")
    parser.add_argument("--no-cache", action="store_true", help="Rebuild all image layers")
    parser.add_argument("--pull", action="store_true", help="Refresh configured base images")
    parser.add_argument(
        "--refresh-system",
        action="store_true",
        help="Refresh signed OS updates without discarding dependency caches",
    )
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
    flags = (["--no-cache"] if args.no_cache else []) + (["--pull"] if args.pull else [])
    refresh = datetime.now(UTC).isoformat() if args.refresh_system else "cached"
    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            args.env_file,
            "-f",
            "compose.production.yaml",
            "build",
            *flags,
            "--build-arg",
            "OS_UPDATE_GENERATION=" + refresh,
            "api",
            "web",
        ],
        cwd=root,
        env=env,
        check=True,
    )
    if args.sandbox:
        config = json.loads(
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    args.env_file,
                    "-f",
                    "compose.production.yaml",
                    "config",
                    "--format",
                    "json",
                ],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        subprocess.run(
            [
                "docker",
                "build",
                *flags,
                "--platform",
                "linux/amd64",
                "--build-arg",
                "SOURCE_REVISION=" + revision,
                "--build-arg",
                "OS_UPDATE_GENERATION=" + refresh,
                "--build-arg",
                "PYTHON_RUNTIME_IMAGE="
                + config["services"]["api"]["build"]["args"]["PYTHON_RUNTIME_IMAGE"],
                "-f",
                "docker/sandbox/Dockerfile",
                "-t",
                config["services"]["sandbox-manager"]["environment"]["SANDBOX_IMAGE"],
                ".",
            ],
            cwd=root,
            env=env,
            check=True,
        )


if __name__ == "__main__":
    main()
