"""
The middleware that ties the phases together.

For each client request it: resolves the identity and the applicable rule
(Phase 4), runs the atomic limit check (Phase 3, or the in-memory baseline of
Phase 2), records metrics (Phase 6), and either rejects with 429 + standard
headers or lets the request fall through to the proxy view (Phase 1). Allowed
responses also carry the RateLimit-* headers so well-behaved clients can
self-throttle.

Phase 5 -- client-facing behaviour: we model the headers on what GitHub/Stripe
actually send. A good limiter tells clients how to back off instead of just
slamming the door:
  * RateLimit-Limit      -- the ceiling for the current window
  * RateLimit-Remaining  -- requests left before rejection
  * RateLimit-Reset      -- seconds until the window resets
  * Retry-After          -- (on 429) seconds to wait before retrying
"""
import json
import time

from django.conf import settings
from django.http import HttpResponse

from .config import bucket_key, resolve_identity, resolve_rule
from .limiter import check_limit
from .metrics import METRICS

# Gateway-operational paths that must never be rate-limited or proxied.
_EXEMPT_PREFIXES = ("/healthz", "/metrics", "/admin/", "/demo-upstream/")
# Exact-match exemptions (can't be prefixes: "/" is a prefix of everything).
_EXEMPT_EXACT = ("/", "/favicon.ico")


def _apply_headers(response, result):
    response["RateLimit-Limit"] = str(result.limit)
    response["RateLimit-Remaining"] = str(result.remaining)
    response["RateLimit-Reset"] = str(int(result.reset_after))
    # Which instance handled this request -- proves the load spread across
    # separate processes while the global limit still held.
    response["X-Gateway-Instance"] = settings.INSTANCE_ID


class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in _EXEMPT_EXACT or request.path.startswith(_EXEMPT_PREFIXES):
            return self.get_response(request)

        identity = resolve_identity(request)
        rule = resolve_rule(request, identity)
        key = bucket_key(identity, rule)

        started = time.perf_counter()
        result, backend_ok = check_limit(key, rule)
        overhead = time.perf_counter() - started

        METRICS.record(identity.key, result.allowed, overhead)
        if not backend_ok:
            METRICS.record_error()

        if not result.allowed:
            body = json.dumps(
                {
                    "error": "rate_limit_exceeded",
                    "message": "Too Many Requests",
                    "retry_after_seconds": int(result.retry_after),
                }
            )
            response = HttpResponse(body, status=429, content_type="application/json")
            _apply_headers(response, result)
            response["Retry-After"] = str(int(result.retry_after))
            return response

        response = self.get_response(request)
        _apply_headers(response, result)
        return response
