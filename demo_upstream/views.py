import json
import time

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt


def ping(request):
    """Cheapest possible endpoint -- used to demonstrate high limits."""
    return JsonResponse({"ok": True, "path": "/demo-upstream/ping"})


@csrf_exempt
def echo(request):
    """Reflects method, query and body so you can verify the proxy forwards
    everything faithfully (headers, verb, payload)."""
    body = request.body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(body) if body else None
    except json.JSONDecodeError:
        parsed = body
    return JsonResponse(
        {
            "method": request.method,
            "query": dict(request.GET),
            "body": parsed,
            "content_type": request.META.get("CONTENT_TYPE", ""),
        }
    )


@csrf_exempt
def expensive(request):
    """Simulates a costly endpoint -- the motivating example for per-endpoint
    limits (an expensive POST should get a tighter budget than a cheap GET)."""
    time.sleep(0.05)
    return JsonResponse({"ok": True, "cost": "high"})
