"""
Phase 3 -- the distributed limiter (the centerpiece).

Counting state lives in Redis so every gateway instance reads and writes the
same counters. The subtle bug this defends against is the read-then-write race:
instance A reads 99, instance B reads 99 before either writes, both allow, and
the limit is exceeded. The fix is atomicity -- the read + increment + check must
be one indivisible operation. Each algorithm is implemented as a Lua script that
Redis executes atomically server-side, which eliminates the race regardless of
how many instances hit the same key at the same millisecond.
"""
import time
import uuid
from pathlib import Path

from ..types import LimitResult, LimitRule

_LUA_DIR = Path(__file__).resolve().parent.parent / "lua"


def _load(script_name: str) -> str:
    return (_LUA_DIR / script_name).read_text(encoding="utf-8")


class RedisRateLimiter:
    """Distributed limiter. `algorithm` selects which Lua script backs it."""

    ALGORITHMS = {"fixed_window", "sliding_window", "sliding_window_log", "token_bucket"}

    def __init__(self, redis_client, algorithm: str = "sliding_window"):
        if algorithm not in self.ALGORITHMS:
            raise ValueError(f"Unknown algorithm: {algorithm}")
        self.name = f"redis-{algorithm}"
        self.algorithm = algorithm
        self._redis = redis_client
        # register_script uses EVALSHA with an automatic EVAL fallback, so the
        # script body is shipped once and thereafter referenced by hash.
        self._scripts = {
            "fixed_window": redis_client.register_script(_load("fixed_window.lua")),
            "sliding_window": redis_client.register_script(_load("sliding_window.lua")),
            "sliding_window_log": redis_client.register_script(_load("sliding_window_log.lua")),
            "token_bucket": redis_client.register_script(_load("token_bucket.lua")),
        }

    def check(self, key: str, rule: LimitRule) -> LimitResult:
        now = time.time()
        if self.algorithm == "fixed_window":
            raw = self._scripts["fixed_window"](
                keys=[key], args=[rule.limit, rule.window_seconds]
            )
        elif self.algorithm == "sliding_window":
            raw = self._scripts["sliding_window"](
                keys=[key], args=[rule.limit, rule.window_seconds, now]
            )
        elif self.algorithm == "sliding_window_log":
            # unique member so two requests at the same timestamp both count
            member = f"{now}:{uuid.uuid4().hex}"
            raw = self._scripts["sliding_window_log"](
                keys=[key], args=[rule.limit, rule.window_seconds, now, member]
            )
        else:  # token_bucket
            capacity = rule.effective_burst
            refill_rate = rule.limit / rule.window_seconds
            ttl = int(rule.window_seconds * 2)
            raw = self._scripts["token_bucket"](
                keys=[key], args=[capacity, refill_rate, now, ttl]
            )

        allowed, remaining, reset_after, retry_after = (int(x) for x in raw)
        return LimitResult(
            allowed=bool(allowed),
            limit=rule.limit,
            remaining=int(remaining),
            reset_after=float(reset_after),
            retry_after=float(retry_after),
        )
