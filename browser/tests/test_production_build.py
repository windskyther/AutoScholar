import json
import subprocess
import sys
from typing import Any

import pytest

from scripts import build_production


def test_refresh_reaches_api_web_and_cpu_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        output = ""
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            output = "a" * 40
        elif "config" in command:
            output = json.dumps(
                {
                    "services": {
                        "api": {
                            "build": {
                                "args": {"PYTHON_RUNTIME_IMAGE": "public-runtime@sha256:fixed"}
                            }
                        },
                        "sandbox-manager": {"environment": {"SANDBOX_IMAGE": "public-cpu:fixture"}},
                    }
                }
            )
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        sys, "argv", ["build_production", "--sandbox", "--no-cache", "--pull", "--refresh-system"]
    )
    build_production.main()
    builds = [c for c in calls if c[0] == "docker" and "build" in c]
    assert len(builds) == 2
    assert all("--no-cache" in c and "--pull" in c for c in builds)
    refresh = [[arg for arg in c if arg.startswith("OS_UPDATE_GENERATION=")] for c in builds]
    assert refresh[0] == refresh[1] and refresh[0] != ["OS_UPDATE_GENERATION=cached"]
    assert builds[0][-2:] == ["api", "web"]
    assert "PYTHON_RUNTIME_IMAGE=public-runtime@sha256:fixed" in builds[1]


def test_refresh_cannot_bypass_clean_release_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, " M Dockerfile", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["build_production", "--refresh-system"])
    with pytest.raises(SystemExit, match="clean checkout"):
        build_production.main()
    assert len(calls) == 1 and calls[0][0] == "git"
