import logging
from pathlib import Path

import httpx
import pytest
import structlog
from alembic import command
from alembic.config import Config
from pydantic import SecretStr

from autoscholar.core.config import Settings
from autoscholar.core.logging import configure_logging
from autoscholar.research.providers import SearchProviderError, TavilySearchProvider


def test_application_foreign_logs_and_exceptions_are_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(
        "DEBUG",
        settings=Settings(
            experiment_api_token=SecretStr("public-synthetic-token-123456"),
            llm_api_key=SecretStr("synthetic-provider-key"),
            database_url="postgresql+asyncpg://user:synthetic-db-password@localhost/database",
        ),
    )
    logger = structlog.get_logger("security-test")
    logger.info(
        "safe_event",
        api_key="unregistered-key",
        body="private-prompt",
        token="public-synthetic-token-123456",
        authorization="Bearer unknown-auth",
    )
    try:
        raise RuntimeError("exception-with-private-document")
    except RuntimeError:
        logger.exception("provider_failed")
        logging.getLogger("httpx").exception(
            "HTTP https://username:synthetic-provider-key@example.test/private-path?token=secret"
        )
    logging.getLogger("driver").warning(
        "DSN postgresql+asyncpg://user:synthetic-db-password@localhost/database"
    )
    output = capsys.readouterr().out
    assert "safe_event" in output and "provider_failed" in output
    for secret in (
        "unregistered-key",
        "private-prompt",
        "public-synthetic-token-123456",
        "unknown-auth",
        "synthetic-provider-key",
        "synthetic-db-password",
        "exception-with-private-document",
        "private-path",
        "token=secret",
    ):
        assert secret not in output


@pytest.mark.parametrize("failure", ["timeout", "rate_limit", "server_error"])
async def test_supplier_default_policy_never_retries(failure: str) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("unknown result", request=request)
        return httpx.Response(429 if failure == "rate_limit" else 503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as api:
        provider = TavilySearchProvider(api_key="public", client=api)
        with pytest.raises(SearchProviderError):
            await provider.search("public", limit=1)
    assert len(calls) == 1


async def test_opt_in_retry_backoff_is_bounded_by_total_deadline() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "10000"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as api:
        provider = TavilySearchProvider(
            api_key="public", client=api, timeout_seconds=0.02, retry_delays=(0.01,)
        )
        with pytest.raises(SearchProviderError):
            await provider.search("public", limit=1)
    assert len(calls) == 1


def test_audit_migration_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url", "sqlite+aiosqlite:///" + (tmp_path / "schema.db").as_posix()
    )
    # Isolate the new migration. Historical PostgreSQL FK alterations are intentionally
    # not emulated by SQLite; the complete chain has a separate PostgreSQL check.
    command.stamp(config, "20261006_0013")
    command.upgrade(config, "20261104_0014")
    command.downgrade(config, "20261006_0013")
    command.upgrade(config, "20261104_0014")
