import pytest
from pydantic import SecretStr, ValidationError

from autoscholar.core.config import Settings


def test_settings_use_safe_defaults() -> None:
    settings = Settings(llm_api_key=None, llm_model=None)

    assert settings.app_env == "development"
    assert settings.app_port == 8000
    assert settings.llm_configured is False


def test_llm_is_configured_only_with_key_and_model() -> None:
    settings = Settings(
        llm_api_key=SecretStr("secret"),
        llm_model="test-model",
    )

    assert settings.llm_configured is True
    assert "secret" not in repr(settings)


def test_invalid_port_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(app_port=70000)
