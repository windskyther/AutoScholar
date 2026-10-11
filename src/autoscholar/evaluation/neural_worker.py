"""Persistent local FastEmbed inference; fixed public models, cache-only, no Settings."""

import asyncio
import hashlib
import math
import os
import re
import sys
from collections import Counter
from importlib import import_module
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from autoscholar.evaluation.datasets import decode_json, payload_digest
from autoscholar.evaluation.models import EvaluationModel, Identifier
from autoscholar.rag.embeddings import (
    FastEmbedProvider,
    FastEmbedReranker,
    FastEmbedSparseProvider,
)
from autoscholar.rag.models import RetrievedChunk

DENSE_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
SPARSE_MODEL = "Qdrant/bm25"
RERANKER_MODEL = "BAAI/bge-reranker-base"
MODEL_REPOSITORIES = {
    "dense_model": "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q",
    "sparse_model": "Qdrant/bm25",
    "reranker_model": "BAAI/bge-reranker-base",
}
MAX_PACKET_BYTES = 2 * 1024 * 1024
Method = Literal[
    "identity",
    "verify",
    "dense_documents",
    "sparse_documents",
    "dense_query",
    "sparse_query",
    "rerank",
]


class NeuralRequest(EvaluationModel):
    request_id: int = Field(ge=1, le=10000)
    method: Method
    payload: dict[str, object] = Field(default_factory=dict, max_length=3)


