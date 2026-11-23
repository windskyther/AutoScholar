"""Versioned, read-only export of a quiescent workbench and staged desktop import."""

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import IO, Any
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import DateTime, Numeric, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from autoscholar.agent.database_models import Base
from autoscholar.core.config import get_settings
from autoscholar.desktop.storage import DesktopDatabase
from autoscholar.experiment.artifacts import ArtifactManager
from autoscholar.infrastructure import Database
from autoscholar.main import create_app  # noqa: F401 - register the shared relational schema
from autoscholar.rag.database_models import DocumentJobRow


def encode_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported database value: {type(value).__name__}")


ACTIVE = ("queued", "running", "pause_requested", "cancel_requested")


def transfer_file(source: IO[bytes], target: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while block := source.read(1024 * 1024):
        digest.update(block)
        target.write(block)
    return digest.hexdigest()


def member(table: str, row: dict[str, Any]) -> str | None:
    if table == "documents":
        key = str(row["storage_key"])
        if not re.fullmatch(r"[a-fA-F0-9-]{36}\.pdf", key):
            raise ValueError("Invalid PDF storage key")
        return "documents/" + key
    if table == "artifacts":
        if (
            not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", row["task_id"])
            or row["path"] not in ArtifactManager.allowed
        ):
            raise ValueError("Invalid artifact path")
        return f"workspaces/{row['task_id']}/{row['path']}"
    return None


async def export(
    connection: AsyncConnection, output: Path, documents: Path, workspaces: Path
) -> None:
    if connection.dialect.name == "postgresql":
        await connection.exec_driver_sql(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )
    if await connection.scalar(
        text(
            "SELECT count(*) FROM workflow_jobs WHERE status IN "
            "('queued','running','pause_requested','cancel_requested')"
        )
    ):
        raise ValueError("Pause all active tasks before export")
    if await connection.scalar(
        text("SELECT count(*) FROM document_jobs WHERE status IN ('queued','processing')")
    ):
        raise ValueError("Wait for document processing before export")
    tables: dict[str, Any] = {}
    saved: dict[str, str] = {}
    with ZipFile(output, "x", ZIP_DEFLATED, allowZip64=True) as archive:
        for table in Base.metadata.sorted_tables:
            rows = await connection.stream(select(table))
            count = 0
            digest = hashlib.sha256()
            files = []
            with archive.open(f"tables/{table.name}.jsonl", "w", force_zip64=True) as stream:
                async for record in rows.mappings():
                    row = dict(record)
                    if table.name == "model_profiles":
                        row.update(encrypted_key="", enabled=False)
                    if table.name == "project_claims":
                        row.update(indexed_model=None, indexed_version=0)
                    name_in_zip = member(table.name, row)
                    if name_in_zip:
                        files.append((name_in_zip, row["sha256"]))
                    line = (
                        json.dumps(row, ensure_ascii=False, default=encode_value) + "\n"
                    ).encode()
                    stream.write(line)
                    digest.update(line)
                    count += 1
            tables[table.name] = {"count": count, "sha256": digest.hexdigest()}
            for name, expected in files:
                if name not in saved:
                    prefix, relative = name.split("/", 1)
                    root = documents if prefix == "documents" else workspaces
                    path = (root / relative).resolve(strict=True)
                    if not path.is_relative_to(root.resolve()):
                        raise ValueError("File outside library")
                    with path.open("rb") as src, archive.open(name, "w", force_zip64=True) as dst:
                        saved[name] = transfer_file(src, dst)
                if saved[name] != expected:
                    raise ValueError("Source file checksum mismatch")
        archive.writestr(
            "manifest.json", json.dumps({"version": 1, "tables": tables, "files": saved})
        )


async def import_library(package: Path, parent: Path) -> Path:
    staging = Path(tempfile.mkdtemp(prefix=".import-", dir=parent))
    db = DesktopDatabase(staging / "library.sqlite3")
    try:
        await db.initialize()
        with ZipFile(package) as archive, archive.open("manifest.json") as manifest:
            info = json.load(manifest)
            if info["version"] != 1 or set(info["tables"]) != set(Base.metadata.tables):
                raise ValueError("Unsupported export schema")
            files: dict[str, str] = {}
            async with db.session_factory.begin() as session:
                await session.execute(text("PRAGMA defer_foreign_keys=ON"))
                for table in Base.metadata.sorted_tables:
                    digest = hashlib.sha256()
                    with archive.open(f"tables/{table.name}.jsonl") as stream:
                        for line in stream:
                            digest.update(line)
                            row = json.loads(line)
                            if set(row) != set(table.columns.keys()):
                                raise ValueError("Unexpected columns")
                            if table.name == "model_profiles":
                                row.update(encrypted_key="", enabled=False)
                            for col in table.columns:
                                if isinstance(col.type, DateTime) and row[col.name]:
                                    row[col.name] = datetime.fromisoformat(row[col.name])
                                elif isinstance(col.type, Numeric) and row[col.name] is not None:
                                    row[col.name] = Decimal(row[col.name])
                            name_in_zip = member(table.name, row)
                            if name_in_zip:
                                files[name_in_zip] = row["sha256"]
                            await session.execute(table.insert(), row)
                    total = await session.scalar(select(func.count()).select_from(table))
                    if info["tables"][table.name] != {"count": total, "sha256": digest.hexdigest()}:
                        raise ValueError("Relational data verification failed")
                if files != info["files"]:
                    raise ValueError("Unexpected file manifest")
                for name, expected in files.items():
                    path = staging / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as src, path.open("xb") as dst:
                        if transfer_file(src, dst) != expected:
                            raise ValueError("File verification failed")
                ready = await session.scalars(text("SELECT id FROM documents WHERE status='ready'"))
                for document_id in list(ready):
                    await session.execute(
                        insert(DocumentJobRow),
                        {
                            "id": str(uuid4()),
                            "document_id": document_id,
                            "kind": "reindex",
                            "status": "queued",
                            "attempts": 0,
                            "available_at": datetime.now(UTC),
                        },
                    )
                await session.execute(
                    text("UPDATE documents SET status='queued' WHERE status='ready'")
                )
        await db.close()
        destination = parent / ("library-" + uuid4().hex)
        staging.rename(destination)
        return destination
    finally:
        await db.close()
        if staging.exists():
            shutil.rmtree(staging)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Export a paused AutoScholar workbench")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    settings = get_settings()
    database = Database(settings.database_url)
    try:
        async with database.session_factory() as session:
            await export(
                await session.connection(),
                args.output,
                settings.document_storage_path,
                settings.workspace_root,
            )
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
