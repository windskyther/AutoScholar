import os

import pytest

from autoscholar.core.config import Settings

# Test collection imports application modules before autouse fixtures execute.
# Disable real configuration before those imports, including the default app.
Settings.model_config["env_file"] = None
for _setting in Settings.model_fields:
    os.environ.pop(_setting.upper(), None)


@pytest.fixture(autouse=True)
def isolate_settings_from_local_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep unit-test settings independent from a developer's local secrets."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
