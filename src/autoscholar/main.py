from fastapi import FastAPI

from autoscholar import __version__
from autoscholar.api.routes.health import router as health_router
from autoscholar.core.config import Settings, get_settings
from autoscholar.core.errors import register_exception_handlers
from autoscholar.core.logging import configure_logging
from autoscholar.core.middleware import request_context_middleware


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    application = FastAPI(
        title="AutoScholar API",
        description="Autonomous AI/ML research and experiment agent platform",
        version=__version__,
    )
    application.state.settings = resolved_settings
    application.middleware("http")(request_context_middleware)
    register_exception_handlers(application)
    application.include_router(health_router)
    return application


app = create_app()

