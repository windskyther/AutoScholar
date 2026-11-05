"""Exercise fresh, isolated production ingress with public fixtures and no supplier keys."""

import argparse
import http.client
import json
import os
import shutil
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from configure_production import initialize


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tls", action="store_true", help="Use a temporary localhost certificate")
    parser.add_argument("--openssl", help="OpenSSL executable (required for --tls)")
    parser.add_argument("--api", default="autoscholar-api:prod-local")
    parser.add_argument("--web", default="autoscholar-web:prod-local")
    parser.add_argument("--sandbox", default="autoscholar-python-sandbox:phase11")
    parser.add_argument(
        "--workflow", action="store_true", help="Run real CPU workflow through Nginx"
    )
    parser.add_argument(
        "--fault-once", action="store_true", help="Lose one metric artifact, then recover"
    )
    parser.add_argument("--download-dataset", action="store_true")
    parser.add_argument(
        "--keep-stack", action="store_true", help="Retain owned fixture for recovery drill"
    )
    args = parser.parse_args()
    if args.fault_once and not args.workflow:
        raise SystemExit("--fault-once requires --workflow")
    root = Path(__file__).resolve().parents[1]
    project = "phase11e-" + uuid4().hex[:12]
    output = root / "data/validation" / project
    secret_dir = output / "secrets"
    initialize(secret_dir)
    # Never inherit application, supplier, Compose or deployment settings from the caller.
    # Only the public example and this fresh fixture configure the acceptance stack.
    from autoscholar.core.config import Settings

    settings = {name.upper() for name in Settings.model_fields}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in settings
        and not key.upper().startswith(("COMPOSE_", "PROD_", "AUTOSCHOLAR_"))
    }
    environment.update(
        COMPOSE_PROJECT_NAME=project,
        PROD_SECRETS_DIR=str(secret_dir),
        PYTHONUTF8="1",
        AUTOSCHOLAR_API_IMAGE=args.api,
        AUTOSCHOLAR_WEB_IMAGE=args.web,
        SANDBOX_IMAGE=args.sandbox,
        PHASE11_TEST_FAULT_ONCE="1" if args.fault_once else "0",
    )
    command = [
        "docker",
        "compose",
        "--env-file",
        ".env.production.example",
        "-p",
        project,
        "-f",
        "compose.production.yaml",
        "-f",
        "tests/integration/phase11.production.yaml",
    ]
    if args.workflow:
        command += ["-f", "tests/integration/phase11.workflow.yaml"]

    def run(values: list[str], *, directory: Path = root) -> str:
        result = subprocess.run(
            values,
            cwd=directory,
            env=environment,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        # Logs are restricted to the generated fixture; no normal application .env is loaded.
        name = f"{len(list(output.glob('command-*.log'))):02d}"
        (output / f"command-{name}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"Acceptance command failed ({name}); inspect {output}")
        return result.stdout

    if args.tls:
        openssl = args.openssl or shutil.which("openssl")
        if not openssl:
            raise SystemExit("--tls requires OpenSSL; supply --openssl PATH")
        certificate, key = secret_dir / "localhost.pem", secret_dir / "localhost.key"
        config = output / "openssl.cnf"
        config.write_text("[req]\ndistinguished_name=dn\n[dn]\n", encoding="ascii")
        run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
                "-config",
                str(config),
                "-keyout",
                str(key),
                "-out",
                str(certificate),
            ]
        )
        key.chmod(0o644)  # Private parent, explicit mount readable by Nginx UID 101.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        environment.update(
            PROD_TLS_CERT=str(certificate),
            PROD_TLS_KEY=str(key),
            PROD_TLS_PORT=str(port),
            API_ALLOWED_ORIGINS=json.dumps([f"https://127.0.0.1:{port}"]),
        )
        command += ["-f", "compose.production.tls.yaml"]
    config = json.loads(run([*command, "config", "--format", "json"]))
    services = config["services"]
    for name, service in services.items():
        if name == "web":
            assert len(service["ports"]) == 1
            assert service["ports"][0]["host_ip"] == "127.0.0.1"
            assert service["ports"][0]["target"] == (8443 if args.tls else 8080)
        else:
            assert not service.get("ports"), name
        if name in {"api", "workflow-worker", "document-worker", "web"}:
            assert service["user"] in {"10001:10001", "101:101"} and service["read_only"]
        for volume in service.get("volumes", []):
            if volume.get("target") == "/var/run/docker.sock":
                assert name == "sandbox-manager"
        for name in ("LLM_API_KEY", "TAVILY_API_KEY", "SEMANTIC_SCHOLAR_API_KEY"):
            assert name not in service.get("environment", {})
    assert all(
        value.get("internal") for name, value in config["networks"].items() if name != "ingress"
    )
    assert all(
        not value.get("external") and value["name"].startswith(project + "_")
        for value in config["volumes"].values()
    )
    assert "ingress" not in services["api"]["networks"]
    report: dict[str, object] = {
        "project": project,
        "tls": args.tls,
        "started": datetime.now(UTC).isoformat(),
        "only_loopback_web_published": True,
        "supplier_credentials": False,
        "workflow_execution": "real CPU with offline coordinator"
        if args.workflow
        else "held; ingress acceptance only",
        "expected_training_runs": 2 if args.fault_once else 1 if args.workflow else 0,
        "accepted": False,
    }
    try:
        if args.workflow:
            dataset = config["volumes"]["mnist"]["name"]
            run(
                [
                    "docker",
                    "volume",
                    "create",
                    "--label",
                    "com.docker.compose.project=" + project,
                    "--label",
                    "com.docker.compose.volume=mnist",
                    dataset,
                ]
            )
            prepare = [
                "docker",
                "run",
                "--rm",
                "--user",
                "0:0",
                "--label",
                "com.docker.compose.project=" + project,
                "--mount",
                f"type=volume,source={dataset},target=/datasets/mnist",
            ]
            if args.download_dataset:
                run([*prepare, args.sandbox, "python", "/opt/autoscholar/prepare_mnist.py"])
            else:
                run(
                    [
                        *prepare,
                        "--network",
                        "none",
                        "--mount",
                        "type=volume,source=autoscholar_mnist_data,target=/public,readonly",
                        "--mount",
                        "type=bind,source="
                        + str(root / "tests/integration/phase9_stack_runtime.py")
                        + ",target=/dataset_fixture.py,readonly",
                        args.api,
                        "python",
                        "/dataset_fixture.py",
                        "dataset-copy",
                    ]
                )
        run([*command, "up", "-d", "--no-build", "--wait", "--wait-timeout", "240"])
        target = "8443" if args.tls else "8080"
        published = run([*command, "port", "web", target]).strip()
        assert published.startswith("127.0.0.1:") and published.rsplit(":", 1)[1].isdigit()
        url = ("https://" if args.tls else "http://") + published
        environment.update(
            PRODUCTION_URL=url,
            PRODUCTION_TEST_TOKEN=(secret_dir / "api_token").read_text().strip(),
            PRODUCTION_REPORT_FILE=str(output / "browser.json"),
        )
        run(
            [
                "node",
                "scripts/browser.mjs",
                "test",
                "--config",
                "playwright.workflow.config.ts"
                if args.workflow
                else "playwright.production.config.ts",
            ],
            directory=root / "web",
        )
        browser = json.loads((output / "browser.json").read_text(encoding="utf-8"))
        expected = 1 if args.workflow else 6
        assert browser["stats"]["expected"] == expected and browser["stats"]["unexpected"] == 0
        containers = run([*command, "ps", "-q"]).split()
        actual = json.loads(run(["docker", "inspect", *containers]))
        for item in actual:
            service = item["Config"]["Labels"]["com.docker.compose.service"]
            if service in {"api", "workflow-worker", "document-worker", "web"}:
                assert item["HostConfig"]["ReadonlyRootfs"]
                assert item["Config"]["User"] in {"10001:10001", "101:101"}
                assert "ALL" in item["HostConfig"]["CapDrop"]
                assert item["HostConfig"]["Memory"] > 0 and item["HostConfig"]["PidsLimit"] > 0
        # Declared upload size is rejected at the proxy before sending any document bytes.
        if args.tls:
            import ssl

            connection = http.client.HTTPSConnection(
                "127.0.0.1",
                int(published.rsplit(":", 1)[1]),
                timeout=10,
                context=ssl._create_unverified_context(),  # Only the generated fixture certificate.
            )
        else:
            connection = http.client.HTTPConnection(
                "127.0.0.1", int(published.rsplit(":", 1)[1]), timeout=10
            )
        try:
            connection.putrequest("POST", "/api/workbench/projects/fixture/documents")
            connection.putheader("Content-Length", str(51 * 1024 * 1024 + 1))
            connection.endheaders()
            assert connection.getresponse().status == 413
        finally:
            connection.close()
        image_ids = {
            item["Image"]
            for item in actual
            if item["Config"]["Labels"]["com.docker.compose.service"]
            in {"api", "workflow-worker", "document-worker"}
        }
        assert len(image_ids) == 1
        report.update(
            accepted=True, browser_passed=expected, url=url, backend_image_id=image_ids.pop()
        )
    finally:
        try:
            run([*command, "logs", "--no-color", "--tail", "80"])
        finally:
            # Rendered configuration above rejects external/non-owned volumes before any creation.
            assert output.resolve().is_relative_to((root / "data/validation").resolve())
            retained = args.keep_stack and report["accepted"]
            if not retained:
                run([*command, "down", "--volumes", "--remove-orphans"])
                remaining = run(
                    [
                        "docker",
                        "ps",
                        "-aq",
                        "--filter",
                        f"label=com.docker.compose.project={project}",
                    ]
                ).strip()
                assert not remaining
            report["containers_removed"] = not retained
            (output / "receipt.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {"accepted": report["accepted"], "receipt": str(output / "receipt.json")}
                )
            )


if __name__ == "__main__":
    main()
