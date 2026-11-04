"""Create fresh local secret files, without printing values or changing an existing set."""

import argparse
import json
import secrets
from pathlib import Path


def initialize(directory: Path) -> None:
    if directory.is_symlink():
        raise ValueError("Secret directory cannot be a link")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(directory.iterdir()):
        raise ValueError("Refuse to overwrite an existing secret directory")
    directory.chmod(0o700)
    password, redis, qdrant = (secrets.token_urlsafe(32) for _ in range(3))
    values = {
        "postgres_password": password,
        "database_url": f"postgresql+asyncpg://autoscholar:{password}@postgres:5432/autoscholar",
        "redis_url": f"redis://:{redis}@redis:6379/0",
        "redis_config": (
            f"bind 0.0.0.0\nprotected-mode yes\nrequirepass {redis}\n"
            "appendonly yes\nmaxmemory 512mb\nmaxmemory-policy noeviction\n"
        ),
        "qdrant_key": qdrant,
        "qdrant_config": json.dumps({"service": {"api_key": qdrant}}),
        "api_token": secrets.token_urlsafe(32),
        "sandbox_token": secrets.token_urlsafe(32),
        "llm_api_key": "",
        "embedding_api_key": "",
        "tavily_api_key": "",
        "semantic_scholar_api_key": "",
    }
    for name, value in values.items():
        path = directory / name
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(value + ("\n" if value else ""))
        # The containing directory is private. Explicit per-service mounts must
        # be readable by non-root Linux container users, including file-backed secrets.
        path.chmod(0o644)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path("data/deployment/secrets"))
    args = parser.parse_args()
    initialize(args.directory.resolve())
    print("Created local secret files; provider slots are empty. Values were not printed.")


if __name__ == "__main__":
    main()
