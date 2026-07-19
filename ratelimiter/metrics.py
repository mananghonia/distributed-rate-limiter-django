"""
Phase 6 -- observability.

Cheap, dependency-free in-process counters. Every gateway instance exposes its
own /metrics; in a real deployment you'd scrape each instance and aggregate
(e.g. Prometheus). Kept intentionally tiny -- the goal is to answer "how do you
know it works?" with numbers (allowed vs rejected, added latency) rather than a
full metrics stack.
"""
import threading
from collections import defaultdict


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.allowed = 0
        self.rejected = 0
        self.errors = 0            # limiter/backend failures (Redis down, etc.)
        self.per_identity = defaultdict(lambda: {"allowed": 0, "rejected": 0})
        self._latency_sum = 0.0    # seconds of limiter overhead
        self._latency_count = 0

    def record(self, identity_key: str, allowed: bool, overhead_seconds: float):
        with self._lock:
            if allowed:
                self.allowed += 1
                self.per_identity[identity_key]["allowed"] += 1
            else:
                self.rejected += 1
                self.per_identity[identity_key]["rejected"] += 1
            self._latency_sum += overhead_seconds
            self._latency_count += 1

    def record_error(self):
        with self._lock:
            self.errors += 1

    def snapshot(self) -> dict:
        with self._lock:
            total = self.allowed + self.rejected
            avg_ms = (self._latency_sum / self._latency_count * 1000) if self._latency_count else 0.0
            return {
                "allowed": self.allowed,
                "rejected": self.rejected,
                "errors": self.errors,
                "total": total,
                "reject_rate": round(self.rejected / total, 4) if total else 0.0,
                "avg_limiter_overhead_ms": round(avg_ms, 3),
                "top_identities": dict(
                    sorted(
                        self.per_identity.items(),
                        key=lambda kv: kv[1]["allowed"] + kv[1]["rejected"],
                        reverse=True,
                    )[:20]
                ),
            }

    def reset(self):
        with self._lock:
            self.__init__()


# Process-wide singleton.
METRICS = Metrics()
