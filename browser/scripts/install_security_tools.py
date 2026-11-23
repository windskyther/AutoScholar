"""Install checksum-pinned scanner binaries under ignored data/tooling, never globally."""

import argparse
import hashlib
import io
import json
import platform
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path


def install(name: str, root: Path) -> Path:
    pins = json.loads((root / "security/tools.json").read_text(encoding="utf-8"))["tools"][name]
    if platform.machine().lower() not in {"amd64", "x86_64"}:
        raise ValueError("Scanner pins currently support x86_64 only")
    artifact = pins["platforms"][sys.platform]
    directory = root / "data/tooling" / name / pins["version"]
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / Path(artifact["url"]).name
    if not archive.exists():
        with urllib.request.urlopen(artifact["url"], timeout=60) as response:
            data = response.read(200_000_001)
        if len(data) > 200_000_000 or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise ValueError("Scanner download checksum mismatch")
        archive.write_bytes(data)
    data = archive.read_bytes()
    if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        raise ValueError("Cached scanner checksum mismatch")
    # Extract only the expected executable as bytes; never extract arbitrary archive paths.
    executable = name + (".exe" if sys.platform == "win32" else "")
    if archive.suffix == ".zip":
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            payload = bundle.read(executable)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
            stream = bundle.extractfile(executable)
            if stream is None:
                raise ValueError("Scanner executable missing")
            payload = stream.read()
    path = directory / executable
    path.write_bytes(payload)
    path.chmod(0o755)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", choices=["gitleaks", "trivy"])
    args = parser.parse_args()
    print(install(args.name, Path(__file__).resolve().parents[1]))
