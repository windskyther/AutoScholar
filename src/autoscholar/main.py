from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from autoscholar import __version__
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.agent.runner import AgentRunner, AgentService, ResearchSearch, TaskStore
from autoscholar.agent.tools import CalculatorTool, RestrictedPythonTool
from autoscholar.api.routes.agent import router as agent_router
from autoscholar.api.routes.chat import router as chat_router
from autoscholar.api.routes.health import router as health_router
from autoscholar.api.routes.projects import router as projects_router
from autoscholar.api.routes.rag import router as rag_router
from autoscholar.core.config import Settings, get_settings
from autoscholar.core.errors import register_exception_handlers
from autoscholar.core.logging import configure_logging
from autoscholar.core.middleware import request_context_middleware
from autoscholar.core.responses import UTF8JSONResponse
from autoscholar.infrastructure import Database, Qdrant, RedisClient
from autoscholar.infrastructure.base import ManagedDependency
from autoscholar.llm import LLMProvider, create_llm_provider
from autoscholar.rag import (
    ChunkIndex,
    DocumentStorage,
    EmbeddingProvider,
    KnowledgeRepository,
    KnowledgeStore,
    LocalDocumentStorage,
    QdrantChunkIndex,
    RAGQueryService,
    RAGQueryServiceProtocol,
)
from autoscholar.rag.worker import create_embedding_provider
from autoscholar.research import (
    RedisResearchCache,
    ResearchSearchService,
    SemanticScholarSearchProvider,
    TavilySearchProvider,
)


