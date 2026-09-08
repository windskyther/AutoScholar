import os

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AUTOSCHOLAR_RUN_INTEGRATION") != "1",
        reason="set AUTOSCHOLAR_RUN_INTEGRATION=1 to test the Compose stack",
    ),
]


def test_compose_stack_is_ready() -> None:
    live_response = httpx.get("http://localhost:8000/health/live", timeout=5)
    ready_response = httpx.get("http://localhost:8000/health/ready", timeout=5)

    assert live_response.status_code == 200
    assert live_response.json()["status"] == "ok"
    assert ready_response.status_code == 200
    readiness = ready_response.json()
    assert readiness["status"] == "ready"
    assert readiness["dependencies"] == {
        "postgres": {"status": "ok"},
        "qdrant": {"status": "ok"},
        "redis": {"status": "ok"},
    }
    assert readiness["capabilities"]["rag"] == {"status": "ok"}
    assert readiness["capabilities"]["paper_search"] == {"status": "ok"}
    assert readiness["capabilities"]["web_search"]["status"] in {
        "ok",
        "not_configured",
    }

    missing_task_response = httpx.get(
        "http://localhost:8000/agent/tasks/integration-missing", timeout=5
    )
    assert missing_task_response.status_code == 404
    assert missing_task_response.json()["error"]["code"] == "agent_task_not_found"

    missing_evidence_response = httpx.get(
        "http://localhost:8000/agent/tasks/integration-missing/evidence", timeout=5
    )
    assert missing_evidence_response.status_code == 404
    assert missing_evidence_response.json()["error"]["code"] == "agent_task_not_found"
