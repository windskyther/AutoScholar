"""Fail-closed parsers for public dependency and container audit reports."""

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any


def findings(kind: str, report: dict[str, Any]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    if kind == "python":
        dependencies = report["dependencies"]
        if not isinstance(dependencies, list) or not dependencies:
            raise ValueError("Python audit has no dependency inventory")
        for dependency in dependencies:
            if "skip_reason" in dependency:
                if "environment marker" not in dependency["skip_reason"]:
                    raise ValueError("Python dependency was not audited")
                continue
            for vulnerability in dependency["vulns"]:
                result.add((dependency["name"], vulnerability["id"]))
    elif kind == "npm":
        if report.get("error") or report["auditReportVersion"] != 2:
            raise ValueError("npm audit failed")
        if report["metadata"]["dependencies"]["total"] < 1:
            raise ValueError("npm audit has no dependency inventory")

        def advisory_ids(name: str, seen: set[str]) -> set[str]:
            if name in seen:
                return set()
            ids: set[str] = set()
            for value in report["vulnerabilities"][name]["via"]:
                if isinstance(value, str):
                    ids.update(advisory_ids(value, seen | {name}))
                elif value["severity"] in {"high", "critical"}:
                    identifier = value["url"].rsplit("/", 1)[-1]
                    if not identifier.startswith("GHSA-"):
                        raise ValueError("npm advisory ID missing")
                    ids.add(identifier)
            return ids

        for name, entry in report["vulnerabilities"].items():
            if entry["severity"] not in {"info", "low", "moderate", "high", "critical"}:
                raise ValueError("Unknown npm severity")
            if entry["severity"] in {"high", "critical"}:
                ids = advisory_ids(name, set())
                if not ids:
                    raise ValueError("High npm finding has no auditable advisory")
                result.update((name, identifier) for identifier in ids)
    elif kind == "image":
        if report["SchemaVersion"] != 2 or not report["Metadata"] or not report["Results"]:
            raise ValueError("Container inventory missing")
        if not any(entry.get("Class") == "os-pkgs" for entry in report["Results"]):
            raise ValueError("Container OS was not audited")
        for entry in report["Results"]:
            for vulnerability in entry.get("Vulnerabilities", []):
                severity = vulnerability["Severity"]
                if severity not in {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}:
                    raise ValueError("Unknown image severity")
                if severity in {"HIGH", "CRITICAL", "UNKNOWN"}:
                    result.add((vulnerability["PkgName"], vulnerability["VulnerabilityID"]))
    else:
        raise ValueError("Unknown audit format")
    return result


def evaluate(
    kind: str,
    report: dict[str, Any],
    exceptions: list[dict[str, str]],
    *,
    today: date,
    image_profile: str | None = None,
) -> set[tuple[str, str]]:
    accepted: set[tuple[str, str]] = set()
    for entry in exceptions:
        expiry = date.fromisoformat(entry["expires"])
        if (
            expiry < today
            or expiry > today + timedelta(days=90)
            or len(entry["reason"].strip()) < 40
            or not entry["owner"].strip()
            or entry["id"] == "*"
            or entry["package"] == "*"
        ):
            raise ValueError(
                "Audit exception requires bounded expiry, owner and concrete rationale"
            )
        if entry["kind"] == "image" and (
            not entry.get("image_profile")
            or not entry.get("version")
            or "*" in entry["image_profile"]
            or "*" in entry["version"]
            or not entry.get("source", "").startswith("https://")
        ):
            raise ValueError("Image exception requires an exact profile, version and source")
        if entry["kind"] == kind and kind != "image":
            accepted.add((entry["package"], entry["id"]))
    if kind == "image":
        # Validate the entire report first. Review of one image/version must not waive
        # another image or a second installed version with the same package/advisory.
        raw = findings(kind, report)
        blocked: set[tuple[str, str]] = set()
        for result in report["Results"]:
            for value in result.get("Vulnerabilities", []):
                key = (value["PkgName"], value["VulnerabilityID"])
                if key not in raw:
                    continue
                waived = any(
                    entry["kind"] == "image"
                    and image_profile is not None
                    and entry["image_profile"] == image_profile
                    and entry["package"] == key[0]
                    and entry["id"] == key[1]
                    and entry["version"] == value.get("InstalledVersion")
                    for entry in exceptions
                )
                if not waived:
                    blocked.add(key)
        return blocked
    return findings(kind, report) - accepted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["python", "npm", "image"])
    parser.add_argument("report", type=Path)
    parser.add_argument("--exceptions", type=Path, default=Path("security/exceptions.json"))
    parser.add_argument("--image-profile")
    args = parser.parse_args()
    blocked = evaluate(
        args.kind,
        json.loads(args.report.read_text(encoding="utf-8-sig")),
        json.loads(args.exceptions.read_text(encoding="utf-8")),
        today=datetime.now(UTC).date(),
        image_profile=args.image_profile,
    )
    print(json.dumps({"kind": args.kind, "blocked": sorted(blocked)}))
    raise SystemExit(1 if blocked else 0)


if __name__ == "__main__":
    main()
