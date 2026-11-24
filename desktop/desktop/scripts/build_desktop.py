"""Assemble the locked Windows runtime and a current-user NSIS installer."""

import argparse
import hashlib
import importlib.metadata as metadata
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"


def copy_runtime(destination: Path) -> None:
    base = Path(sys.base_prefix)
    for file in [base / "python.exe", base / "LICENSE.txt", *base.glob("*.dll")]:
        shutil.copy2(file, destination)
    shutil.copytree(base / "DLLs", destination / "DLLs")
    shutil.copytree(
        base / "Lib",
        destination / "Lib",
        ignore=shutil.ignore_patterns("site-packages", "__pycache__", "test", "idlelib"),
    )
    site = Path(sysconfig.get_path("purelib")).resolve()
    target = destination / "Lib" / "site-packages"
    target.mkdir()
    requirements = subprocess.check_output(
        [
            "uv",
            "export",
            "--frozen",
            "--offline",
            "--no-default-groups",
            "--no-emit-project",
            "--no-hashes",
            "--format",
            "requirements.txt",
        ],
        cwd=ROOT,
        text=True,
        encoding="utf8",
    )
    for line in requirements.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        req = Requirement(line)
        if req.marker and not req.marker.evaluate():
            continue
        dist = metadata.distribution(req.name)
        if dist.version not in req.specifier or not dist.files:
            raise ValueError(f"Sync the locked environment before packaging {req.name}")
        for rel in dist.files:
            src = Path(dist.locate_file(rel)).resolve()
            if not src.is_relative_to(site) or src.suffix == ".pyc" or not src.is_file():
                continue
            dest = target / src.relative_to(site)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    shutil.copytree(
        ROOT / "src/autoscholar",
        target / "autoscholar",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(ROOT / "frontend/dist", destination / "web")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell", required=True, type=Path)
    parser.add_argument("--makensis", required=True, type=Path)
    parser.add_argument(
        "--default-root", type=Path, help="Parent for suggested program and data folders"
    )
    args = parser.parse_args()
    if args.default_root and not args.default_root.is_absolute():
        parser.error("--default-root requires an absolute path")
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12):
        parser.error("Run with the locked Windows x64 Python 3.12 environment")
    output = DESKTOP / "target"
    with tempfile.TemporaryDirectory(prefix="bundle-", dir=output) as tmp:
        stage = Path(tmp)
        runtime = stage / "runtime"
        runtime.mkdir()
        copy_runtime(runtime)
        shutil.copy2(DESKTOP / "runtime/qdrant.exe", runtime)
        shutil.copy2(args.shell, stage / "AutoScholar.exe")
        # GNU builds may import MinGW runtime DLLs. These are private to the desktop shell.
        for name in ("libgcc_s_seh-1.dll", "libwinpthread-1.dll", "libstdc++-6.dll"):
            dll = shutil.which(name)
            if dll:
                shutil.copy2(dll, stage)
        for sidecar in args.shell.parent.glob("WebView2Loader.dll"):
            shutil.copy2(sidecar, stage)
        subprocess.run(
            [
                runtime / "python.exe",
                "-I",
                "-c",
                "from autoscholar.desktop.runtime import desktop_app; import sys; "
                "assert 'onnxruntime' not in sys.modules; import fastembed, aiosqlite",
            ],
            check=True,
        )
        installer = output / "AutoScholar-Setup-x64.exe"
        candidate = output / (stage.name + ".exe")
        subprocess.run(
            [
                args.makensis.resolve(),
                "/V2",
                "/INPUTCHARSET",
                "UTF8",
                *([f"/DDEFAULT_ROOT={args.default_root}"] if args.default_root else []),
                f"/DSTAGE={stage}",
                f"/DOUTPUT={candidate}",
                DESKTOP / "installer.nsi",
            ],
            check=True,
        )
        candidate.replace(installer)
        with installer.open("rb") as built:
            print(f"{installer}  SHA256 {hashlib.file_digest(built, 'sha256').hexdigest()}")


if __name__ == "__main__":
    main()
