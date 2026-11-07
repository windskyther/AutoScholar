import asyncio
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from autoscholar.coding.sandbox import (
    DockerSandboxExecutor,
    SandboxClient,
    SandboxError,
    SandboxRunRequest,
)
from autoscholar.core.config import Settings
from autoscholar.sandbox.access import ManagerSettings
from autoscholar.sandbox.manager import create_manager_app
from tests.sandbox_credentials import AUTH, TOKEN, TOKEN_SECRET
from tests.test_sandbox import FakeSandbox

PAYLOAD = {"task_id": "public", "action": "run_pytest", "files": {}}


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer bad"}, [("Authorization", "Bearer " + TOKEN)] * 2]
)
def test_unauthorized_manager_requests_never_reach_executor(headers: Any) -> None:
    fake = FakeSandbox()
    app = create_manager_app(fake, settings=ManagerSettings(sandbox_manager_token=TOKEN_SECRET))
    with TestClient(app) as api:
        assert api.get("/health/live").status_code == 200
        assert api.get("/internal/health", headers=headers).status_code == 401
        assert (
            api.post("/internal/v1/run", content=b"invalid JSON", headers=headers).status_code
            == 401
        )
    assert fake.request is None


async def test_denied_upload_body_is_not_consumed() -> None:
    app = create_manager_app(
        FakeSandbox(), settings=ManagerSettings(sandbox_manager_token=TOKEN_SECRET)
    )
    consumed = []

    async def body() -> Any:
        consumed.append(True)
        yield b"private-body"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://manager"
    ) as api:
        assert (await api.post("/internal/v1/run", content=body())).status_code == 401
    assert not consumed


def test_missing_secret_and_browser_and_disallowed_action_fail_closed() -> None:
    fake = FakeSandbox()
    with TestClient(create_manager_app(fake, settings=ManagerSettings())) as api:
        assert api.post("/internal/v1/run", json=PAYLOAD, headers=AUTH).status_code == 503
    configured = ManagerSettings(
        sandbox_manager_token=TOKEN_SECRET, sandbox_allowed_actions=["static_check"]
    )
    with TestClient(create_manager_app(fake, settings=configured)) as api:
        assert (
            api.post(
                "/internal/v1/run", json=PAYLOAD, headers=AUTH | {"Origin": "http://evil.test"}
            ).status_code
            == 403
        )
        assert api.post("/internal/v1/run", json=PAYLOAD, headers=AUTH).status_code == 403
    assert fake.request is None


async def test_body_cap_counts_chunks_without_content_length() -> None:
    fake = FakeSandbox()
    app = create_manager_app(
        fake,
        settings=ManagerSettings(
            sandbox_manager_token=TOKEN_SECRET,
            sandbox_manager_max_body_bytes=1024,
        ),
    )

    async def chunks() -> Any:
        yield b"a" * 700
        yield b"b" * 700

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://manager"
    ) as api:
        assert (
            await api.post("/internal/v1/run", content=chunks(), headers=AUTH)
        ).status_code == 413
    assert fake.request is None


