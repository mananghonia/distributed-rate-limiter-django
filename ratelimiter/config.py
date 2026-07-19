"""
Phase 4 -- configurable, multi-dimensional limits.

Real limiters never apply one global rule. Limits vary along several axes:

  * identity  -- who is the client? (API key / user tier / IP)
  * tier      -- free vs paid get very different budgets
  * endpoint  -- a cheap GET may allow far more than an expensive POST

The policy below is defined as data (a dict, easily swapped for a DB table or
JSON file), NOT hardcoded in the checking logic -- that separation is what makes
the thing look operable rather than a demo.

Identity resolution trade-off (a deliberate design choice worth defending):
  * IP address -- zero client effort, but every user behind a shared NAT or
    corporate proxy collapses into one bucket (false throttling), and it is
    trivially spoofed via X-Forwarded-For unless you trust your edge.
  * API key    -- accurate and per-customer, but requires clients to
    authenticate. This is what production API providers use.
We prefer the API key when present and fall back to IP for anonymous traffic.
"""
from dataclasses import dataclass

from .types import LimitRule

# --------------------------------------------------------------------------
# Policy table. In a larger system this would be a DB table / config service;
# keeping it here keeps the project self-contained while staying data-driven.
# --------------------------------------------------------------------------

# API key -> tier. Unknown/absent keys are treated as anonymous.
API_KEY_TIERS: dict[str, str] = {
    "free-key-123": "free",
    "paid-key-456": "paid",
}

# tier -> default rule applied to that tier's traffic.
TIER_RULES: dict[str, LimitRule] = {
    "anonymous": LimitRule(limit=20, window_seconds=60, name="anonymous"),
    "free": LimitRule(limit=100, window_seconds=60, name="free"),
    "paid": LimitRule(limit=10_000, window_seconds=60, burst=200, name="paid"),
}

# (method, path_prefix) -> rule override. More specific than the tier rule and
# used to give expensive endpoints a tighter budget. First match wins, so order
# from most specific to least.
ENDPOINT_OVERRIDES: list[tuple[str, str, LimitRule]] = [
    ("POST", "/expensive", LimitRule(limit=5, window_seconds=60, name="expensive-post")),
    ("*", "/expensive", LimitRule(limit=10, window_seconds=60, name="expensive")),
]


@dataclass(frozen=True)
class ClientIdentity:
    """Who we are limiting, and the label used to build the Redis key."""

    key: str        # unique bucket id, e.g. "apikey:paid-key-456" or "ip:1.2.3.4"
    tier: str


def resolve_identity(request) -> ClientIdentity:
    """Prefer an API key; fall back to client IP for anonymous traffic."""
    api_key = request.headers.get("X-API-Key") or request.GET.get("api_key")
    if api_key:
        tier = API_KEY_TIERS.get(api_key, "anonymous")
        return ClientIdentity(key=f"apikey:{api_key}", tier=tier)

    # X-Forwarded-For's first hop is the original client (behind our LB/nginx).
    xff = request.headers.get("X-Forwarded-For", "")
    client_ip = xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", "unknown")
    return ClientIdentity(key=f"ip:{client_ip}", tier="anonymous")


def resolve_rule(request, identity: ClientIdentity) -> LimitRule:
    """Endpoint override (if any) wins over the tier default."""
    path = request.path
    for method, prefix, rule in ENDPOINT_OVERRIDES:
        if method in (request.method, "*") and prefix in path:
            return rule
    return TIER_RULES.get(identity.tier, TIER_RULES["anonymous"])


def bucket_key(identity: ClientIdentity, rule: LimitRule) -> str:
    """Namespace the Redis key by identity + rule so different policies for the
    same client (e.g. a global tier limit vs a per-endpoint limit) never share
    a counter."""
    return f"rl:{{{identity.key}}}:{rule.name}"
