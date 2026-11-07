"""All business routes fail closed before parsing or dispatch, including legacy APIs."""

import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from autoscholar.core.config import Settings
from tests.api_credentials import AUTH, TOKEN, TOKEN_SECRET
from tests.managed_fixture import MODEL_ID
from tests.managed_fixture import managed_offline_app as offline_app
from tests.test_chat import SuccessfulProvider
from tests.test_health import FakeDependency, FakeSandbox
from tests.test_projects_api import FakeKnowledgeStore


def client_for(
    *, token: SecretStr | None = TOKEN_SECRET, **config: object
) -> tuple[TestClient, SuccessfulProvider, FakeKnowledgeStore]:
    provider, knowledge = SuccessfulProvider(), FakeKnowledgeStore()
    app = offline_app(
        Settings(model_encryption_key=token, **config),  # type: ignore[arg-type]
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=provider,
        sandbox_executor=FakeSandbox(),
        knowledge_repository=knowledge,
    )
    return TestClient(app), provider, knowledge


def test_every_business_route_rejects_before_validation_or_dispatch() -> None:
    client, provider, store = client_for()
    with client:
        paths = client.get("/openapi.json", headers=AUTH).json()["paths"]
        assert "/chat" in paths and "/projects" in paths and "/workbench/projects" in paths
        for template, operations in paths.items():
            if template == "/health/live":
                continue
            path = re.sub(r"\{[^{}]+\}", "not-a-valid-resource", template)
            for method in operations:
                if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                    continue
                response = client.request(
                    method,
                    path,
                    content=b"{invalid JSON",
                    headers={
                        "Origin": "https://foreign.invalid",
                        "content-type": "application/json",
                        "X-Request-ID": "denied",
                    },
                )
                assert response.status_code == 403, (method, path, response.text)
                assert response.json()["request_id"] == "denied"
                assert response.headers["cache-control"] == "private, no-store"
                assert "www-authenticate" not in response.headers
    assert not provider.messages and not store.projects and not store.documents


async def test_denied_upload_does_not_consume_body_stream() -> None:
    client, provider, store = client_for()
    reads = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal reads
        reads += 1
        yield b"This stream must not be consumed"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://testserver"
    ) as api:
        response = await api.post(
            "/projects/missing/documents",
            content=body(),
            headers={
                "Content-Type": "multipart/form-data; boundary=invalid",
                "Origin": "https://foreign.invalid",
            },
        )
    assert response.status_code == 403 and reads == 0
    assert not provider.messages and not store.documents


def test_local_calls_need_no_login_or_encryption_key() -> None:
    client, provider, store = client_for(token=None)
    with client:
        assert (
            client.post(
                "/chat", json={"model_profile_id": MODEL_ID, "message": "hello"}
            ).status_code
            == 200
        )
        assert (
            client.post("/projects", json={"name": "public test"}, headers=AUTH).status_code == 201
        )
        assert client.get("/health/ready", headers=AUTH).status_code == 200
        assert client.get("/openapi.json", headers=AUTH).status_code == 200
    assert provider.messages and len(store.projects) == 1


@pytest.mark.parametrize("origin", ["null", "https://evil.example", "http://testserver.evil"])
def test_disallowed_origin_is_rejected_even_with_credentials(origin: str) -> None:
    client, provider, _ = client_for()
    with client:
        assert (
            client.post(
                "/chat",
                json={"model_profile_id": MODEL_ID, "message": "hello"},
                headers={**AUTH, "Origin": origin},
            ).status_code
            == 403
        )
    assert not provider.messages