async def test_concurrency_and_cancel_release_execution_slot() -> None:
    entered = asyncio.Event()

    class HeldSandbox(FakeSandbox):
        async def run(self, request: SandboxRunRequest) -> Any:
            entered.set()
            await asyncio.Event().wait()

    app = create_manager_app(
        HeldSandbox(),
        settings=ManagerSettings(
            sandbox_manager_token=TOKEN_SECRET,
            sandbox_manager_max_concurrent=1,
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://manager", headers=AUTH
    ) as api:
        pending = asyncio.create_task(api.post("/internal/v1/run", json=PAYLOAD))
        await asyncio.wait_for(entered.wait(), 2)
        assert (await api.post("/internal/v1/run", json=PAYLOAD)).status_code == 429
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert (await api.get("/internal/health")).status_code == 200


def test_separate_secret_file_is_bounded_and_never_echoed(tmp_path: Path) -> None:
    path = tmp_path / "manager.secret"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    assert ManagerSettings(sandbox_manager_token_file=path).sandbox_manager_token == TOKEN_SECRET
    assert Settings(sandbox_manager_token_file=path).sandbox_manager_token == TOKEN_SECRET
    with pytest.raises(ValidationError) as exc:
        ManagerSettings(sandbox_manager_token_file=path, sandbox_manager_token=TOKEN_SECRET)
    assert TOKEN not in str(exc.value)
    with pytest.raises(ValidationError):
        Settings(sandbox_manager_token=TOKEN_SECRET, model_encryption_key=TOKEN_SECRET)
    path.write_bytes(b"a" * 4097)
    with pytest.raises(ValidationError):
        ManagerSettings(sandbox_manager_token_file=path)


def test_experiment_mcp_loads_a_distinct_manager_credential(tmp_path: Path) -> None:
    from autoscholar.tool_platform.experiment_server import ExperimentServerSettings

    path = tmp_path / "manager.secret"
    path.write_text(TOKEN, encoding="utf-8")
    settings = ExperimentServerSettings(
        database_url="postgresql+asyncpg://user:public-fixture-password@localhost/fixture",
        mcp_service_token=SecretStr("public-mcp-service-token-0123456789"),
        sandbox_manager_token_file=path,
    )
    assert settings.sandbox_manager_token == TOKEN_SECRET
    assert "public-fixture-password" not in repr(settings)
    with pytest.raises(ValidationError):
        ExperimentServerSettings(
            database_url="unused",
            mcp_service_token=TOKEN_SECRET,
            sandbox_manager_token=TOKEN_SECRET,
        )


async def test_client_attaches_service_credential_and_never_follows_redirect() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer " + TOKEN
        return httpx.Response(307, headers={"Location": "http://evil.test/"})

    sandbox = SandboxClient(
        "http://manager", token=TOKEN_SECRET, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(SandboxError):
            await sandbox.run(SandboxRunRequest.model_validate(PAYLOAD))
        assert len(calls) == 1
    finally:
        await sandbox.close()


async def test_unconfigured_client_makes_no_http_request() -> None:
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    sandbox = SandboxClient("http://manager", transport=httpx.MockTransport(handler))
    try:
        assert not (await sandbox.health()).engine
        with pytest.raises(SandboxError):
            await sandbox.run(SandboxRunRequest.model_validate(PAYLOAD))
        assert not calls
    finally:
        await sandbox.close()


@pytest.mark.parametrize(
    "override",
    [{"privileged": True}, {"network_mode": "host"}, {"mounts": ["/:/host"]}, {"action": "docker"}],
)
def test_payload_cannot_request_host_privileges(override: dict[str, Any]) -> None:
    fake = FakeSandbox()
    with TestClient(
        create_manager_app(fake, settings=ManagerSettings(sandbox_manager_token=TOKEN_SECRET))
    ) as api:
        response = api.post("/internal/v1/run", json=PAYLOAD | override, headers=AUTH)
        assert response.status_code == 422
    assert fake.request is None


def test_archive_member_count_and_file_size_caps() -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for i in range(33):
            directory = tarfile.TarInfo(f"dir{i}")
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
    with pytest.raises(SandboxError):
        DockerSandboxExecutor._read_archive_file(stream.getvalue(), "metrics.json")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo("metrics.json")
        info.size = 10
        archive.addfile(info, io.BytesIO(b"x" * 10))
    with pytest.raises(SandboxError):
        DockerSandboxExecutor._read_archive_file(stream.getvalue(), "metrics.json", max_bytes=5)


@pytest.mark.parametrize(
    "kind", ["traversal", "absolute", "symlink", "hardlink", "device", "gzip", "wrong_parent"]
)
def test_artifact_archive_boundary_rejects_unsafe_members(kind: str) -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz" if kind == "gzip" else "w") as archive:
        name = {
            "traversal": "../metrics.json",
            "absolute": "/metrics.json",
            "wrong_parent": "other/metrics.json",
        }.get(kind, "metrics.json")
        info = tarfile.TarInfo(name)
        if kind in {"symlink", "hardlink", "device"}:
            info.type = {
                "symlink": tarfile.SYMTYPE,
                "hardlink": tarfile.LNKTYPE,
                "device": tarfile.CHRTYPE,
            }[kind]
            info.linkname = "../../.env"
        else:
            info.size = 2
        archive.addfile(info, io.BytesIO(b"{}") if info.isfile() else None)
    with pytest.raises(SandboxError):
        DockerSandboxExecutor._read_archive_file(stream.getvalue(), "outputs/metrics.json")


class EngineFixture:
    def __init__(self, foreign: bool = False, unknown_create: bool = False) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.foreign, self.unknown_create = foreign, unknown_create
        self.started: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/volumes/create":
            body = json.loads(request.content)
            self.records["/volumes/" + body["Name"]] = body
            return httpx.Response(201, json=body)
        if path == "/containers/create":
            body = json.loads(request.content)
            identifier = ("a" if not self.started else "b") * 64
            if self.foreign:
                body["Labels"] = {"autoscholar.instance": "other"}
            record = {"Config": body}
            self.records["/containers/" + identifier + "/json"] = record
            name = str(request.url.params["name"])
            self.records["/containers/" + name + "/json"] = record
            if self.unknown_create:
                raise httpx.ReadError("lost acknowledgement", request=request)
            return httpx.Response(201, json={"Id": identifier})
        if request.method == "DELETE":
            self.deleted.append(path)
            self.records.pop(path + "/json", None)
            self.records.pop(path, None)
            return httpx.Response(204)
        if request.method == "GET" and path in self.records:
            return httpx.Response(200, json=self.records[path])
        if path.endswith("/start"):
            self.started.append(path)
            return httpx.Response(204)
        if path.endswith("/archive") and request.method == "PUT":
            return httpx.Response(200)
        if path.endswith("/wait"):
            return httpx.Response(200, json={"StatusCode": 0})
        if path.endswith("/logs"):
            return httpx.Response(200, content=b"ok")
        return httpx.Response(404)


@pytest.mark.parametrize("foreign,unknown", [(False, False), (True, False), (False, True)])
async def test_cleanup_requires_labels_and_recovers_lost_create_ack(
    foreign: bool, unknown: bool
) -> None:
    fixture = EngineFixture(foreign=foreign, unknown_create=unknown)
    executor = DockerSandboxExecutor(
        image="public", dataset_volume="public", dataset_ready_file="missing"
    )
    await executor._client.aclose()
    executor._client = httpx.AsyncClient(
        base_url="http://docker", transport=httpx.MockTransport(fixture.handle)
    )
    try:
        if foreign or unknown:
            with pytest.raises(SandboxError):
                await executor.run(SandboxRunRequest.model_validate(PAYLOAD))
        else:
            assert (
                await executor.run(SandboxRunRequest.model_validate(PAYLOAD))
            ).status == "succeeded"
        if foreign:
            assert not fixture.started
            assert not any(path.startswith("/containers/") for path in fixture.deleted)
        elif unknown:
            assert any(
                path.startswith("/containers/autoscholar-loader-") for path in fixture.deleted
            )
        else:
            assert len(fixture.started) == 2
            assert len(fixture.deleted) == 3
    finally:
        await executor.close()
