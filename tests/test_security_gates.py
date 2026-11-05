from datetime import date

import pytest

from autoscholar.deployment.security_gate import evaluate, findings
from scripts.check_repository import check_paths


@pytest.mark.parametrize(
    "name", [".env", "data/report.json", "a/.env.local", "设计文档.md", "tls.key"]
)
def test_private_paths_cannot_enter_source_scan(name: str) -> None:
    with pytest.raises(ValueError):
        check_paths(["README.md", name])


@pytest.mark.parametrize("kind", ["python", "npm", "image"])
def test_empty_or_failed_scanner_output_never_passes(kind: str) -> None:
    with pytest.raises((ValueError, KeyError)):
        findings(kind, {})


def test_python_advisory_and_expiring_specific_exception() -> None:
    report = {"dependencies": [{"name": "public-package", "vulns": [{"id": "GHSA-public"}]}]}
    today = date(2026, 11, 5)
    assert evaluate("python", report, [], today=today) == {("public-package", "GHSA-public")}
    exception = {
        "kind": "python",
        "package": "public-package",
        "id": "GHSA-public",
        "owner": "repository-owner",
        "expires": "2026-11-06",
        "reason": "Public test fixture with a concrete, bounded exception rationale.",
    }
    assert not evaluate("python", report, [exception], today=today)
    for changes in (
        {"package": "*"},
        {"expires": "2026-11-04"},
        {"expires": "2027-12-01"},
        {"owner": ""},
    ):
        with pytest.raises(ValueError):
            evaluate("python", report, [{**exception, **changes}], today=today)


def test_container_unfixed_high_finding_still_blocks() -> None:
    report = {
        "SchemaVersion": 2,
        "Metadata": {"OS": "public-linux"},
        "Results": [
            {
                "Class": "os-pkgs",
                "Vulnerabilities": [
                    {
                        "PkgName": "public",
                        "Severity": "HIGH",
                        "VulnerabilityID": "CVE-public",
                        "FixedVersion": "",
                    }
                ],
            }
        ],
    }
    assert findings("image", report) == {("public", "CVE-public")}


def test_npm_transitive_findings_retain_individual_advisory_ids() -> None:
    report = {
        "auditReportVersion": 2,
        "metadata": {"dependencies": {"total": 2}},
        "vulnerabilities": {
            "parent": {"severity": "high", "via": ["child"]},
            "child": {
                "severity": "high",
                "via": [{"severity": "high", "url": "https://github.com/advisories/GHSA-public"}],
            },
        },
    }
    assert findings("npm", report) == {("parent", "GHSA-public"), ("child", "GHSA-public")}


def test_image_review_cannot_waive_another_profile_or_another_installed_version() -> None:
    one = {
        "PkgName": "public-lib",
        "VulnerabilityID": "CVE-public",
        "Severity": "HIGH",
        "InstalledVersion": "1.0",
    }
    vulnerabilities = [one]
    report = {
        "SchemaVersion": 2,
        "Metadata": {"OS": "public"},
        "Results": [{"Class": "os-pkgs", "Vulnerabilities": vulnerabilities}],
    }
    exception = {
        "kind": "image",
        "package": "public-lib",
        "id": "CVE-public",
        "version": "1.0",
        "image_profile": "production-api",
        "owner": "reviewer",
        "source": "https://security-tracker.debian.org/tracker/CVE-public",
        "expires": "2026-11-06",
        "reason": "Public test of a narrowly scoped and version-bound review record.",
    }
    today = date(2026, 11, 5)
    assert not evaluate("image", report, [exception], today=today, image_profile="production-api")
    expected = {("public-lib", "CVE-public")}
    assert (
        evaluate("image", report, [exception], today=today, image_profile="cpu-sandbox") == expected
    )
    assert evaluate("image", report, [exception], today=today) == expected
    vulnerabilities.append({**one, "InstalledVersion": "2.0"})
    assert (
        evaluate("image", report, [exception], today=today, image_profile="production-api")
        == expected
    )
    for field in ("version", "image_profile", "source"):
        with pytest.raises(ValueError):
            evaluate("image", report, [{**exception, field: ""}], today=today)
