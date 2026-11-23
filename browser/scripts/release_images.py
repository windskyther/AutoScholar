"""Explicit manual GHCR publication after successful Checks on the same main commit."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path


def validate_version(version: str) -> None:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?", version):
        raise ValueError("Use a release version such as v0.1.0")


def verified_checks(payload: dict[str, object], revision: str) -> bool:
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        return False
    return any(
        isinstance(run, dict)
        and run.get("head_sha") == revision
        and run.get("head_branch") == "main"
        and run.get("conclusion") == "success"
        and run.get("event") in {"push", "workflow_dispatch"}
        and run.get("status") == "completed"
        for run in runs
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--publish", action="store_true", help="Publish; default writes a local plan only"
    )
    args = parser.parse_args()
    validate_version(args.version)
    root = Path(__file__).resolve().parents[1]

    def run(*values: str) -> str:
        return subprocess.run(
            values, cwd=root, check=True, capture_output=True, text=True, encoding="utf-8"
        ).stdout.strip()

    revision = run("git", "rev-parse", "HEAD")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", repository):
        raise ValueError("GITHUB_REPOSITORY is required")
    if run("git", "status", "--porcelain"):
        raise ValueError("Release checkout is dirty")
    if args.publish and (
        os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_SHA") != revision
    ):
        raise ValueError("Publication requires explicit manual dispatch from main")
    images = {}
    for profile, local, suffix in (
        ("production-api", "autoscholar-api:prod-local", "api"),
        ("production-web", "autoscholar-web:prod-local", "web"),
        ("cpu-sandbox", "autoscholar-python-sandbox:phase11", "cpu"),
    ):
        metadata = json.loads(run("docker", "image", "inspect", local))[0]
        if metadata["Config"]["Labels"].get("org.opencontainers.image.revision") != revision:
            raise ValueError("Release image source revision mismatch")
        images[profile] = {
            "local": local,
            "id": metadata["Id"],
            "target": f"ghcr.io/{repository.lower()}-{suffix}:{args.version}",
        }
    # Re-run the audit against the exact local images. Errors and unresolved findings block.
    run(sys.executable, "scripts/audit_images.py")
    manifest = {
        "revision": revision,
        "version": args.version,
        "lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
        "images": images,
        "published": False,
    }
    if args.publish:
        token = os.environ["GH_TOKEN"]
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repository}/actions/workflows/checks.yml/runs?head_sha={revision}&branch=main&status=success&per_page=100",
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if not verified_checks(json.load(response), revision):
                raise ValueError("The exact main revision has no successful Checks run")
        try:
            subprocess.run(
                [
                    "docker",
                    "login",
                    "ghcr.io",
                    "--username",
                    repository.split("/")[0],
                    "--password-stdin",
                ],
                input=token,
                text=True,
                capture_output=True,
                check=True,
            )
            for image in images.values():
                # Bind publication to the exact image IDs validated above.
                if (
                    json.loads(run("docker", "image", "inspect", image["local"]))[0]["Id"]
                    != image["id"]
                ):
                    raise ValueError("Release image changed during validation")
                run("docker", "tag", image["id"], image["target"])
                run("docker", "push", image["target"])
                metadata = json.loads(run("docker", "image", "inspect", image["target"]))[0]
                image["registry_digests"] = metadata["RepoDigests"]
            manifest["published"] = True
        finally:
            run("docker", "logout", "ghcr.io")
    output = root / "data/validation" / ("release-" + args.version + "-" + revision[:12] + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
