"""
Phase 2 -- single-node, in-memory fixed-window limiter.

This is the baseline. It is correct on ONE instance and intentionally kept
simple. Its whole reason for existing in the repo is to make the distributed
problem concrete: run several copies of this and the global limit breaks,
because each process counts in its own memory. A client hitting 3 instances
with a 100/min limit can get ~300/min. That failure is exactly why the counting
state has to be externalised into Redis (Phase 3).

Thread-safe within a process via a lock; that safety does NOT extend across
processes or hosts -- which is the point.
"""
import threading
import time

from ..types import LimitResult, LimitRule


class InMemoryFixedWindowLimiter:
    name = "memory-fixed-window"

    def __init__(self):
        self._lock = threading.Lock()
        # key -> (window_id, count)
        self._buckets: dict[str, tuple[int, int]] = {}

    def check(self, key: str, rule: LimitRule) -> LimitResult:
        now = time.time()
        window = rule.window_seconds
        window_id = int(now // window)
        elapsed = now - (window_id * window)
        reset_after = window - elapsed

        with self._lock:
            stored_window, count = self._buckets.get(key, (window_id, 0))
            if stored_window != window_id:
                count = 0  # new window -> reset
            count += 1
            self._buckets[key] = (window_id, count)

        allowed = count <= rule.limit
        remaining = max(0, rule.limit - count)
        retry_after = reset_after if not allowed else 0.0
        return LimitResult(
            allowed=allowed,
            limit=rule.limit,
            remaining=remaining,
            reset_after=reset_after,
            retry_after=retry_after,
        )
