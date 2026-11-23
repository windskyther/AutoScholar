"""Exercise native PDF ingestion and hybrid retrieval using free local model weights."""

import argparse
import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from autoscholar.desktop.retrieval import Retrieval
from autoscholar.desktop.storage import DesktopDatabase
from autoscholar.main import create_app  # noqa: F401 - register all database tables
from autoscholar.rag.repository import KnowledgeRepository


def sample(path: Path) -> None:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for line in (
        "Study design uses a placebo and two groups. "
        "The control group followed a blinded protocol with random allocation "
        "for all participants.",
        "Treatment increased measured accuracy to ninety percent. "
        "The final accuracy was recorded after the last evaluation "
        "and independently checked.",
    ):
        page = writer.add_blank_page(600, 800)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 700 Td ({line}) Tj ET".encode())
        page.replace_contents(stream)
    writer.write(path)


async def run(folder: Path, executable: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    database = DesktopDatabase(folder / "library.sqlite3")
    await database.initialize()
    retrieval = Retrieval(folder, executable)
    try:
        knowledge = KnowledgeRepository(database.session_factory)
        project = await knowledge.create_project(name="PDF acceptance", description=None)
        document_id = str(uuid4())
        documents = folder / "documents"
        documents.mkdir(exist_ok=True)
        pdf = documents / (document_id + ".pdf")
        sample(pdf)
        await knowledge.create_document(
            document_id=document_id,
            project_id=project.id,
            original_filename="test.pdf",
            title="Study result",
            content_type="application/pdf",
            storage_key=pdf.name,
            sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
            size_bytes=pdf.stat().st_size,
        )
        assert await retrieval.call("document")
        record = await knowledge.get_document(project.id, document_id)
        if record is None or record.status != "ready":
            raise RuntimeError(f"Indexing failed: {record.error_code if record else 'missing'}")
        hits = await retrieval.call(
            "retrieve",
            project_id=project.id,
            document_ids=[document_id],
            question="How much did the treatment increase accuracy?",
            retrieval_mode="hybrid_rerank",
            top_k=1,
        )
        assert len(hits) == 1 and hits[0].page == 2 and "ninety percent" in hits[0].content
        print("PDF uploaded, indexed and retrieved with a verified page-2 citation")
    finally:
        retrieval.close()
        await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, type=Path)
    parser.add_argument("--qdrant", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.folder.resolve(), args.qdrant.resolve()))
