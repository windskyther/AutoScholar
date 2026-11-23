"""Public encryption material for offline fixtures; HTTP requests need no login."""

from pydantic import SecretStr

TOKEN = "public-functional-api-token"
TOKEN_SECRET = SecretStr(TOKEN)
AUTH: dict[str, str] = {}
