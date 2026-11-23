"""Prove the pinned secret scanner rejects a generated, nonfunctional test credential."""

import argparse
import hashlib
import subprocess
import tempfile
from pathlib import Path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gitleaks", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    (root / "data").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="scanner-negative-", dir=root / "data") as temporary:
        # This is deterministically derived public test data, never a GitHub credential.
        value = "ghp_" + hashlib.sha256(b"public scanner negative fixture").hexdigest()[:36]
        (Path(temporary) / "public.txt").write_text("github_token=" + value, encoding="ascii")
        result = subprocess.run(
            [str(Path(args.gitleaks).resolve()), "dir", "--redact=100", "--no-banner", temporary],
            cwd=root, capture_output=True,
        )
        if result.returncode != 1:
            raise SystemExit("Secret scanner negative control failed")
        print("Generated public sentinel rejected; secret scanner negative control passed")
