"""
Facade that the middleware talks to. It picks the configured backend, applies
the fail-open / fail-closed policy when the backend errors, and hides which
algorithm/store is in use behind a single `check()` call.

Fail-open vs fail-closed (a stretch decision worth an opinion): if Redis goes
down, do we allow all traffic (available but unprotected) or reject all traffic
(protected but the API is effectively down)? There is no universally right
answer. Default here is fail-open, because a rate limiter is usually a guard in
front of an otherwise-healthy backend and taking the whole API down to protect
it is normally the worse outcome -- but it's a config flag precisely because the
right call depends on what you're protecting.
"""
import logging

from django.conf import settings

from .backends.memory import InMemoryFixedWindowLimiter
from .backends.redis_backend import RedisRateLimiter
from .redis_client import get_redis
from .types import LimitResult, LimitRule

logger = logging.getLogger("ratelimiter")

_limiter = None


def get_limiter():
    """Build the configured limiter once and reuse it.

    Set RATELIMIT_ALGORITHM=memory to use the single-node in-memory baseline
    (Phase 2); otherwise a Redis-backed distributed limiter is used (Phase 3).
    """
    global _limiter
    if _limiter is not None:
        return _limiter

    algorithm = settings.RATELIMIT_ALGORITHM
    if algorithm == "memory":
        _limiter = InMemoryFixedWindowLimiter()
    else:
        _limiter = RedisRateLimiter(get_redis(), algorithm=algorithm)
    logger.info("Rate limiter backend: %s", _limiter.name)
    return _limiter


def reset_limiter_for_tests():
    global _limiter
    _limiter = None


def check_limit(key: str, rule: LimitRule) -> tuple[LimitResult, bool]:
    """Run the limit check. Returns (result, backend_ok).

    On backend failure we honour RATELIMIT_FAILURE_MODE: fail-open synthesises
    an "allowed" result, fail-closed synthesises a "denied" one.
    """
    try:
        return get_limiter().check(key, rule), True
    except Exception as exc:  # backend/Redis failure
        logger.error("Limiter backend error (%s): %s", settings.RATELIMIT_FAILURE_MODE, exc)
        if settings.RATELIMIT_FAILURE_MODE == "fail_closed":
            return LimitResult.denied(rule.limit, retry_after=rule.window_seconds), False
        # fail_open
        return (
            LimitResult(
                allowed=True,
                limit=rule.limit,
                remaining=rule.limit,
                reset_after=rule.window_seconds,
                retry_after=0.0,
            ),
            False,
        )
