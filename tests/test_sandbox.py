from pathlib import Path

from fastapi.testclient import TestClient

from autoscholar.coding.sandbox import (
    DockerSandboxExecutor,
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.sandbox.manager import create_manager_app


class FakeSandbox:
    def __init__(self) -> None:
        self.request: SandboxRunRequest | None = None
        self.closed = False

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        self.request = request
        return SandboxRunResult(
            status="succeeded",
            exit_code=0,
            stdout="测试通过\n",
            stderr="",
            duration_ms=12.5,
        )

    async def health(self) -> SandboxHealth:
        return SandboxHealth(status="ok", engine=True, image=True, mnist_dataset=True)

    async def close(self) -> None:
        self.closed = True


def test_sandbox_request_rejects_paths_and_shell_injection() -> None:
    invalid = ["../main.py", "/etc/passwd", "C:\\secret", "./main.py"]
    for path in invalid:
        try:
            SandboxRunRequest(
                task_id="task-1", action="run_python", path=path, files={"main.py": ""}
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe path accepted: {path}")

    try:
        SandboxRunRequest(
            task_id="task-1", action="run_shell", path="sh", files={"main.py": ""}
        )
    except ValueError:
        pass
    else:
        raise AssertionError("unapproved executable accepted")


def test_manager_exposes_only_validated_internal_execution() -> None:
    fake = FakeSandbox()
    with TestClient(create_manager_app(fake)) as client:
        health = client.get("/internal/health")
        result = client.post(
            "/internal/v1/run",
            json={
                "task_id": "task-1",
                "action": "run_pytest",
                "args": ["tests"],
                "files": {"tests/test_ok.py": "def test_ok():\n    assert True\n"},
                "timeout_seconds": 30,
            },
        )

    assert health.json()["mnist_dataset"] is True
    assert result.status_code == 200
    assert result.json()["stdout"] == "测试通过\n"
    assert fake.request is not None and fake.request.action == "run_pytest"
    assert fake.closed is True


def test_container_config_has_required_isolation_controls() -> None:
    executor = DockerSandboxExecutor(
        image="sandbox:test",
        dataset_volume="mnist",
        dataset_ready_file="/datasets/mnist/.ready",
    )
    request = SandboxRunRequest(
        task_id="task-1",
        action="run_python",
        path="train.py",
        args=["--epochs", "1"],
        files={"train.py": "print('ok')\n"},
    )
    config = executor._container_config(request)

    assert config["NetworkDisabled"] is True
    assert config["User"] == "65532:65532"
    assert config["Env"] == [
        "HOME=/tmp",
        "PYTHONIOENCODING=utf-8",
        "PYTHONDONTWRITEBYTECODE=1",
        "MNIST_ROOT=/datasets/mnist",
    ]
    assert config["HostConfig"]["ReadonlyRootfs"] is True
    assert config["HostConfig"]["CapDrop"] == ["ALL"]
    assert config["HostConfig"]["SecurityOpt"] == ["no-new-privileges"]
    assert config["HostConfig"]["Memory"] == 4 * 1024**3
    assert config["HostConfig"]["PidsLimit"] == 256
    assert config["HostConfig"]["Mounts"][0] == {
        "Type": "volume",
        "Source": "autoscholar-test-workspace",
        "Target": "/workspace",
        "ReadOnly": False,
    }
    assert config["HostConfig"]["Mounts"][1]["ReadOnly"] is True

    loader = executor._loader_config("temporary-workspace")
    assert loader["NetworkDisabled"] is True
    assert loader["HostConfig"]["ReadonlyRootfs"] is False
    assert loader["HostConfig"]["PidsLimit"] == 32
    assert loader["Cmd"] == ["python", "-c", "import time; time.sleep(30)"]


def test_source_archive_contains_only_workspace_source(tmp_path: Path) -> None:
    del tmp_path
    archive = DockerSandboxExecutor._source_archive(
        {"pkg/model.py": "class Model:\n    pass\n"}
    )
    assert b".env" not in archive
    assert b".autoscholar-source-ready" not in archive
    assert b"source/pkg/model.py" in archive
