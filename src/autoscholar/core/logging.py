import logging
import logging.config

import structlog

from autoscholar.core.config import Settings
from autoscholar.core.redaction import configure_redaction, safe_log_event


def configure_logging(log_level: str, *, settings: Settings | None = None) -> None:
    """Configure standard logging and structlog for JSON output."""

    if settings is not None:
        configure_redaction(settings)
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "processors": [safe_log_event, structlog.processors.JSONRenderer()],
                    "keep_exc_info": False,
                    "keep_stack_info": False,
                    "foreign_pre_chain": shared_processors,
                }
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["default"], "level": log_level},
            "loggers": {
                "uvicorn.error": {"handlers": [], "propagate": True},
                "uvicorn.access": {"handlers": [], "propagate": False},
                "httpx": {"level": "WARNING", "handlers": [], "propagate": True},
                "httpcore": {"level": "WARNING", "handlers": [], "propagate": True},
            },
        }
    )

    structlog.configure(
        processors=[
            *shared_processors,
            safe_log_event,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
