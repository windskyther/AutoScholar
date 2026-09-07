import pytest
from pydantic import SecretStr, ValidationError

from autoscholar.core.config import Settings


def test_settings_use_safe_defaults() -> None:
    settings = Settings(llm_api_key=None, llm_model=None)

    assert settings.app_env == "development"
    assert settings.app_port == 8000
    assert settings.llm_configured is False
    assert settings.web_search_configured is False
    assert settings.research_timeout_seconds == 20
    assert settings.research_cache_ttl_seconds == 86_400


def test_llm_is_configured_only_with_key_and_model() -> None:
    settings = Settings(
        llm_api_key=SecretStr("secret"),
        llm_model="test-model",
    )

    assert settings.llm_configured is True
    assert "secret" not in repr(settings)


def test_research_keys_are_secret_and_tavily_controls_web_capability() -> None:
    settings = Settings(
        tavily_api_key=SecretStr("tavily-secret"),
        semantic_scholar_api_key=SecretStr("s2-secret"),
    )

    assert settings.web_search_configured is True
    assert "tavily-secret" not in repr(settings)
    assert "s2-secret" not in repr(settings)


def test_invalid_port_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(app_port=70000)
