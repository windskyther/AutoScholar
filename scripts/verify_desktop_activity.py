"""Exercise a release install with a local model stub and a pre-cached PDF fixture."""

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import closing, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil
import requests
import websocket
from verify_desktop_memory import close_window, interactive


def memory(app: psutil.Process) -> float:
    size = 0
    for process in [app, *app.children(recursive=True)]:
        with suppress(psutil.NoSuchProcess):
            size += process.memory_full_info().uss
    return size / 1048576


def exercise(executable: Path, profile: Path, pdf: Path) -> None:
    received, release = threading.Event(), threading.Event()

    class Model(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            received.set()
            release.wait(30)
            self.send_error(400, "Intentional offline lifecycle fixture")

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = subprocess.Popen(
        [executable],
        env={
            **os.environ,
            "AUTOSCHOLAR_DESKTOP_PROFILE": str(profile),
            "HF_HUB_OFFLINE": "1",
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={port}",
        },
    )
    http = requests.Session()
    http.trust_env = False
    try:
        interactive(port, app)
        page = next(
            p
            for p in http.get(f"http://127.0.0.1:{port}/json/list", timeout=5).json()
            if p["type"] == "page"
        )
        with closing(
            websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True)
        ) as ws:
            ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": "[location.origin, window.__autoscholarSession]",
                            "returnByValue": True,
                        },
                    }
                )
            )
            origin, token = json.loads(ws.recv())["result"]["result"]["value"]
        http.headers["X-AutoScholar-Session"] = token
        base = origin + "/api/workbench"

        def post(path: str, **kwargs):
            response = http.post(base + path, timeout=20, **kwargs)
            response.raise_for_status()
            return response.json()

        project = post("/projects", json={"name": "Release fixture"})["id"]
        with pdf.open("rb") as stream:
            document = post(
                f"/projects/{project}/documents",
                files={"file": ("sample.pdf", stream, "application/pdf")},
            )["id"]
        process = psutil.Process(app.pid)
        peak = 0.0
        for _ in range(240):
            peak = max(peak, memory(process))
            state = http.get(f"{base}/projects/{project}/documents/{document}", timeout=5).json()
            if state["status"] == "ready":
                break
            assert state["status"] != "failed", state["error_code"]
            time.sleep(0.5)
        else:
            raise RuntimeError("Document did not become ready")
        for _ in range(90):
            children = [p.name().lower() for p in process.children(recursive=True)]
            if "qdrant.exe" not in children and children.count("python.exe") == 1:
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"Retrieval processes still running after idle timeout: {children}")
        print(
            f"PDF indexing peak {peak:.1f} MiB; after release {memory(process):.1f} MiB", flush=True
        )
        model = post(
            "/models",
            json={
                "name": "Local stub",
                "model": "test",
                "api_key": "fixture",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            },
        )["id"]
        post(
            "/agent/tasks",
            headers={"Idempotency-Key": "lifecycle"},
            json={
                "model_profile_id": model,
                "mode": "autonomous",
                "workflow_kind": "research",
                "project_id": project,
                "objective": "Describe the study design",
                "document_ids": [],
                "research_depth": "quick",
            },
        )
        assert received.wait(15), "Model did not receive the local research request"
        print(f"Research with window open {memory(process):.1f} MiB", flush=True)
        close_window(app.pid)
        time.sleep(5)
        assert app.poll() is None, "Active task was terminated by closing the window"
        print(f"Research in tray {memory(process):.1f} MiB", flush=True)
        release.set()
        app.wait(timeout=60)
        assert app.returncode == 0
    finally:
        release.set()
        http.close()
        server.shutdown()
        server.server_close()
        if app.poll() is None:
            app.kill()
            app.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("models", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="autoscholar-activity-") as temp:
        profile = Path(temp)
        shutil.copytree(args.models, profile / "library/models")
        exercise(args.executable.resolve(), profile, args.pdf)
