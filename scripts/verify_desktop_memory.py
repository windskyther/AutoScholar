"""Measure a release install from a dedicated profile with psutil and websocket-client."""

import argparse
import ctypes
import json
import os
import socket
import subprocess
import tempfile
import time
from contextlib import closing
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import psutil
import websocket


def interactive(port: int, process: subprocess.Popen[bytes]) -> None:
    opener = build_opener(ProxyHandler({}))
    for _ in range(180):
        if process.poll() is not None:
            raise RuntimeError(f"Desktop exited during startup: {process.returncode}")
        try:
            pages = json.loads(opener.open(f"http://127.0.0.1:{port}/json/list", timeout=1).read())
            page = next(item for item in pages if item["type"] == "page")
            with closing(
                websocket.create_connection(
                    page["webSocketDebuggerUrl"], suppress_origin=True, timeout=2
                )
            ) as session:
                session.send(
                    json.dumps(
                        {
                            "id": 1,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": "!!document.querySelector('#workspace-content')",
                                "returnByValue": True,
                            },
                        }
                    )
                )
                reply = json.loads(session.recv())
                if reply.get("result", {}).get("result", {}).get("value"):
                    return
        except (OSError, StopIteration, websocket.WebSocketException):
            pass
        time.sleep(0.5)
    raise RuntimeError("Workbench did not connect in WebView2")


def close_window(pid: int) -> None:
    user = ctypes.windll.user32
    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback
    def visit(hwnd: int, _: int) -> bool:
        owner = ctypes.c_ulong()
        user.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
        caption = ctypes.create_unicode_buffer(256)
        user.GetWindowTextW(ctypes.c_void_p(hwnd), caption, len(caption))
        if owner.value == pid and caption.value == "AutoScholar":
            print("Sending WM_CLOSE to the desktop window", flush=True)
            user.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)
        return True

    user.EnumWindows(visit, 0)


def measure(executable: Path, folder: Path, seconds: int) -> dict[str, object]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    start = time.monotonic()
    app = subprocess.Popen(
        [executable],
        cwd=executable.parent,
        env={
            **os.environ,
            "AUTOSCHOLAR_DESKTOP_PROFILE": str(folder),
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={port}",
        },
    )
    try:
        interactive(port, app)
        startup = time.monotonic() - start
        time.sleep(seconds)
        parent = psutil.Process(app.pid)
        tree = [parent, *parent.children(recursive=True)]
        names = [p.name().lower() for p in tree]
        if "msedgewebview2.exe" not in names or "python.exe" not in names or "qdrant.exe" in names:
            raise RuntimeError(f"Unexpected idle process tree: {names}")
        memory = sum(p.memory_full_info().uss for p in tree) / (1024**2)
        print(
            f"Measured {memory:.1f} MiB, {len(tree)} processes; closing PID {app.pid}", flush=True
        )
        close_window(app.pid)
        app.wait(timeout=30)
        _, remaining = psutil.wait_procs(tree, timeout=10)
        return {
            "interactive_seconds": round(startup, 2),
            "private_working_set_mib": round(memory, 1),
            "process_count": len(tree),
            "remaining": [p.name() for p in remaining],
        }
    finally:
        if app.poll() is None:
            app.kill()
            app.wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(
        prefix="autoscholar-memory-", ignore_cleanup_errors=True
    ) as profile:
        for _ in range(args.runs):
            result = measure(args.executable.resolve(), Path(profile), args.seconds)
            print(json.dumps(result), flush=True)
            if result["private_working_set_mib"] > 500 or result["remaining"]:
                raise SystemExit("Desktop resource acceptance failed")


if __name__ == "__main__":
    main()
