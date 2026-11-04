from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from autoscholar.core.admission import RequestLimit, default_request_limits
from autoscholar.core.budget import BudgetLimits
from autoscholar.core.service_credentials import sandbox_credential


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "0.0.0.0"
    app_port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    api_allowed_hosts: list[str] = Field(default_factory=lambda: ["*"])
    api_allowed_origins: list[str] = Field(default_factory=list)
    api_request_limits: dict[str, RequestLimit] = Field(default_factory=default_request_limits)
    api_json_max_bytes: int = Field(default=1_048_576, ge=1024, le=16_777_216)
    api_multipart_overhead_bytes: int = Field(default=1_048_576, ge=1024, le=4_194_304)
    api_response_max_bytes: int = Field(default=8_388_608, ge=1024, le=134_217_728)
    api_body_timeout_seconds: float = Field(default=120, gt=0, le=600)
    api_body_idle_seconds: float = Field(default=15, gt=0, le=60)
    api_send_timeout_seconds: float = Field(default=15, gt=0, le=60)
    api_sse_idle_seconds: float = Field(default=45, gt=0, le=120)
    api_lease_seconds: float = Field(default=30, ge=3, le=120)
    api_control_timeout_seconds: float = Field(default=3, gt=0, le=10)

    @field_validator("api_request_limits", mode="before")
    @classmethod
    def default_admission_limits(cls, value: object) -> object:
        return default_request_limits() if value is None else value

    @field_validator("api_request_limits")
    @classmethod
    def complete_admission_limits(cls, limits: dict[str, RequestLimit]) -> dict[str, RequestLimit]:
        if limits.keys() != default_request_limits().keys():
            raise ValueError("API_REQUEST_LIMITS must specify every documented category")
        return limits

    database_url: str = Field(
        default="postgresql+asyncpg://autoscholar:autoscholar@localhost:5432/autoscholar",
        repr=False,
    )
    redis_url: str = Field(default="redis://localhost:6379/0", repr=False)
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = "autoscholar_chunks_v1"

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    llm_compatibility_profile: Literal["auto", "generic", "deepseek"] = "auto"

    tavily_api_key: SecretStr | None = None
    semantic_scholar_api_key: SecretStr | None = None
    research_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    research_cache_ttl_seconds: int = Field(default=86_400, ge=0, le=604_800)
    research_tool_backend: Literal["native", "mcp"] = "native"
    filesystem_tool_backend: Literal["native", "mcp"] = "native"
    git_tool_backend: Literal["disabled", "mcp"] = "disabled"
    experiment_tool_backend: Literal["native", "mcp"] = "native"
    mcp_experiment_url: str = "http://experiment-mcp:8094/mcp"
    mcp_artifact_root: Path = Path("data/mcp-artifacts")
    mcp_git_url: str = "http://git-mcp:8093/mcp"
    mcp_filesystem_url: str = "http://filesystem-mcp:8092/mcp"
    mcp_research_url: str = "http://research-mcp:8091/mcp"
    mcp_service_token: SecretStr | None = None
    mcp_timeout_seconds: float = Field(default=25, gt=0, le=120)
    experiment_api_token_file: Path | None = None
    llm_api_key_file: Path | None = None
    embedding_api_key_file: Path | None = None
    tavily_api_key_file: Path | None = None
    semantic_scholar_api_key_file: Path | None = None
    qdrant_api_key_file: Path | None = None
    mcp_service_token_file: Path | None = None

    @field_validator(
        "experiment_api_token_file",
        "llm_api_key_file",
        "embedding_api_key_file",
        "tavily_api_key_file",
        "semantic_scholar_api_key_file",
        "qdrant_api_key_file",
        "mcp_service_token_file",
        mode="before",
    )
    @classmethod
    def empty_secret_file_is_unconfigured(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def load_secret_files(self) -> "Settings":
        for name in (
            "experiment_api_token",
            "llm_api_key",
            "embedding_api_key",
            "tavily_api_key",
            "semantic_scholar_api_key",
            "qdrant_api_key",
            "mcp_service_token",
        ):
            path = getattr(self, name + "_file")
            if path is None:
                continue
            direct = getattr(self, name)
            if direct is not None and direct.get_secret_value():
                raise ValueError(f"Configure only {name.upper()} or {name.upper()}_FILE")
            try:
                with path.open("rb") as stream:
                    raw = stream.read(4097)
                if len(raw) > 4096:
                    raise ValueError
                value = raw.decode("utf-8").rstrip("\r\n")
                if not value or any(character.isspace() for character in value):
                    raise ValueError
            except (OSError, UnicodeError, ValueError):
                raise ValueError(
                    f"{name.upper()}_FILE must be a readable bounded secret file"
                ) from None
            setattr(self, name, SecretStr(value))
        return self

    @field_validator("api_allowed_hosts")
    @classmethod
    def validate_api_hosts(cls, hosts: list[str]) -> list[str]:
        if not hosts or any(
            not host
            or any(char in host for char in "/@?#\\ \t\r\n")
            or ("*" in host and host != "*")
            for host in hosts
        ):
            raise ValueError("API_ALLOWED_HOSTS must contain exact hostnames or a development '*'")
        normalized = [host.lower().strip("[]") for host in hosts]
        for host in normalized:
            if ":" in host:
                try:
                    ip_address(host)
                except ValueError:
                    raise ValueError("API_ALLOWED_HOSTS cannot include ports") from None
        return normalized

    @field_validator("api_allowed_origins")
    @classmethod
    def validate_api_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            try:
                parsed = urlsplit(origin)
                _ = parsed.port
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.path
                    or parsed.query
                    or parsed.fragment
                    or "*" in origin
                    or any(char.isspace() or char == "\\" for char in origin)
                ):
                    raise ValueError
            except ValueError:
                raise ValueError("API_ALLOWED_ORIGINS must contain exact HTTP(S) origins") from None
        return origins

    @model_validator(mode="after")
    def validate_mcp_configuration(self) -> "Settings":
        if "mcp" in (
            self.research_tool_backend,
            self.filesystem_tool_backend,
            self.git_tool_backend,
            self.experiment_tool_backend,
        ) and (
            self.mcp_service_token is None or len(self.mcp_service_token.get_secret_value()) < 32
        ):
            raise ValueError("MCP mode requires MCP_SERVICE_TOKEN with at least 32 characters")
        return self

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
    workspace_max_artifact_files: int = Field(default=100, ge=1, le=1_000)
    workspace_max_artifact_file_bytes: int = Field(default=16_777_216, ge=1, le=104_857_600)
    workspace_max_artifact_bytes: int = Field(default=67_108_864, ge=1, le=1_073_741_824)
    sandbox_manager_url: str = "http://sandbox-manager:8090"
    sandbox_manager_token: SecretStr | None = None
    sandbox_manager_token_file: Path | None = None

    @field_validator("sandbox_manager_token_file", mode="before")
    @classmethod
    def empty_sandbox_secret_file(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_sandbox_credential(self) -> "Settings":
        self.sandbox_manager_token = sandbox_credential(
            self.sandbox_manager_token, self.sandbox_manager_token_file
        )
        if self.sandbox_manager_token is not None:
            value = self.sandbox_manager_token.get_secret_value()
            for name in (
                "experiment_api_token",
                "llm_api_key",
                "embedding_api_key",
                "tavily_api_key",
                "semantic_scholar_api_key",
                "qdrant_api_key",
                "mcp_service_token",
            ):
                other = getattr(self, name)
                if other and other.get_secret_value() == value:
                    raise ValueError("Sandbox credentials must be distinct from other credentials")
        return self

    sandbox_timeout_seconds: int = Field(default=300, ge=1, le=600)
    sandbox_max_repairs: int = Field(default=3, ge=0, le=10)
    experiment_timeout_seconds: int = Field(default=600, ge=30, le=600)
    experiment_api_token: SecretStr | None = None
    autonomous_budget: BudgetLimits = Field(default_factory=BudgetLimits)
    workflow_worker_poll_seconds: float = Field(default=2, gt=0, le=60)
    workflow_job_lease_seconds: int = Field(default=30, ge=6, le=300)
    workflow_approval_threshold: int = Field(default=20000, ge=0, le=1200000)

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.app_env != "production":
            return self
        token = self.experiment_api_token.get_secret_value() if self.experiment_api_token else ""
        if (
            len(token) < 32
            or len(set(token)) < 8
            or any(char.isspace() for char in token)
            or any(word in token.casefold() for word in ("changeme", "placeholder", "replace-me"))
        ):
            raise ValueError("Production requires a strong EXPERIMENT_API_TOKEN (32+ characters)")
        if "*" in self.api_allowed_hosts:
            raise ValueError("Production requires explicit API_ALLOWED_HOSTS")
        try:
            database = make_url(self.database_url)
            password = database.password or ""
            if (
                database.get_backend_name() != "postgresql"
                or not database.host
                or len(password) < 16
                or len(set(password)) < 8
                or password.casefold() in {"autoscholar", "password", "changeme"}
                or any(
                    word in password.casefold() for word in ("password", "changeme", "placeholder")
                )
            ):
                raise ValueError
        except (ValueError, ArgumentError):
            raise ValueError(
                "Production requires PostgreSQL with a non-default strong password"
            ) from None
        if password == token:
            raise ValueError("The API access token must be distinct from database credentials")
        for origin in self.api_allowed_origins:
            parsed = urlsplit(origin)
            if parsed.scheme != "https" and parsed.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise ValueError("Production remote origins require HTTPS")
        for name in (
            "llm_api_key",
            "embedding_api_key",
            "tavily_api_key",
            "semantic_scholar_api_key",
            "qdrant_api_key",
            "mcp_service_token",
        ):
            secret = getattr(self, name)
            if secret is not None and secret.get_secret_value() == token:
                raise ValueError("The API access token must be distinct from service credentials")
        return self

    @property
    def experiment_api_configured(self) -> bool:
        return bool(self.experiment_api_token and self.experiment_api_token.get_secret_value())

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
