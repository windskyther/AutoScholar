from pathlib import Path
from typing import Any

import pytest

from scripts.verify_cpu import CPUAcceptance


def fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, alien: bool = False
) -> tuple[CPUAcceptance, list[tuple[str, ...]]]:
    instance = CPUAcceptance.__new__(CPUAcceptance)
    instance.owner = "phase11cpu-public"
    instance.output = tmp_path
    instance.volumes = ["phase11cpu-public-dataset"]
    instance.containers = []
    instance.report = {"accepted": False}
    calls: list[tuple[str, ...]] = []

    def run(*args: str, **kwargs: Any) -> str:
        calls.append(args)
        # Include an interrupted --rm downloader, which was never added to the
        # controller list: it still must be cleaned before its dataset volume.
        return "interrupted-downloader" if args[:3] == ("docker", "ps", "-aq") else ""

    def inspect(kind: str, name: str) -> dict[str, Any]:
        labels = {"autoscholar.acceptance": "another-owner" if alien else instance.owner}
        return {"Config": {"Labels": labels}} if kind == "container" else {"Labels": labels}

    monkeypatch.setattr(instance, "run", run)
    monkeypatch.setattr(instance, "inspect", inspect)
    return instance, calls


def test_interrupted_downloader_is_removed_before_owned_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, calls = fixture(tmp_path, monkeypatch)
    instance.close()
    container = next(i for i, call in enumerate(calls) if call[:3] == ("docker", "rm", "--force"))
    volume = next(i for i, call in enumerate(calls) if call[:3] == ("docker", "volume", "rm"))
    assert container < volume
    assert instance.report["resources_removed"]


def test_cpu_cleanup_refuses_changed_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, calls = fixture(tmp_path, monkeypatch, alien=True)
    with pytest.raises(AssertionError):
        instance.close()
    assert not any("rm" in call for call in calls)
