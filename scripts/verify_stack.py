"""Portable isolated browser/worker/CPU stack acceptance, never normal deployment."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from verify_cpu import CPUAcceptance

from autoscholar.core.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-dataset", action="store_true")
    parser.add_argument("--api", default="autoscholar-api:prod-local")
    parser.add_argument("--sandbox", default="autoscholar-python-sandbox:phase11")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    fixture = CPUAcceptance(root, args.api, args.sandbox)
    project = "autoscholar-phase11f-" + uuid4().hex[:12]
    output = root / "data/validation" / project
    output.mkdir(parents=True)
    settings = {name.upper() for name in Settings.model_fields}
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in settings
        and not k.upper().startswith(("COMPOSE_", "PROD_", "AUTOSCHOLAR_", "PHASE11_"))
    }
    env.update(
        COMPOSE_PROJECT_NAME=project,
        PHASE11_API_IMAGE=fixture.api,
        PHASE11_CPU_IMAGE=fixture.sandbox,
        PYTHONUTF8="1",
    )
    command = [
        "docker",
        "compose",
        "--env-file",
        "tests/integration/phase9-public-config.txt",
        "-p",
        project,
        "-f",
        "tests/integration/phase9.compose.yaml",
        "-f",
        "tests/integration/phase11.stack.yaml",
    ]

    def run(values: list[str], directory: Path = root) -> str:
        result = subprocess.run(
            values,
            cwd=directory,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        log = output / f"command-{len(list(output.glob('command-*.log'))):02d}.log"
        log.write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"Isolated stack failed; inspect {log}")
        return result.stdout

    accepted = False
    configured = False
    resources_removed = False
    try:
        dataset = fixture.dataset(None if args.download_dataset else "autoscholar_mnist_data")
        fixture.verify(dataset)
        env["PHASE11_DATASET_SOURCE"] = dataset
        config = json.loads(run([*command, "config", "--format", "json"]))
        for name, volume in config["volumes"].items():
            if name == "public-mnist":
                assert volume["external"] and volume["name"] == dataset
            else:
                assert not volume.get("external") and volume["name"].startswith(project + "_")
        for name, service in config["services"].items():
            assert not service.get("ports") or name == "ingress"
        assert config["services"]["ingress"]["ports"][0]["host_ip"] == "127.0.0.1"
        configured = True
        run([*command, "up", "-d", "--no-build", "--wait", "postgres", "redis", "qdrant"])
        run([*command, "run", "--rm", "--no-deps", "volume-init"])
        run([*command, "run", "--rm", "--no-deps", "migrate"])
        for mode in ("database-checks",):
            run(
                [
                    *command,
                    "run",
                    "--rm",
                    "--no-deps",
                    "migrate",
                    "python",
                    "/acceptance/runtime.py",
                    mode,
                ]
            )
        run(
            [
                *command,
                "run",
                "--rm",
                "--no-deps",
                "migrate",
                "alembic",
                "downgrade",
                "20261005_0012",
            ]
        )
        run([*command, "run", "--rm", "--no-deps", "migrate", "alembic", "upgrade", "head"])
        run(
            [
                *command,
                "run",
                "--rm",
                "--no-deps",
                "migrate",
                "python",
                "/acceptance/runtime.py",
                "backfill-check",
            ]
        )
        run([*command, "run", "--rm", "--no-deps", "dataset-copy"])
        run(
            [
                *command,
                "up",
                "-d",
                "--no-build",
                "--no-deps",
                "sandbox-manager",
                "api",
                "ingress",
                "workflow-worker",
                "document-worker",
            ]
        )
        binding = run([*command, "port", "ingress", "8000"]).strip()
        assert binding.startswith("127.0.0.1:")
        port = binding.rsplit(":", 1)[1]
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            web_port = listener.getsockname()[1]
        env.update(STACK_API_PORT=port, STACK_WEB_PORT=str(web_port))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def ready() -> None:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    request = urllib.request.Request(
                        f"http://{binding}/workbench/health/ready",
                        headers={"Authorization": "Bearer public-phase9g-token"},
                    )
                    with opener.open(request, timeout=2) as response:
                        if json.load(response)["status"] == "ready":
                            return
                except (OSError, ValueError):
                    pass
                time.sleep(0.25)
            raise TimeoutError("Isolated stack readiness timed out")

        ready()
        run(["npm.cmd" if sys.platform == "win32" else "npm", "run", "build"], root / "web")
        run(
            ["node", "scripts/browser.mjs", "test", "--config", "playwright.stack.config.ts"],
            root / "web",
        )
        run([*command, "restart", "api", "workflow-worker", "document-worker"])
        ready()
        run(
            [
                *command,
                "run",
                "--rm",
                "--no-deps",
                "migrate",
                "python",
                "/acceptance/runtime.py",
                "persistence-check",
            ]
        )
        accepted = True
    finally:
        try:
            if configured:
                if not accepted:
                    with suppress(RuntimeError):
                        run(
                            [
                                *command,
                                "run",
                                "--rm",
                                "--no-deps",
                                "migrate",
                                "python",
                                "/acceptance/runtime.py",
                                "diagnostics",
                            ]
                        )
                try:
                    # Stop consumers before cleaning their owned child resources.
                    run(
                        [
                            *command,
                            "stop",
                            "api",
                            "workflow-worker",
                            "document-worker",
                            "sandbox-manager",
                        ]
                    )
                    run([*command, "logs", "--no-color", "--tail", "80"])
                    ids = run(
                        [
                            "docker",
                            "ps",
                            "-aq",
                            "--filter",
                            "label=autoscholar.evaluation=" + project,
                        ]
                    ).split()
                    for identifier in ids:
                        record = json.loads(run(["docker", "inspect", identifier]))[0]
                        assert record["Config"]["Labels"].get("autoscholar.evaluation") == project
                        run(["docker", "rm", "--force", "--volumes", identifier])
                    for identifier in run(
                        [
                            "docker",
                            "volume",
                            "ls",
                            "-q",
                            "--filter",
                            "label=autoscholar.evaluation=" + project,
                        ]
                    ).split():
                        record = json.loads(run(["docker", "volume", "inspect", identifier]))[0]
                        assert record["Labels"].get("autoscholar.evaluation") == project
                        assert record["Labels"].get("autoscholar.sandbox") == "true"
                        run(["docker", "volume", "rm", identifier])
                finally:
                    run([*command, "down", "--volumes", "--remove-orphans"])
                assert not run(
                    [
                        "docker",
                        "ps",
                        "-aq",
                        "--filter",
                        "label=com.docker.compose.project=" + project,
                    ]
                ).strip()
            resources_removed = True
        finally:
            fixture.close()
            receipt = {
                "accepted": accepted and resources_removed,
                "project": project,
                "supplier_api_calls": 0,
                "cpu_receipt": str(fixture.output / "receipt.json"),
                "resources_removed": resources_removed,
            }
            (output / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            print(json.dumps(receipt))


if __name__ == "__main__":
    main()
