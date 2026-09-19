from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from autoscholar.api.experiment_auth import require_experiment_token
from autoscholar.core.config import Settings
from autoscholar.core.errors import register_exception_handlers


def _client(token: str | None) -> TestClient:
    app = FastAPI()
    settings = Settings(
        experiment_api_token=SecretStr(token) if token else None,
    )
    app.state.settings = settings
    register_exception_handlers(app)

    @app.get("/guarded")
    async def guarded(request: Request) -> dict[str, bool]:
        require_experiment_token(request)
        return {"ok": True}

    return TestClient(app)


def test_experiment_routes_fail_closed_without_configuration() -> None:
    with _client(None) as client:
        assert client.get("/guarded").status_code == 503


def test_experiment_routes_require_bearer_token() -> None:
    with _client("long-test-token") as client:
        assert client.get("/guarded").status_code == 401
        assert client.get(
            "/guarded", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
        assert client.get(
            "/guarded", headers={"Authorization": "Bearer long-test-token"}
        ).json() == {"ok": True}
