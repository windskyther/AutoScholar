from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi.testclient import TestClient

from autoscholar.core.config import Settings
from autoscholar.main import create_app
from autoscholar.rag import DuplicateDocumentError, LocalDocumentStorage
from autoscholar.rag.models import (
    DocumentJobKind,
    DocumentRecord,
    DocumentStatus,
    ProjectRecord,
)
from tests.test_chat import SuccessfulProvider
from tests.test_health import FakeDependency


class FakeKnowledgeStore:
    def __init__(self) -> None:
        self.projects: dict[str, ProjectRecord] = {}
        self.documents: dict[str, DocumentRecord] = {}

    async def create_project(self, *, name: str, description: str | None) -> ProjectRecord:
        now = datetime.now(UTC)
        record = ProjectRecord(
            id=str(uuid4()),
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
        )
        self.projects[record.id] = record
        return record

    async def get_project(self, project_id: str) -> ProjectRecord | None:
        return self.projects.get(project_id)

    async def list_projects(
        self, *, limit: int, offset: int
    ) -> tuple[list[ProjectRecord], int]:
        items = list(self.projects.values())
        return items[offset : offset + limit], len(items)

    async def create_document(
        self,
        *,
        document_id: str,
        project_id: str,
        original_filename: str,
        title: str,
        content_type: str,
        storage_key: str,
        sha256: str,
        size_bytes: int,
    ) -> DocumentRecord:
        if any(
            item.project_id == project_id and item.sha256 == sha256
            for item in self.documents.values()
        ):
            raise DuplicateDocumentError
        now = datetime.now(UTC)
        record = DocumentRecord(
            id=document_id,
            project_id=project_id,
            original_filename=original_filename,
            title=title,
            content_type=content_type,
            storage_key=storage_key,
            sha256=sha256,
            size_bytes=size_bytes,
            status="queued",
            page_count=None,
            chunk_count=0,
            embedding_model=None,
            index_version=0,
            error_code=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        self.documents[record.id] = record
        return record

    async def get_document(
        self, project_id: str, document_id: str
    ) -> DocumentRecord | None:
        record = self.documents.get(document_id)
        return record if record is not None and record.project_id == project_id else None

    async def list_documents(
        self, project_id: str, *, limit: int, offset: int
    ) -> tuple[list[DocumentRecord], int]:
        items = [item for item in self.documents.values() if item.project_id == project_id]
        return items[offset : offset + limit], len(items)

    async def enqueue_document_job(
        self, document_id: str, *, kind: DocumentJobKind
    ) -> DocumentRecord:
        record = self.documents[document_id]
        status = "deleting" if kind == "delete" else "queued"
        updated = replace(
            record,
            status=cast(DocumentStatus, status),
            updated_at=datetime.now(UTC),
        )
        self.documents[document_id] = updated
        return updated


def create_client(tmp_path: Path) -> tuple[TestClient, FakeKnowledgeStore]:
    store = FakeKnowledgeStore()
    client = TestClient(
        create_app(
            Settings(llm_api_key=None, llm_model=None),
            database=FakeDependency(),
            redis=FakeDependency(),
            qdrant=FakeDependency(),
            llm_provider=SuccessfulProvider(),
            knowledge_repository=store,
            document_storage=LocalDocumentStorage(tmp_path),
        )
    )
    return client, store


def test_project_upload_list_and_content_round_trip(tmp_path: Path) -> None:
    client, _ = create_client(tmp_path)
    with client:
        project_response = client.post(
            "/projects", json={"name": "  LoRA Study  ", "description": "Adapters"}
        )
        project_id = project_response.json()["id"]
        upload = client.post(
            f"/projects/{project_id}/documents",
            files={"file": ("paper.pdf", b"%PDF-1.4\nbody", "application/pdf")},
            data={"title": "LoRA Paper"},
        )
        document_id = upload.json()["id"]
        listed = client.get(f"/projects/{project_id}/documents")
        content = client.get(f"/projects/{project_id}/documents/{document_id}/content")

    assert project_response.status_code == 201
    assert project_response.json()["name"] == "LoRA Study"
    assert upload.status_code == 202
    assert upload.headers["location"].endswith(document_id)
    assert upload.json()["status"] == "queued"
    assert listed.json()["total"] == 1
    assert content.status_code == 200
    assert content.content == b"%PDF-1.4\nbody"
    assert content.headers["content-type"] == "application/pdf"


def test_upload_rejects_duplicates_and_non_pdf_content(tmp_path: Path) -> None:
    client, _ = create_client(tmp_path)
    with client:
        project_id = client.post("/projects", json={"name": "Test"}).json()["id"]
        first = client.post(
            f"/projects/{project_id}/documents",
            files={"file": ("paper.pdf", b"%PDF-1.4\nbody", "application/pdf")},
        )
        duplicate = client.post(
            f"/projects/{project_id}/documents",
            files={"file": ("copy.pdf", b"%PDF-1.4\nbody", "application/pdf")},
        )
        invalid = client.post(
            f"/projects/{project_id}/documents",
            files={"file": ("fake.pdf", b"not pdf", "application/pdf")},
        )

    assert first.status_code == 202
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "duplicate_document"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_pdf"


def test_project_boundaries_hide_other_project_documents(tmp_path: Path) -> None:
    client, _ = create_client(tmp_path)
    with client:
        first_project = client.post("/projects", json={"name": "First"}).json()["id"]
        second_project = client.post("/projects", json={"name": "Second"}).json()["id"]
        document_id = client.post(
            f"/projects/{first_project}/documents",
            files={"file": ("paper.pdf", b"%PDF-1.4\nbody", "application/pdf")},
        ).json()["id"]
        response = client.get(f"/projects/{second_project}/documents/{document_id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "document_not_found"
