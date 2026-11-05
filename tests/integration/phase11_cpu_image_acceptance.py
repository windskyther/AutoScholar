"""Real CPU image compatibility checks; run only in an owned trusted Docker controller."""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path

from autoscholar.coding.sandbox import DockerSandboxExecutor, SandboxRunRequest
from autoscholar.experiment.models import ExperimentSpecification, ModelMetrics


async def main() -> None:
    executor = DockerSandboxExecutor(
        image=os.environ["SANDBOX_IMAGE"],
        dataset_volume=os.environ["SANDBOX_DATASET_VOLUME"],
        dataset_ready_file="/datasets/mnist/.autoscholar-ready",
        evaluation_owner=os.environ["ACCEPTANCE_OWNER"],
    )
    try:
        probe = await executor.run(
            SandboxRunRequest(
                task_id="cpu-image-security",
                action="run_python",
                path="probe.py",
                timeout_seconds=30,
                files={
                    "probe.py": (
                        "import os,stat,socket,sys,json\nfrom pathlib import Path\n"
                        "import torch,torchvision,PIL,filelock\n"
                        "assert os.getuid()==65532\n"
                        "assert 'NoNewPrivs:\\t1' in Path('/proc/self/status').read_text()\n"
                        "assert torch.__version__=='2.10.0+cpu'\n"
                        "assert torchvision.__version__=='0.25.0+cpu'\n"
                        "assert PIL.__version__=='13.0.0'\n"
                        "assert not any('TOKEN' in k or 'API_KEY' in k for k in os.environ)\n"
                        "assert not Path('/var/run/docker.sock').exists()\n"
                        "for folder in ['/usr/bin','/usr/sbin']:\n"
                        " for path in Path(folder).iterdir():\n"
                        "  assert not path.stat().st_mode & (stat.S_ISUID|stat.S_ISGID)\n"
                        "for name in ['/root-write-test','/datasets/mnist/write-test']:\n"
                        " try: Path(name).write_text('forbidden')\n"
                        " except OSError: pass\n"
                        " else: raise AssertionError('protected path writable')\n"
                        "try: socket.create_connection(('192.0.2.1',443),timeout=.2)\n"
                        "except OSError: pass\n"
                        "else: raise AssertionError('network escape')\n"
                        "print(json.dumps({'python':sys.version.split()[0],"
                        "'torch':torch.__version__,"
                        "'torchvision':torchvision.__version__, 'pillow':PIL.__version__}))\n"
                    )
                },
            )
        )
        assert probe.status == "succeeded", probe.stderr
        from autoscholar.experiment import __file__ as experiment_file

        assert experiment_file
        template_root = Path(experiment_file).parent
        spec = ExperimentSpecification(epochs=1, train_samples=128, test_samples=128)
        files = {
            "train.py": (template_root / "train_template.py").read_text(encoding="utf-8"),
            "test_models.py": (template_root / "test_template.py").read_text(encoding="utf-8"),
            "experiment_config.json": spec.model_dump_json(),
        }
        for action in ("static_check", "run_pytest"):
            result = await executor.run(
                SandboxRunRequest(
                    task_id="cpu-image-compatibility",
                    action=action,
                    files=files,
                    timeout_seconds=90,
                )
            )
            assert result.status == "succeeded", (action, result.stdout, result.stderr)
        paths = [
            "outputs/raw_metrics.json",
            "outputs/loss.png",
            "outputs/accuracy.png",
            "checkpoints/mlp.pt",
            "checkpoints/cnn.pt",
        ]
        result = await executor.run(
            SandboxRunRequest(
                task_id="cpu-image-training",
                action="run_python",
                path="train.py",
                files=files,
                timeout_seconds=120,
                collect_artifacts=paths,
            )
        )
        assert result.status == "succeeded", (result.stdout, result.stderr)
        assert {item.path for item in result.artifacts} == set(paths)
        artifacts = {}
        for item in result.artifacts:
            value = base64.b64decode(item.data_base64, validate=True)
            assert (
                hashlib.sha256(value).hexdigest() == item.sha256 and len(value) == item.size_bytes
            )
            artifacts[item.path] = value
        metrics = json.loads(artifacts["outputs/raw_metrics.json"])
        runs = [ModelMetrics.model_validate(value) for value in metrics["runs"]]
        assert [run.model for run in runs] == ["mlp", "cnn"]
        assert metrics["train_samples"] == metrics["test_samples"] == 128
        print(
            json.dumps(
                {
                    "accepted": True,
                    "security_probe": json.loads(probe.stdout.strip()),
                    "training_runs": 1,
                    "artifacts_verified": len(artifacts),
                    "accuracy": {run.model: run.test_accuracy for run in runs},
                    "supplier_api_calls": 0,
                }
            )
        )
    finally:
        await executor.close()


if __name__ == "__main__":
    asyncio.run(main())
