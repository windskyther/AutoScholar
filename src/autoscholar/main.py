from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from autoscholar import __version__
from autoscholar.api.routes.chat import router as chat_router
from autoscholar.api.routes.health import router as health_router
from autoscholar.core.config import Settings, get_settings
from autoscholar.core.errors import register_exception_handlers
from autoscholar.core.logging import configure_logging
from autoscholar.core.middleware import request_context_middleware
from autoscholar.infrastructure import Database, RedisClient
from autoscholar.infrastructure.base import ManagedDependency
from autoscholar.llm import LLMProvider, create_llm_provider


def create_app(
    settings: Settings | None = None,
    *,
    database: ManagedDependency | None = None,
    redis: ManagedDependency | None = None,
    llm_provider: LLMProvider | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    resolved_database = database or Database(resolved_settings.database_url)
    resolved_redis = redis or RedisClient(resolved_settings.redis_url)
    resolved_llm_provider = llm_provider or create_llm_provider(resolved_settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.database = resolved_database
        application.state.redis = resolved_redis
        application.state.llm_provider = resolved_llm_provider
        yield
        await resolved_llm_provider.close()
        await resolved_redis.close()
        await resolved_database.close()

    application = FastAPI(
        title="AutoScholar API",
        description="Autonomous AI/ML research and experiment agent platform",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.database = resolved_database
    application.state.redis = resolved_redis
    application.state.llm_provider = resolved_llm_provider
    application.middleware("http")(request_context_middleware)
    register_exception_handlers(application)
    application.include_router(health_router)
    application.include_router(chat_router)
    return application


app = create_app()