def create_app(
    settings: Settings | None = None,
    *,
    database: ManagedDependency | None = None,
    redis: ManagedDependency | None = None,
    qdrant: ManagedDependency | None = None,
    llm_provider: LLMProvider | None = None,
    agent_repository: TaskStore | None = None,
    agent_runner: AgentService | None = None,
    research_services: list[ResearchSearch] | None = None,
    knowledge_repository: KnowledgeStore | None = None,
    document_storage: DocumentStorage | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    chunk_index: ChunkIndex | None = None,
    rag_query_service: RAGQueryServiceProtocol | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    resolved_database = database or Database(resolved_settings.database_url)
    resolved_redis = redis or RedisClient(resolved_settings.redis_url)
    qdrant_key = (
        resolved_settings.qdrant_api_key.get_secret_value()
        if resolved_settings.qdrant_api_key
        else None
    )
    resolved_qdrant = qdrant or Qdrant(resolved_settings.qdrant_url, api_key=qdrant_key)
    resolved_llm_provider = llm_provider or create_llm_provider(resolved_settings)
    resolved_agent_repository = agent_repository
    resolved_knowledge_repository = knowledge_repository
    if resolved_agent_repository is None and isinstance(resolved_database, Database):
        resolved_agent_repository = AgentTaskRepository(resolved_database.session_factory)
    if resolved_knowledge_repository is None and isinstance(resolved_database, Database):
        resolved_knowledge_repository = KnowledgeRepository(resolved_database.session_factory)
    resolved_document_storage = document_storage or LocalDocumentStorage(
        resolved_settings.document_storage_path
    )
    resolved_embedding_provider = embedding_provider
    resolved_chunk_index = chunk_index
    if resolved_embedding_provider is None and isinstance(resolved_qdrant, Qdrant):
        resolved_embedding_provider = create_embedding_provider(resolved_settings)
    if (
        resolved_chunk_index is None
        and isinstance(resolved_qdrant, Qdrant)
        and resolved_embedding_provider is not None
    ):
        resolved_chunk_index = QdrantChunkIndex(
            resolved_qdrant.client,
            collection=resolved_settings.qdrant_collection,
            dense_dimensions=resolved_embedding_provider.dimensions,
        )
    resolved_research_services = research_services
    if resolved_research_services is None:
        cache = (
            RedisResearchCache(
                resolved_redis,
                ttl_seconds=resolved_settings.research_cache_ttl_seconds,
            )
            if isinstance(resolved_redis, RedisClient)
            else None
        )
        tavily_key = (
            resolved_settings.tavily_api_key.get_secret_value()
            if resolved_settings.tavily_api_key
            else None
        )
        semantic_scholar_key = (
            resolved_settings.semantic_scholar_api_key.get_secret_value()
            if resolved_settings.semantic_scholar_api_key
            else None
        )
        resolved_research_services = [
            ResearchSearchService(
                provider=TavilySearchProvider(
                    api_key=tavily_key,
                    timeout_seconds=resolved_settings.research_timeout_seconds,
                ),
                cache=cache,
            ),
            ResearchSearchService(
                provider=SemanticScholarSearchProvider(
                    api_key=semantic_scholar_key,
                    timeout_seconds=resolved_settings.research_timeout_seconds,
                ),
                cache=cache,
            ),
        ]
    resolved_rag_query_service = rag_query_service
    if (
        resolved_rag_query_service is None
        and resolved_embedding_provider is not None
        and resolved_chunk_index is not None
        and resolved_knowledge_repository is not None
        and resolved_agent_repository is not None
    ):
        resolved_rag_query_service = RAGQueryService(
            provider=resolved_llm_provider,
            embeddings=resolved_embedding_provider,
            index=resolved_chunk_index,
            knowledge=resolved_knowledge_repository,
            tasks=resolved_agent_repository,
            default_top_k=resolved_settings.rag_top_k,
        )
    resolved_agent_runner = agent_runner
    if resolved_agent_runner is None and resolved_agent_repository is not None:
        resolved_agent_runner = AgentRunner(
            provider=resolved_llm_provider,
            repository=resolved_agent_repository,
            tools=[CalculatorTool(), RestrictedPythonTool()],
            research_services=resolved_research_services,
            knowledge_service=resolved_rag_query_service,
        )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.database = resolved_database
        application.state.redis = resolved_redis
        application.state.qdrant = resolved_qdrant
        application.state.llm_provider = resolved_llm_provider
        application.state.agent_repository = resolved_agent_repository
        application.state.agent_runner = resolved_agent_runner
        application.state.knowledge_repository = resolved_knowledge_repository
        application.state.document_storage = resolved_document_storage
        application.state.research_services = resolved_research_services
        application.state.embedding_provider = resolved_embedding_provider
        application.state.chunk_index = resolved_chunk_index
        application.state.rag_query_service = resolved_rag_query_service
        yield
        for service in resolved_research_services:
            await service.close()
        await resolved_llm_provider.close()
        if resolved_embedding_provider is not None:
            await resolved_embedding_provider.close()
        await resolved_redis.close()
        await resolved_qdrant.close()
        await resolved_database.close()

    application = FastAPI(
        title="AutoScholar API",
        description="Autonomous AI/ML research and experiment agent platform",
        version=__version__,
        lifespan=lifespan,
        default_response_class=UTF8JSONResponse,
    )
    application.state.settings = resolved_settings
    application.state.database = resolved_database
    application.state.redis = resolved_redis
    application.state.qdrant = resolved_qdrant
    application.state.llm_provider = resolved_llm_provider
    application.state.agent_repository = resolved_agent_repository
    application.state.agent_runner = resolved_agent_runner
    application.state.knowledge_repository = resolved_knowledge_repository
    application.state.document_storage = resolved_document_storage
    application.state.research_services = resolved_research_services
    application.state.embedding_provider = resolved_embedding_provider
    application.state.chunk_index = resolved_chunk_index
    application.state.rag_query_service = resolved_rag_query_service
    application.middleware("http")(request_context_middleware)
    register_exception_handlers(application)
    application.include_router(health_router)
    application.include_router(chat_router)
    application.include_router(agent_router)
    application.include_router(projects_router)
    application.include_router(rag_router)
    return application


app = create_app()
