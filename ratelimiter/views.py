"""
Operational endpoints served by the gateway itself (exempt from limiting/proxy):

  GET /healthz                 -- liveness + which backend/algorithm is active
  GET /metrics                 -- allowed/rejected/overhead counters (Phase 6)
  GET/DELETE /admin/limits/... -- inspect or reset a client's live limit state
"""
import json

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .config import bucket_key
from .metrics import METRICS
from .redis_client import get_redis
from .types import LimitRule


_LANDING = """<!doctype html>
<html><head><meta charset="utf-8"><title>Distributed Rate-Limiting Gateway</title>
<style>
 body{{background:#0d1117;color:#c9d1d9;font:16px/1.6 system-ui,sans-serif;max-width:720px;margin:6vh auto;padding:0 20px}}
 h1{{color:#fff}} code{{background:#161b22;padding:2px 6px;border-radius:4px;color:#58a6ff}}
 a{{color:#58a6ff}} li{{margin:.35em 0}} .muted{{color:#8b949e}}
</style></head><body>
<h1>Distributed Rate-Limiting Gateway</h1>
<p>A Django edge gateway that rate-limits requests (Redis + atomic Lua) before
proxying them upstream. This is the live demo &mdash; algorithm
<code>{algo}</code>.</p>
<p><strong>Try it:</strong></p>
<ul>
 <li><a href="/healthz">/healthz</a> &mdash; liveness + Redis status</li>
 <li><a href="/ping">/ping</a> &mdash; a proxied request (anonymous tier: 20/min). Refresh to watch <code>RateLimit-Remaining</code> fall.</li>
 <li><code>POST /expensive</code> &mdash; tight 5/min limit; the 6th returns <code>429</code> with <code>Retry-After</code>.</li>
 <li><a href="/metrics">/metrics</a> &mdash; allowed / rejected counters</li>
</ul>
<p class="muted">Send header <code>X-API-Key: paid-key-456</code> for the paid tier (10,000/min).
Free instance: the first request after idle may cold-start (~50s).</p>
<p class="muted">Code &amp; design notes:
<a href="https://github.com/mananghonia/distributed-rate-limiter-django">github.com/mananghonia/distributed-rate-limiter-django</a></p>
</body></html>"""


def index(request):
    """Human-friendly landing page. Exempt from limiting/proxying so visitors
    (and their browsers' automatic requests) don't hit the proxy path."""
    return HttpResponse(_LANDING.format(algo=settings.RATELIMIT_ALGORITHM))


def favicon(request):
    """No favicon -- return 204 so browsers stop requesting it (and so it never
    falls through to the proxy)."""
    return HttpResponse(status=204)


def healthz(request):
    info = {
        "status": "ok",
        "algorithm": settings.RATELIMIT_ALGORITHM,
        "failure_mode": settings.RATELIMIT_FAILURE_MODE,
    }
    try:
        get_redis().ping()
        info["redis"] = "up"
    except Exception:
        info["redis"] = "down"
        info["status"] = "degraded"
    return JsonResponse(info)


def metrics(request):
    return JsonResponse(METRICS.snapshot())


@csrf_exempt
def admin_limit_state(request, identity):
    """Inspect (GET) or reset (DELETE) the Redis keys backing a client's limits.

    `identity` is the bucket id, e.g. "apikey:paid-key-456" or "ip:1.2.3.4".
    Handy for live debugging and demos ("reset my limit and watch it refill").
    In production this would sit behind auth -- it's operator-only.
    """
    redis = get_redis()
    # Keys are namespaced rl:{<identity>}:<rule>; match all rules for this id.
    pattern = f"rl:{{{identity}}}:*"
    try:
        keys = [k.decode() if isinstance(k, bytes) else k for k in redis.keys(pattern)]
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=503)

    if request.method == "DELETE":
        deleted = redis.delete(*keys) if keys else 0
        return JsonResponse({"identity": identity, "deleted_keys": deleted})

    state = {}
    for k in keys:
        ttl = redis.ttl(k)
        ktype = redis.type(k).decode() if isinstance(redis.type(k), bytes) else redis.type(k)
        if ktype == "hash":
            raw = redis.hgetall(k)
            value = {
                (f.decode() if isinstance(f, bytes) else f): (
                    v.decode() if isinstance(v, bytes) else v
                )
                for f, v in raw.items()
            }
        else:
            v = redis.get(k)
            value = v.decode() if isinstance(v, bytes) else v
        state[k] = {"ttl": ttl, "value": value}
    return JsonResponse({"identity": identity, "keys": state})
