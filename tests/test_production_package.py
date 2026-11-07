import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from autoscholar.core.config import Settings
from autoscholar.deployment import worker_health
from autoscholar.deployment.manifest import build_manifest
from autoscholar.deployment.volumes import initialize
from scripts.configure_production import initialize as initialize_secrets


def test_connection_files_load_before_production_validation(tmp_path: Path) -> None:
    database, redis = tmp_path / "database", tmp_path / "redis"
    database.write_text(
        "postgresql+asyncpg://user:strong-public-123456789@postgres/app\n", encoding="utf-8"
    )
    redis.write_text("redis://:public-redis-password@redis:6379/0\n", encoding="utf-8")
    value = Settings(database_url_file=database, redis_url_file=redis)
    assert value.database_url.endswith("@postgres/app") and value.redis_url.endswith(
        "@redis:6379/0"
    )
    assert "strong-public-123456789" not in repr(value)
    with pytest.raises(ValidationError):
        Settings(database_url_file=database, database_url="sqlite:///explicit")


@pytest.mark.parametrize("content", [b"", b"invalid whitespace", b"x" * 8193, b"\xff"])
def test_invalid_connection_file_is_bounded_and_sanitized(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "connection"
    path.write_bytes(content)
    with pytest.raises(ValidationError) as exc:
        Settings(database_url_file=path)
    assert "readable bounded connection file" in str(exc.value)
    assert "invalid whitespace" not in str(exc.value)


def test_manifest_binds_lock_and_unique_migration_head(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("public dependency lock", encoding="utf-8")
    migrations = tmp_path / "migrations/versions"
    migrations.mkdir(parents=True)
    (migrations / "one.py").write_text("revision: str = 'one'\ndown_revision: str | None = None\n")
    digest = hashlib.sha256((tmp_path / "uv.lock").read_bytes()).hexdigest()
    assert build_manifest(tmp_path, "a" * 40, digest)["schema_revision"] == "one"
    assert build_manifest(tmp_path, "working-tree", digest)["release_source"] is False
    with pytest.raises(ValueError):
        build_manifest(tmp_path, "a" * 40, "b" * 64)
    (migrations / "two.py").write_text("revision: str = 'two'\ndown_revision: str | None = None\n")
    with pytest.raises(ValueError):
        build_manifest(tmp_path, "a" * 40, digest)


def test_fresh_secret_setup_never_overwrites_existing_files(tmp_path: Path) -> None:
    target = tmp_path / "secrets"
    initialize_secrets(target)
    first = {path.name: path.read_bytes() for path in target.iterdir()}
    assert first["model_encryption_key"] != first["sandbox_token"]
    assert first["postgres_password"].strip() in first["database_url"]
    assert (
        json.loads(first["qdrant_config"])["service"]["api_key"]
        == first["qdrant_key"].decode().strip()
    )
    with pytest.raises(ValueError):
        initialize_secrets(target)
    assert first == {path.name: path.read_bytes() for path in target.iterdir()}


@pytest.mark.skipif(sys.platform == "win32", reason="Linux volume ownership runs in container")
def test_volume_initializer_refuses_legacy_content_and_is_idempotent(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("Linux ownership check")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    content = legacy / "private-existing-data"
    content.write_bytes(b"unchanged")
    with pytest.raises(ValueError):
        initialize(legacy, uid=os.getuid(), gid=os.getgid())
    assert content.read_bytes() == b"unchanged"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    initialize(fresh, uid=os.getuid(), gid=os.getgid())
    (fresh / "retained").write_bytes(b"retained")
    initialize(fresh, uid=os.getuid(), gid=os.getgid())
    assert (fresh / "retained").read_bytes() == b"retained"


async def test_worker_heartbeat_lifespan_removes_its_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(worker_health, "PATH", path)
    async with worker_health.heartbeat():
        await asyncio.sleep(0)
        assert json.loads(path.read_text())["pid"] == os.getpid()
    assert not path.exists()


def test_worker_probe_cannot_signal_a_process_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("must not signal Windows processes"))
    with pytest.raises(SystemExit):
        worker_health.main()


@pytest.mark.parametrize("age", [-1, 121])
def test_worker_probe_rejects_stale_or_future_heartbeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, age: int
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps({"pid": os.getpid(), "at": time.time() - age}))
    monkeypatch.setattr(worker_health, "PATH", path)
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("invalid timestamps cannot pass"))
    with pytest.raises(SystemExit):
        worker_health.main()
