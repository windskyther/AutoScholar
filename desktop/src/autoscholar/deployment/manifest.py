import argparse
import ast
import hashlib
import json
import re
from pathlib import Path


def build_manifest(root: Path, revision: str, expected_lock: str) -> dict[str, str | bool]:
    if not re.fullmatch(r"[a-f0-9]{40}|working-tree", revision):
        raise ValueError("Source revision must be a commit SHA or explicit working-tree build")
    actual = hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_lock) or actual != expected_lock:
        raise ValueError("Dependency lock does not match the build argument")
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in (root / "migrations/versions").glob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id in {"revision", "down_revision"}
                and isinstance(node.value, ast.Constant)
            ):
                value = node.value.value
                if isinstance(value, str):
                    (revisions if node.target.id == "revision" else parents).add(value)
    heads = revisions - parents
    if len(heads) != 1:
        raise ValueError("Production image requires exactly one migration head")
    return {
        "revision": revision,
        "release_source": revision != "working-tree",
        "uv_lock_sha256": actual,
        "schema_revision": heads.pop(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--root", type=Path, default=Path("/app"))
    args = parser.parse_args()
    (args.root / "build-info.json").write_text(
        json.dumps(build_manifest(args.root, args.revision, args.lock), sort_keys=True),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
