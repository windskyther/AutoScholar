"""Explicit public credentials for existing functional HTTP fixtures."""

from pydantic import SecretStr

TOKEN = "public-functional-api-token"
TOKEN_SECRET = SecretStr(TOKEN)
AUTH = {"Authorization": "Bearer " + TOKEN}
