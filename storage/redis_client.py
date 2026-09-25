"""Redis client initialization."""
import os
from upstash_redis import Redis

_redis: Redis = None


def get_redis() -> Redis:
    """Get or create Redis client instance."""
    global _redis
    if _redis is None:
        url = os.getenv("UPSTASH_REDIS_REST_URL")
        token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
        if not url or not token:
            raise RuntimeError("Missing UPSTASH_REDIS_REST_URL or UPSTASH_REDIS_REST_TOKEN")
        _redis = Redis(url=url, token=token)
    return _redis


# Convenience reference
redis = get_redis