class NeuralResponse(EvaluationModel):
    request_id: int = Field(ge=1, le=10000)
    ok: bool
    data: object = None
    counts: dict[Identifier, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    error: Literal["invalid_request", "cache_not_ready", "inference_failed"] | None = None


class Texts(EvaluationModel):
    texts: list[Annotated[str, Field(min_length=1, max_length=8192)]] = Field(
        min_length=1, max_length=64
    )


class Query(EvaluationModel):
    query: str = Field(min_length=1, max_length=10000)


class RerankRequest(Query):
    chunks: list[RetrievedChunk] = Field(min_length=1, max_length=32)
    limit: int = Field(ge=1, le=20)


def inference_code_digest() -> str:
    return payload_digest(
        {
            "worker": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "embeddings": hashlib.sha256(
                (Path(__file__).parents[1] / "rag/embeddings.py").read_bytes()
            ).hexdigest(),
        }
    )


def ensure_isolation() -> None:
    if os.name != "posix" or getattr(os, "getuid", lambda: -1)() != 65532:
        raise ValueError("Run neural models in the dedicated non-root inference container")
    if Path("/var/run/docker.sock").exists() or Path("/app/.env").exists():
        raise ValueError("Worker must not receive Docker socket or application configuration")
    if {path.name for path in Path("/sys/class/net").iterdir()} != {"lo"}:
        raise ValueError("Worker must have no external network interface")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    if int(status["CapEff"].strip(), 16) != 0 or status["NoNewPrivs"].strip() != "1":
        raise ValueError("Worker privileges are not restricted")
    mounts = [line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    if any(
        not any(line[4] == mount and "ro" in line[5].split(",") for line in mounts)
        for mount in ("/", "/models", "/app/src")
    ):
        raise ValueError("Model/source/root mounts must be read-only")


def cached_snapshot(cache: Path, repository: str) -> tuple[Path, str]:
    """Pin a single cached revision and hash actual tokenizer/weights; never download."""
    root = cache.resolve()
    directory = root / ("models--" + repository.replace("/", "--"))
    reference = directory / "refs/main"
    if not reference.is_file() or reference.stat().st_size > 128:
        raise ValueError("Public model reference is missing")
    revision = reference.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid cached model revision")
    snapshot = directory / "snapshots" / revision
    if not snapshot.resolve().is_relative_to(root) or not snapshot.is_dir():
        raise ValueError("Cached model escaped its volume")
    files = sorted(path for path in snapshot.rglob("*") if path.is_file())
    if not files or len(files) > 64:
        raise ValueError("Model file manifest is empty or too large")
    digests = {}
    total = 0
    for path in files:
        if not path.resolve().is_relative_to(root):
            raise ValueError("Model file escaped its cache")
        total += path.stat().st_size
        if total > 3 * 1024**3:
            raise ValueError("Cached model exceeds evaluation size bound")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        digests[path.relative_to(snapshot).as_posix()] = digest.hexdigest()
    return snapshot, payload_digest(
        {"repository": repository, "revision": revision, "files": digests}
    )


class NeuralEngine:
    def __init__(self, cache: Path = Path("/models")) -> None:
        self.cache = cache
        self.counts: Counter[str] = Counter()
        self.dense: FastEmbedProvider | None = None
        self.sparse: FastEmbedSparseProvider | None = None
        self.reranker: FastEmbedReranker | None = None
        self.resources: dict[str, str] = {}

    async def initialize(self) -> dict[str, object]:
        ensure_isolation()
        if any(
            os.getenv(key)
            for key in (
                "LLM_API_KEY",
                "TAVILY_API_KEY",
                "OPENAI_API_KEY",
                "EMBEDDING_API_KEY",
                "SEMANTIC_SCHOLAR_API_KEY",
                "HF_TOKEN",
                "HUGGING_FACE_HUB_TOKEN",
            )
        ):
            raise ValueError("Inference worker must not receive provider credentials")
        paths = {}
        for key, repository in MODEL_REPOSITORIES.items():
            paths[key], self.resources[key] = cached_snapshot(self.cache, repository)
        self.resources["inference_code"] = inference_code_digest()
        self.dense = FastEmbedProvider(
            model=DENSE_MODEL,
            dimensions=384,
            cache_dir=self.cache,
            local_files_only=True,
            model_path=paths["dense_model"],
            threads=2,
        )
        self.sparse = FastEmbedSparseProvider(
            model=SPARSE_MODEL,
            cache_dir=self.cache,
            local_files_only=True,
            model_path=paths["sparse_model"],
            threads=2,
        )
        self.reranker = FastEmbedReranker(
            model=RERANKER_MODEL,
            cache_dir=self.cache,
            local_files_only=True,
            model_path=paths["reranker_model"],
            threads=2,
        )
        # Load all actual models now; lack of cached files fails before benchmark execution.
        await self.dense._get_embedding()
        await self.sparse._get_embedding()
        await self.reranker._get_reranker()
        if import_module("onnxruntime").get_device() != "CPU":
            raise ValueError("Evaluation requires the pinned CPU inference runtime")
        await self.dense.embed_query("Public offline model readiness")
        await self.sparse.embed_query("Public offline model readiness")
        await self.reranker.rerank(
            "Public offline model readiness",
            [
                RetrievedChunk(
                    id="readiness",
                    project_id="public",
                    document_id="public",
                    title="Public",
                    page=1,
                    section=None,
                    content="Public offline model readiness",
                    score=0.0,
                    ordinal=0,
                )
            ],
            limit=1,
        )
        return {
            "resources": self.resources,
            "models": [DENSE_MODEL, SPARSE_MODEL, RERANKER_MODEL],
            "snapshot_revisions": {key: path.name for key, path in paths.items()},
            "fastembed": version("fastembed"),
            "onnxruntime": version("onnxruntime"),
            "cpu_only": True,
            "local_files_only": True,
            "setup_inference_calls": {"dense_query": 1, "sparse_query": 1, "rerank": 1},
            "isolation_verified": True,
        }

    async def dispatch(self, request: NeuralRequest) -> object:
        if request.method == "identity":
            if request.payload or self.dense is not None:
                raise ValueError("Identity handshake must run once with no payload")
            return await self.initialize()
        if self.dense is None or self.sparse is None or self.reranker is None:
            raise ValueError("Models were not initialized")
        if request.method == "verify":
            if request.payload:
                raise ValueError("Cache verification accepts no payload")
            current = {
                key: cached_snapshot(self.cache, repo)[1]
                for key, repo in MODEL_REPOSITORIES.items()
            }
            current["inference_code"] = inference_code_digest()
            return {"cache_and_code_unchanged": current == self.resources}
        self.counts[request.method] += 1
        if request.method == "dense_documents":
            return await self.dense.embed_documents(Texts.model_validate(request.payload).texts)
        if request.method == "dense_query":
            return await self.dense.embed_query(Query.model_validate(request.payload).query)
        if request.method in ("sparse_documents", "sparse_query"):
            if request.method == "sparse_documents":
                vectors = await self.sparse.embed_documents(
                    Texts.model_validate(request.payload).texts
                )
            else:
                vectors = [
                    await self.sparse.embed_query(Query.model_validate(request.payload).query)
                ]
            encoded = [{"indices": vector.indices, "values": vector.values} for vector in vectors]
            return encoded if request.method == "sparse_documents" else encoded[0]
        # Strict dataclass fields are accepted in JSON mode, not as arbitrary Python dicts.
        import json

        rerank = RerankRequest.model_validate_json(json.dumps(request.payload, allow_nan=False))
        chunks = await self.reranker.rerank(rerank.query, rerank.chunks, limit=rerank.limit)
        if any(not math.isfinite(chunk.score) for chunk in chunks):
            raise ValueError("Non-finite neural reranker scores")
        return [{"id": chunk.id, "score": chunk.score} for chunk in chunks]


def main() -> None:
    engine = NeuralEngine()
    while raw := sys.stdin.buffer.readline(MAX_PACKET_BYTES + 1):
        if len(raw) > MAX_PACKET_BYTES or not raw.endswith(b"\n"):
            break
        request_id = 1
        try:
            request = NeuralRequest.model_validate(decode_json(raw))
            request_id = request.request_id
            data = asyncio.run(engine.dispatch(request))
            response = NeuralResponse(
                request_id=request_id, ok=True, data=data, counts=dict(engine.counts)
            )
        except Exception:
            response = NeuralResponse(
                request_id=request_id,
                ok=False,
                counts=dict(engine.counts),
                error="cache_not_ready" if engine.dense is None else "inference_failed",
            )
        output = response.model_dump_json().encode("utf-8") + b"\n"
        if len(output) > MAX_PACKET_BYTES:
            break
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
