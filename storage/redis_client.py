"""Redis client initialization (single shared storage layer)."""
import os
from typing import Optional

from upstash_redis import Redis

_redis: Optional[Redis] = None


def set_redis(client) -> None:
    """Inject a Redis-compatible client (used by tests / custom setups)."""
    global _redis
    _redis = client


def get_redis() -> Redis:
    """Get or create the one and only Redis client instance.

    Resolution order:
      1. injected client (``set_redis``);
      2. Upstash REST credentials from env;
      3. redis-py style ``REDIS_URL`` (fallback, e.g. other providers).
    """
    global _redis
    if _redis is None:
        url = os.getenv("UPSTASH_REDIS_REST_URL")
        token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
        if url and token:
            _redis = Redis(url=url, token=token)
            return _redis
        # Fallback: plain REDIS_URL via redis-py if installed.
        redis_url = os.getenv("REDIS_URL")
        if redis_url:
            import redis as redis_py  # lazy import: optional dependency

            _redis = redis_py.Redis.from_url(redis_url, decode_responses=True)
            return _redis
        raise RuntimeError(
            "Redis is not configured: set UPSTASH_REDIS_REST_URL + "
            "UPSTASH_REDIS_REST_TOKEN (or REDIS_URL)"
        )
    return _redis


# Convenience reference
redis = get_redis
