"""Shared value objects used across the limiter backends."""
from dataclasses import dataclass


@dataclass(frozen=True)
class LimitRule:
    """A single rate-limit policy.

    limit          -- max requests allowed per window (sustained budget)
    window_seconds -- length of the window
    burst          -- token-bucket only: max tokens that can accumulate, i.e.
                      how big an instantaneous burst is tolerated. Defaults to
                      `limit` when not set (no extra burst headroom).
    """

    limit: int
    window_seconds: int
    burst: int | None = None
    name: str = "default"

    @property
    def effective_burst(self) -> int:
        return self.burst if self.burst is not None else self.limit


@dataclass(frozen=True)
class LimitResult:
    """Outcome of a single limit check -- everything the response layer needs
    to both decide (allowed) and inform the client (headers)."""

    allowed: bool
    limit: int
    remaining: int
    # Seconds until the limit fully resets / the window rolls over.
    reset_after: float
    # Seconds the client should wait before retrying (only meaningful on deny).
    retry_after: float

    @classmethod
    def denied(cls, limit: int, retry_after: float) -> "LimitResult":
        return cls(False, limit, 0, retry_after, retry_after)
