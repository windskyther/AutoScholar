"""Owned controller for disposable CI only; never a production entrypoint."""

import os
import sys
from pathlib import Path

from fastapi import FastAPI

from autoscholar.coding.sandbox import DockerSandboxExecutor
from autoscholar.sandbox.manager import create_manager_app


def create_app() -> FastAPI:
    return create_manager_app(
        DockerSandboxExecutor(
            image=os.environ["SANDBOX_IMAGE"],
            dataset_volume=os.environ["SANDBOX_DATASET_VOLUME"],
            dataset_ready_file="/datasets/mnist/.autoscholar-ready",
            evaluation_owner=os.environ["ACCEPTANCE_OWNER"],
        )
    )


if __name__ == "__main__" and sys.argv[1] == "initialize":
    if sys.platform == "win32":
        raise RuntimeError("Volume initialization runs only in the Linux container")
    root = Path("/validation")
    assert root.is_dir()
    for path in (root, root / "documents", root / "workspaces"):
        path.mkdir(exist_ok=True)
        path.chmod(0o750)
        os.chown(path, 10001, 10001)
