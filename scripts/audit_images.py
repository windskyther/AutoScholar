"""Audit exact local image IDs; retain raw findings and keep unresolved issues blocking."""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from autoscholar.deployment.security_gate import evaluate, findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--api", default="autoscholar-api:prod-local")
    parser.add_argument("--web", default="autoscholar-web:prod-local")
    parser.add_argument("--sandbox", default="autoscholar-python-sandbox:phase11")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = (args.output or root / "data/validation" / ("images-" + uuid4().hex[:12])).resolve()
    output.mkdir(parents=True, exist_ok=False)
    pins = json.loads((root / "security/tools.json").read_text(encoding="utf-8"))
    binary = root / "data/tooling/trivy" / pins["tools"]["trivy"]["version"]
    binary /= "trivy.exe" if sys.platform == "win32" else "trivy"
    if not binary.is_file():
        raise SystemExit("Install the pinned scanner with scripts/install_security_tools.py trivy")
    exceptions = json.loads((root / "security/exceptions.json").read_text(encoding="utf-8"))
    summary: dict[str, Any] = {}
    blocked = False
    for profile, name in (
        ("production-api", args.api),
        ("production-web", args.web),
        ("cpu-sandbox", args.sandbox),
    ):
        metadata = json.loads(
            subprocess.run(
                ["docker", "image", "inspect", name], check=True, capture_output=True, text=True
            ).stdout
        )[0]
        image_id = metadata["Id"]
        report_file = output / (profile + ".json")
        with (output / (profile + ".log")).open("w", encoding="utf-8") as stream:
            subprocess.run(
                [
                    str(binary),
                    "image",
                    "--scanners",
                    "vuln",
                    "--image-src",
                    "docker",
                    "--list-all-pkgs",
                    "--format",
                    "json",
                    "--output",
                    str(report_file),
                    "--cache-dir",
                    str(root / "data/tooling/trivy-cache"),
                    "--db-repository",
                    "ghcr.io/aquasecurity/trivy-db:2",
                    "--no-progress",
                    "--timeout",
                    "15m",
                    image_id,
                ],
                cwd=root,
                check=True,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        report = json.loads(report_file.read_text(encoding="utf-8"))
        if not any(item.get("Packages") for item in report["Results"]):
            raise ValueError("Image package inventory is missing")
        remaining = evaluate(
            "image", report, exceptions, today=datetime.now(UTC).date(), image_profile=profile
        )
        summary[profile] = {
            "image": name,
            "image_id": image_id,
            "raw_blocking": len(findings("image", report)),
            "unresolved": sorted(remaining),
            "report": report_file.name,
        }
        blocked |= bool(remaining)
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "release_allowed": not blocked,
                "output": str(output),
                "unresolved": {name: len(value["unresolved"]) for name, value in summary.items()},
            }
        )
    )
    raise SystemExit(1 if blocked else 0)


if __name__ == "__main__":
    main()
