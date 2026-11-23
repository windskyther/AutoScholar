import asyncio
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from autoscholar.desktop.secrets import protect
from autoscholar.llm.profiles import ModelProfileRow, ModelProfiles


def atomic_write(path: Path, data: bytes) -> None:
    with NamedTemporaryFile(dir=path.parent, delete=False) as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(file.name, path)


def library_folder(preferences: Path) -> Path:
    pointer = preferences / "library.json"
    return Path(json.loads(pointer.read_bytes())) if pointer.exists() else preferences / "library"


def search_keys(folder: Path) -> tuple[SecretStr | None, SecretStr | None]:
    file = folder / "search.dpapi"
    keys = json.loads(protect(file.read_bytes(), decrypt=True)) if file.exists() else {}
    return (
        SecretStr(keys["web"]) if keys.get("web") else None,
        SecretStr(keys["paper"]) if keys.get("paper") else None,
    )


class Preferences(BaseModel):
    web: SecretStr | None = None
    paper: SecretStr | None = None
    profile: str | None = None
    profile_key: SecretStr | None = None
    directory: str = Field(default="", max_length=2048)
    package: str = Field(default="", max_length=2048)


def install_settings(app: FastAPI, folder: Path) -> None:
    lock = asyncio.Lock()

    @app.get("/api/desktop/settings")
    async def read() -> dict[str, object]:
        web, paper = search_keys(folder)
        profiles: ModelProfiles = app.state.backend.state.model_profiles
        async with profiles.sessions() as session:
            missing = (
                await session.execute(
                    select(ModelProfileRow.id, ModelProfileRow.name).where(
                        ModelProfileRow.encrypted_key == ""
                    )
                )
            ).all()
        return {
            "directory": str(folder),
            "web": bool(web),
            "paper": bool(paper),
            "profiles": [{"id": row.id, "name": row.name} for row in missing],
        }

    @app.post("/api/desktop/settings")
    async def save(payload: Preferences, request: Request) -> dict[str, bool]:
        from autoscholar.desktop.transfer import import_library

        if lock.locked():
            raise HTTPException(409, "Settings operation in progress")
        async with lock:
            request.app.state.importing = True
            try:
                if payload.profile or payload.profile_key:
                    if (
                        not payload.profile
                        or not payload.profile_key
                        or payload.directory
                        or payload.package
                    ):
                        raise ValueError("Select one imported profile to reconnect")
                    profiles: ModelProfiles = app.state.backend.state.model_profiles
                    async with profiles.lock, profiles.sessions.begin() as session:
                        row = await session.get(ModelProfileRow, payload.profile)
                        if row is None or row.encrypted_key:
                            raise ValueError("This connection already has a credential")
                        row.encrypted_key = (
                            profiles.cipher()
                            .encrypt(payload.profile_key.get_secret_value().encode())
                            .decode()
                        )
                        row.enabled = True
                preferences = request.app.state.preferences
                destination = folder
                if payload.directory or payload.package:
                    base = Path(payload.directory) if payload.directory else folder.parent
                    if not base.is_absolute() or str(base).startswith("\\\\"):
                        raise ValueError("Select a local absolute directory")
                    base.mkdir(exist_ok=True, parents=True)
                    if payload.package:
                        destination = await import_library(Path(payload.package), base)
                    else:
                        destination = base / "AutoScholarLibrary"
                        destination.mkdir(exist_ok=True)
                        if any(destination.iterdir()):
                            raise ValueError("Select an empty library destination")
                web, paper = search_keys(destination)
                keys = {
                    name: key.get_secret_value()
                    for name, key in (
                        ("web", payload.web or web),
                        ("paper", payload.paper or paper),
                    )
                    if key
                }
                if keys:
                    atomic_write(destination / "search.dpapi", protect(json.dumps(keys).encode()))
                if destination != folder:
                    atomic_write(
                        preferences / "library.json", json.dumps(str(destination)).encode()
                    )
                return {"restart": True}
            except Exception as exc:
                raise HTTPException(422, "Settings or import verification failed") from exc
            finally:
                request.app.state.importing = False