def test_cors_preflight_does_not_authorize_business_requests() -> None:
    client, provider, _ = client_for(api_allowed_origins=["https://workbench.example"])
    headers = {"Origin": "https://workbench.example"}
    with client:
        preflight = client.options(
            "/chat",
            headers={
                **headers,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == headers["Origin"]
        assert "access-control-allow-credentials" not in preflight.headers
        assert (
            client.post(
                "/chat", json={"model_profile_id": MODEL_ID, "message": "hello"}, headers=headers
            ).status_code
            == 200
        )
        response = client.post(
            "/chat",
            json={"model_profile_id": MODEL_ID, "message": "hello"},
            headers={**headers, **AUTH},
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == headers["Origin"]
    assert provider.messages


def test_forwarded_headers_do_not_bypass_host_or_origin_boundaries() -> None:
    client, provider, _ = client_for(api_allowed_hosts=["testserver"])
    with client:
        assert (
            client.post(
                "/chat",
                json={"model_profile_id": MODEL_ID, "message": "hello"},
                headers={**AUTH, "Host": "evil.example", "X-Forwarded-Host": "testserver"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/chat",
                json={"model_profile_id": MODEL_ID, "message": "hello"},
                headers={**AUTH, "Origin": "https://testserver", "X-Forwarded-Proto": "https"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/chat",
                json={"model_profile_id": MODEL_ID, "message": "hello"},
                headers={**AUTH, "Origin": "http://testserver"},
            ).status_code
            == 200
        )
    assert provider.messages


def production() -> dict[str, object]:
    return {
        "app_env": "production",
        "model_encryption_key": SecretStr("q9Vb3Wx7Lk2Rh8Ep4Tn6Jc5Ys1Dz0FuA"),
        "api_allowed_hosts": ["testserver"],
        "database_url": "postgresql+asyncpg://autoscholar:Q7Vr9pW2eK5nB8dX3zM6@postgres/autoscholar",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"model_encryption_key": None},
        {"model_encryption_key": SecretStr("short")},
        {"model_encryption_key": SecretStr("a" * 40)},
        {"api_allowed_hosts": ["*"]},
        {"database_url": "postgresql+asyncpg://autoscholar:autoscholar@postgres/autoscholar"},
        {"database_url": "not a URL"},
        {"api_allowed_origins": ["http://remote.example"]},
    ],
)
def test_unsafe_production_configuration_is_rejected(change: dict[str, object]) -> None:
    values = {**production(), **change}
    with pytest.raises(ValidationError) as caught:
        Settings(**values)  # type: ignore[arg-type]
    assert "Q7Vr9pW2" not in str(caught.value) and "q9Vb3Wx7" not in str(caught.value)


def test_production_disables_api_docs_and_separates_access_credentials() -> None:
    values = production()
    settings = Settings(**values)  # type: ignore[arg-type]
    assert "Q7Vr9pW2" not in repr(settings)
    token = values.pop("model_encryption_key")
    assert isinstance(token, SecretStr)
    client, _, _ = client_for(token=token, **values)
    with client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (
                client.get(
                    path, headers={"Authorization": "Bearer " + token.get_secret_value()}
                ).status_code
                == 404
            )
    with pytest.raises(ValidationError, match="distinct"):
        Settings(**{**production(), "llm_api_key": token})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    [
        "model_encryption_key",
        "llm_api_key",
        "embedding_api_key",
        "tavily_api_key",
        "semantic_scholar_api_key",
        "qdrant_api_key",
        "mcp_service_token",
    ],
)
def test_bounded_secret_files_and_conflicts(tmp_path: Path, name: str) -> None:
    path = tmp_path / "secret"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    settings = Settings(**{name + "_file": path})  # type: ignore[arg-type]
    assert getattr(settings, name).get_secret_value() == TOKEN
    assert TOKEN not in repr(settings)
    with pytest.raises(ValidationError, match="Configure only"):
        Settings(**{name: SecretStr("direct"), name + "_file": path})  # type: ignore[arg-type]


@pytest.mark.parametrize("content", [b"", b"x" * 4097, b"line one\nline two", b"\xff"])
def test_bad_secret_files_are_rejected_without_printing_contents(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "secret"
    path.write_bytes(content)
    with pytest.raises(ValidationError, match="readable bounded secret file"):
        Settings(llm_api_key_file=path)


@pytest.mark.parametrize(
    "values",
    [
        {"api_allowed_origins": ["*"]},
        {"api_allowed_origins": ["https://user:secret@example.org"]},
        {"api_allowed_origins": ["https://example.org/path"]},
        {"api_allowed_hosts": []},
        {"api_allowed_hosts": ["localhost:8000"]},
    ],
)
def test_invalid_boundary_configuration_is_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**values)  # type: ignore[arg-type]


def test_public_example_loads_with_empty_optional_secret_files() -> None:
    settings = Settings(_env_file=Path(__file__).resolve().parents[1] / ".env.example")  # type: ignore[call-arg]
    assert settings.llm_api_key_file is None and settings.model_encryption_key_file is None
    assert not settings.model_encryption_key and not settings.llm_configured
