"""Scan only Git-tracked source; reject private-path mistakes before running Gitleaks."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4


def check_paths(paths: list[str]) -> None:
    for value in paths:
        path = Path(value)
        if (
            path.parts[0] in {"data", ".aws", ".agents", ".codex"}
            or (
                path.name.startswith(".env")
                and value not in {".env.example", ".env.production.example"}
            )
            or "设计" in value
            or path.suffix.lower() in {".pem", ".key"}
        ):
            raise ValueError("Private or runtime path is tracked: " + value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gitleaks", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    paths = (
        subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
        .stdout.decode()
        .rstrip("\0")
        .split("\0")
    )
    check_paths(paths)
    (root / "data").mkdir(exist_ok=True)
    reports = root / "data/validation"
    reports.mkdir(exist_ok=True)
    report = reports / ("source-scan-" + uuid4().hex + ".json")
    # Use the checked-out tracked bytes (including local edits), without copying a local .env/data.
    with tempfile.TemporaryDirectory(prefix="source-scan-", dir=root / "data") as temporary:
        target = Path(temporary)
        for value in paths:
            source, destination = root / value, target / value
            if source.is_symlink():
                raise ValueError("Tracked scanner input cannot be a symlink")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
        scan = subprocess.run(
            [
                str(Path(args.gitleaks).resolve()),
                "dir",
                "--redact=100",
                "--no-banner",
                "--report-format",
                "json",
                "--report-path",
                str(report),
                str(target),
            ],
            cwd=root,
        )
        if scan.returncode:
            if report.exists():
                for match in json.loads(report.read_text(encoding="utf-8")):
                    print(
                        json.dumps(
                            {
                                "rule": match["RuleID"],
                                "file": match["File"],
                                "line": match["StartLine"],
                            }
                        )
                    )
            raise SystemExit(scan.returncode)


if __name__ == "__main__":
    main()
