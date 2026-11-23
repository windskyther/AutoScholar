import json
from pathlib import Path

import pytest

from autoscholar.deployment.operations import Operations


def test_backup_refuses_a_running_writer_before_creating_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = Operations.__new__(Operations)
    monkeypatch.setattr(
        operation,
        "containers",
        lambda: [
            {
                "Config": {"Labels": {"com.docker.compose.service": "workflow-worker"}},
                "State": {"Running": True},
            }
        ],
    )
    destination = tmp_path / "backup"
    with pytest.raises(ValueError, match="maintenance"):
        operation.backup(destination)
    assert not destination.exists()


@pytest.mark.parametrize("schema,pending", [("old", 0), ("current", 1)])
def test_incompatible_or_unknown_switch_never_starts_workers(
    monkeypatch: pytest.MonkeyPatch, schema: str, pending: int
) -> None:
    operation = Operations.__new__(Operations)
    operation.image = "public-fixture"
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(operation, "quiescent", lambda: None)
    monkeypatch.setattr(
        operation,
        "database",
        lambda: {"schema": schema, "pending_calls": pending, "unresolved_invocations": 0},
    )

    def run(*args: str) -> str:
        calls.append(args)
        return json.dumps({"schema_revision": "current"})

    monkeypatch.setattr(operation, "run", run)
    monkeypatch.setattr(operation, "compose", lambda *args: pytest.fail("Must not start workers"))
    with pytest.raises(ValueError):
        operation.switch(resume=True)
    assert len(calls) == 1


def test_restore_refuses_an_existing_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from autoscholar.deployment import operations

    operation = Operations.__new__(Operations)
    monkeypatch.setattr(operations, "verify_backup", lambda _: {})
    monkeypatch.setattr(operation, "containers", lambda: [{"existing": True}])
    monkeypatch.setattr(
        operation, "run", lambda *args: pytest.fail("Must not mutate existing project")
    )
    with pytest.raises(ValueError, match="fresh project"):
        operation.restore(tmp_path)
