from fastapi.testclient import TestClient

from autoscholar.core.config import Settings
from autoscholar.main import create_app


def test_liveness_and_request_id() -> None:
    client = TestClient(create_app(Settings()))

    response = client.get("/health/live", headers={"X-Request-ID": "test-request"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "test-request"
    assert response.json() == {
        "status": "ok",
        "service": "autoscholar",
        "version": "0.1.0",
    }


def test_invalid_request_id_is_replaced() -> None:
    client = TestClient(create_app(Settings()))

    response = client.get("/health/live", headers={"X-Request-ID": "invalid request id"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "invalid request id"
