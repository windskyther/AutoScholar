"""Owned CPU acceptance on Linux/Windows; public data, no supplier credentials."""

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4


class CPUAcceptance:
    def __init__(self, root: Path, api: str, sandbox: str) -> None:
        self.root = root
        self.owner = "phase11cpu-" + uuid4().hex[:12]
        self.output = root / "data/validation" / self.owner
        self.output.mkdir(parents=True)
        self.api = self.inspect("image", api)["Id"]
        self.sandbox = self.inspect("image", sandbox)["Id"]
        self.volumes: list[str] = []
        self.containers: list[str] = []
        self.report: dict[str, Any] = {
            "accepted": False,
            "supplier_api_calls": 0,
            "api_image": self.api,
            "sandbox_image": self.sandbox,
        }

    def run(self, *args: str, timeout: int = 300) -> str:
        result = subprocess.run(
            args,
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        log = self.output / f"command-{len(list(self.output.glob('command-*.log'))):02d}.log"
        log.write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"CPU acceptance command failed; inspect {log}")
        return result.stdout

    def inspect(self, kind: str, name: str) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.run("docker", kind, "inspect", name))[0]
        return value

    def volume(self, suffix: str) -> str:
        name = self.owner + "-" + suffix
        self.run(
            "docker", "volume", "create", "--label", "autoscholar.acceptance=" + self.owner, name
        )
        self.volumes.append(name)
        return name

    def dataset(self, source: str | None) -> str:
        target = self.volume("dataset")
        if source is None:
            # Only the trusted public dataset downloader gets network access, never
            # the controller or generated source. torchvision verifies MNIST hashes.
            self.run(
                "docker",
                "run",
                "--rm",
                "--user",
                "0:0",
                "--label",
                "autoscholar.acceptance=" + self.owner,
                "--mount",
                f"type=volume,source={target},target=/datasets/mnist",
                self.sandbox,
                "python",
                "/opt/autoscholar/prepare_mnist.py",
                timeout=300,
            )
        else:
            self.inspect("volume", source)
            self.run(
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "--label",
                "autoscholar.acceptance=" + self.owner,
                "--mount",
                f"type=volume,source={source},target=/public,readonly",
                "--mount",
                f"type=volume,source={target},target=/datasets/mnist",
                "--mount",
                "type=bind,source="
                + str(self.root / "tests/integration/phase9_stack_runtime.py")
                + ",target=/dataset_fixture.py,readonly",
                self.api,
                "python",
                "/dataset_fixture.py",
                "dataset-copy",
            )
        return target

    def verify(self, dataset: str) -> None:
        controller = self.owner + "-controller"
        self.run(
            "docker",
            "create",
            "--name",
            controller,
            "--network",
            "none",
            "--user",
            "0:0",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,size=268435456",
            "--label",
            "autoscholar.acceptance=" + self.owner,
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--mount",
            "type=bind,source="
            + str(self.root / "tests/integration/phase11_cpu_image_acceptance.py")
            + ",target=/check.py,readonly",
            "--env",
            "SANDBOX_IMAGE=" + self.sandbox,
            "--env",
            "SANDBOX_DATASET_VOLUME=" + dataset,
            "--env",
            "ACCEPTANCE_OWNER=" + self.owner,
            self.api,
            "python",
            "/check.py",
        )
        self.containers.append(controller)
        output = self.run("docker", "start", "--attach", controller)
        assert self.inspect("container", controller)["State"]["ExitCode"] == 0
        result = json.loads(output.strip())
        assert result["accepted"] and result["supplier_api_calls"] == 0
        self.report.update(cpu=result, accepted=True)

    def close(self) -> None:
        for kind in ("container", "volume"):
            for name in self.run(
                "docker", kind, "ls", "-q", "--filter", "label=autoscholar.evaluation=" + self.owner
            ).split():
                record = self.inspect(kind, name)
                labels = record["Config"]["Labels"] if kind == "container" else record["Labels"]
                assert labels.get("autoscholar.evaluation") == self.owner
                assert labels.get("autoscholar.sandbox") == "true"
                self.run("docker", kind, "rm", "--force", name)
        owned = self.run(
            "docker", "ps", "-aq", "--filter", "label=autoscholar.acceptance=" + self.owner
        ).split()
        for name in owned:
            assert (
                self.inspect("container", name)["Config"]["Labels"]["autoscholar.acceptance"]
                == self.owner
            )
            self.run("docker", "rm", "--force", "--volumes", name)
        for name in self.volumes:
            assert self.inspect("volume", name)["Labels"]["autoscholar.acceptance"] == self.owner
            self.run("docker", "volume", "rm", name)
        self.report["resources_removed"] = True
        (self.output / "receipt.json").write_text(
            json.dumps(self.report, indent=2), encoding="utf-8"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="autoscholar-api:prod-local")
    parser.add_argument("--sandbox", default="autoscholar-python-sandbox:phase11")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--source-volume", default="autoscholar_mnist_data")
    source.add_argument("--download-dataset", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    # Docker receives explicit fixture env only; no --env-file/.env or supplier keys.
    fixture = CPUAcceptance(root, args.api, args.sandbox)
    try:
        fixture.verify(fixture.dataset(None if args.download_dataset else args.source_volume))
    finally:
        fixture.close()
    print(
        json.dumps(
            {
                "accepted": fixture.report["accepted"],
                "receipt": str(fixture.output / "receipt.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
