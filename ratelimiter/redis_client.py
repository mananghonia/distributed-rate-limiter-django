"""
Redis connection management with a dev/test fallback.

In production this returns a real redis-py client pointed at REDIS_URL. When no
Redis server is available (local dev, CI) and RATELIMIT_USE_FAKEREDIS is on, we
transparently substitute an in-process `fakeredis` server. fakeredis executes
the same Lua scripts, so the atomicity semantics we rely on are preserved and
the limiter behaves identically -- only the "shared across separate processes"
property is lost (fakeredis is per-process).
"""
import logging
import threading

from django.conf import settings

logger = logging.getLogger("ratelimiter")

_client = None
_lock = threading.Lock()


def _build_client():
    import redis

    if settings.RATELIMIT_USE_FAKEREDIS:
        try:
            client = redis.from_url(settings.REDIS_URL, socket_connect_timeout=0.25)
            client.ping()
            logger.info("Connected to real Redis at %s", settings.REDIS_URL)
            return client
        except Exception:
            import fakeredis

            logger.warning(
                "Redis unreachable at %s; falling back to in-process fakeredis "
                "(single-process only -- not truly distributed).",
                settings.REDIS_URL,
            )
            return fakeredis.FakeStrictRedis()

    # Production path: no fallback -- surface connection problems loudly.
    client = redis.from_url(settings.REDIS_URL)
    return client


def get_redis():
    """Process-wide singleton client."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = _build_client()
    return _client


def reset_redis_for_tests():
    """Drop the cached client so tests can swap connections."""
    global _client
    with _lock:
        _client = None
