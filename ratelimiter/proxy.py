"""
Phase 1 -- the pass-through proxy.

This deliberately knows nothing about rate limiting. Its only job is to forward
a request unchanged to the configured upstream and return the response
faithfully: same method, same body, same (hop-by-hop-stripped) headers, same
status code. Keeping "am I proxying correctly?" separate from "am I limiting
correctly?" mirrors how real gateways (Kong, Envoy, nginx) are layered, and it
means the limiter can be reasoned about and tested in isolation.
"""
import logging

import requests
from django.conf import settings
from django.http import HttpResponse

logger = logging.getLogger("ratelimiter")

# Headers that describe a single transport hop and must NOT be copied verbatim
# when relaying a request/response. Forwarding these corrupts the proxied call.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}
# NOTE: content-encoding is deliberately NOT hop-by-hop here. We relay the body
# verbatim (compressed bytes and all), so the encoding header must travel with it
# -- otherwise the client receives compressed bytes it doesn't know to inflate.
# content-length IS stripped: we let Django recompute it from the relayed bytes.


def _forward_request_headers(request):
    headers = {}
    for key, value in request.headers.items():
        if key.lower() in HOP_BY_HOP:
            continue
        headers[key] = value
    # Preserve the client chain for downstream logging / identity resolution.
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    client_ip = request.META.get("REMOTE_ADDR", "")
    headers["X-Forwarded-For"] = f"{xff}, {client_ip}" if xff else client_ip
    # Only negotiate the encodings the client actually asked for. Without this,
    # requests injects its own "Accept-Encoding: gzip", so the upstream would
    # compress a response the client never said it could decompress -- and since
    # we relay bodies verbatim, that client would get bytes it can't read.
    headers["Accept-Encoding"] = request.headers.get("Accept-Encoding", "identity")
    return headers


def proxy_view(request):
    """Forward the request to UPSTREAM_BASE_URL and relay the response."""
    upstream_base = settings.UPSTREAM_BASE_URL.rstrip("/")
    target = f"{upstream_base}{request.path}"

    try:
        upstream_response = requests.request(
            method=request.method,
            url=target,
            params=request.GET,
            data=request.body,
            headers=_forward_request_headers(request),
            timeout=settings.UPSTREAM_TIMEOUT_SECONDS,
            allow_redirects=False,
            # Relay the body untouched: don't let requests transparently inflate
            # it, so what we forward matches the Content-Encoding header we relay.
            stream=True,
        )
    except requests.Timeout:
        logger.warning("Upstream timeout for %s", target)
        return HttpResponse("Upstream timed out", status=504)
    except requests.RequestException as exc:
        logger.warning("Upstream error for %s: %s", target, exc)
        return HttpResponse("Bad gateway", status=502)

    # Raw, still-encoded bytes exactly as the upstream sent them.
    raw_body = upstream_response.raw.read(decode_content=False)
    response = HttpResponse(content=raw_body, status=upstream_response.status_code)
    for key, value in upstream_response.headers.items():
        if key.lower() in HOP_BY_HOP:
            continue
        response[key] = value
    return response
