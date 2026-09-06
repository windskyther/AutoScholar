"""External infrastructure clients."""

from autoscholar.infrastructure.database import Database
from autoscholar.infrastructure.redis_client import RedisClient

__all__ = ["Database", "RedisClient"]

