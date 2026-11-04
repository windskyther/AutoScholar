"""Bounded service-only credentials; never load an application's entire environment."""

from pathlib import Path

from pydantic import SecretStr


def connection_file(path: Path, name: str) -> str:
    try:
        with path.open("rb") as stream:
            raw = stream.read(8193)
        value = raw.decode("utf-8").rstrip("\r\n")
        if len(raw) > 8192 or not value or any(char.isspace() for char in value):
            raise ValueError
    except (OSError, UnicodeError, ValueError):
        raise ValueError(f"{name}_FILE must be a readable bounded connection file") from None
    return value


def sandbox_credential(token: SecretStr | None, path: Path | None) -> SecretStr | None:
    value = token.get_secret_value() if token else ""
    if path is not None:
        if value:
            raise ValueError("Configure SANDBOX_MANAGER_TOKEN or SANDBOX_MANAGER_TOKEN_FILE")
        try:
            with path.open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError
            value = raw.decode("utf-8").rstrip("\r\n")
            if not value:
                raise ValueError
        except (OSError, UnicodeError, ValueError):
            raise ValueError(
                "SANDBOX_MANAGER_TOKEN_FILE must be a readable bounded secret file"
            ) from None
    if not value:
        return None
    if (
        not 32 <= len(value) <= 4096
        or not value.isascii()
        or any(not 33 <= ord(character) <= 126 for character in value)
    ):
        raise ValueError("SANDBOX_MANAGER_TOKEN requires 32+ non-whitespace characters")
    return SecretStr(value)
