from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "0.0.0.0"
    app_port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    database_url: str = "postgresql+asyncpg://autoscholar:autoscholar@localhost:5432/autoscholar"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = "autoscholar_chunks_v1"

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=600)

    tavily_api_key: SecretStr | None = None
    semantic_scholar_api_key: SecretStr | None = None
    research_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    research_cache_ttl_seconds: int = Field(default=86_400, ge=0, le=604_800)

    embedding_provider: Literal["fastembed", "openai_compatible"] = "fastembed"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: SecretStr | None = None
    embedding_dimensions: int = Field(default=384, ge=1, le=65_536)
    sparse_model: str = "Qdrant/bm25"
    reranker_model: str = "BAAI/bge-reranker-base"
    model_cache_path: Path = Path("data/models")

    document_storage_path: Path = Path("data/documents")
    document_max_bytes: int = Field(default=52_428_800, ge=1, le=1_073_741_824)
    document_max_pages: int = Field(default=500, ge=1, le=10_000)
    document_parse_timeout_seconds: int = Field(default=120, ge=1, le=3_600)

    rag_chunk_tokens: int = Field(default=96, ge=32, le=2_048)
    rag_chunk_overlap: int = Field(default=16, ge=0, le=512)
    rag_top_k: int = Field(default=8, ge=1, le=50)
    rag_candidate_limit: int = Field(default=30, ge=1, le=200)
    rag_worker_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    rag_job_lease_seconds: int = Field(default=600, ge=30, le=7_200)
    rag_job_max_attempts: int = Field(default=3, ge=1, le=10)

    workspace_root: Path = Path("data/workspaces")
    workspace_max_files: int = Field(default=100, ge=1, le=1_000)
    workspace_max_file_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760)
    workspace_max_source_bytes: int = Field(default=10_485_760, ge=1, le=104_857_600)
    sandbox_manager_url: str = "http://sandbox-manager:8090"
    sandbox_timeout_seconds: int = Field(default=300, ge=1, le=600)
    sandbox_max_repairs: int = Field(default=3, ge=0, le=10)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_api_key.get_secret_value() and self.llm_model)

    @property
    def web_search_configured(self) -> bool:
        return bool(self.tavily_api_key and self.tavily_api_key.get_secret_value())

    @property
    def embedding_configured(self) -> bool:
        if self.embedding_provider == "fastembed":
            return bool(self.embedding_model.strip())
        return bool(
            self.embedding_model.strip()
            and self.embedding_base_url.strip()
            and self.embedding_api_key
            and self.embedding_api_key.get_secret_value()
        )

    @property
    def rag_configured(self) -> bool:
        return bool(self.qdrant_url.strip() and self.embedding_configured)


@lru_cache
def get_settings() -> Settings:
    return Settings()
