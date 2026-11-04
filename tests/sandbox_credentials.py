from pydantic import SecretStr

TOKEN = "public-sandbox-control-token-0123456789"
TOKEN_SECRET = SecretStr(TOKEN)
AUTH = {"Authorization": "Bearer " + TOKEN}
