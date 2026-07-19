"""
Operational endpoints served by the gateway itself (exempt from limiting/proxy):

  GET /healthz                 -- liveness + which backend/algorithm is active
  GET /metrics                 -- allowed/rejected/overhead counters (Phase 6)
  GET/DELETE /admin/limits/... -- inspect or reset a client's live limit state
"""
import json

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .config import bucket_key
from .metrics import METRICS
from .redis_client import get_redis
from .types import LimitRule


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
