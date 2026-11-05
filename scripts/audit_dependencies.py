"""Audit locked Python/npm dependencies; reports stay under ignored data/validation."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from autoscholar.deployment.security_gate import evaluate


def main() -> None:
    from datetime import UTC, datetime

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output or root / "data/validation" / ("audit-" + uuid4().hex[:12])
    output.mkdir(parents=True, exist_ok=False)
    environment = {**os.environ, "UV_CACHE_DIR": str(root / ".uv-cache")}
    requirements = output / "requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--all-groups",
            "--no-emit-project",
            "--quiet",
            "--output-file",
            str(requirements),
        ],
        cwd=root,
        env=environment,
        check=True,
    )
    python_report = output / "python.json"
    audit = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip_audit",
            "--no-deps",
            "--disable-pip",
            "-r",
            str(requirements),
            "-f",
            "json",
            "-o",
            str(python_report),
        ],
        cwd=root,
        env=environment,
    )
    if audit.returncode not in {0, 1} or not python_report.exists():
        raise RuntimeError("Python audit did not complete")
    npm = subprocess.run(
        [
            "npm.cmd" if sys.platform == "win32" else "npm",
            "audit",
            "--json",
            "--registry=https://registry.npmjs.org",
        ],
        cwd=root / "web",
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    npm_report = json.loads(npm.stdout)
    (output / "npm.json").write_text(json.dumps(npm_report), encoding="utf-8")
    if npm.returncode not in {0, 1}:
        raise RuntimeError("npm audit did not complete")
    exceptions = json.loads((root / "security/exceptions.json").read_text(encoding="utf-8"))
    blocked = {
        kind: sorted(evaluate(kind, report, exceptions, today=datetime.now(UTC).date()))
        for kind, report in [
            ("python", json.loads(python_report.read_text(encoding="utf-8"))), ("npm", npm_report)
        ]
    }
    (output / "summary.json").write_text(json.dumps(blocked, indent=2), encoding="utf-8")
    print(json.dumps({"blocked": blocked, "output": str(output)}))
    raise SystemExit(1 if any(blocked.values()) else 0)


if __name__ == "__main__":
    main()
