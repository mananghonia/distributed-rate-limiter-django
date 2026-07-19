"""
Django settings for the distributed rate-limiting gateway.

Most rate-limiter tuning lives under the RATELIMIT_* names at the bottom and is
driven by environment variables so the same image can run in dev (fakeredis) or
in the docker-compose cluster (real Redis) without code changes.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = _env_bool("DJANGO_DEBUG", "1")

# Host filtering. Defaults to "*" for local dev; in production set
# DJANGO_ALLOWED_HOSTS to a comma-separated list. On Render, the platform-
# provided hostname is picked up automatically.
_hosts = os.environ.get("DJANGO_ALLOWED_HOSTS", "*")
ALLOWED_HOSTS = [h.strip() for h in _hosts.split(",") if h.strip()]
_render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _render_host and _render_host not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_render_host)

# Behind Render/any TLS-terminating proxy, trust the forwarded-proto header and
# the platform origin for CSRF so admin POST/DELETE endpoints work over HTTPS.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
_render_url = os.environ.get("RENDER_EXTERNAL_URL")
CSRF_TRUSTED_ORIGINS = [_render_url] if _render_url else []

# Deliberately minimal: this is a stateless edge gateway, not a CMS. No auth /
# contenttypes / ORM models -- all limiter state lives in Redis, so there's no
# database migration step to run.
INSTALLED_APPS = [
    "ratelimiter",
    "demo_upstream",
]

# NOTE: the RateLimitMiddleware is intentionally placed near the top so limiting
# happens before any heavier processing. It only guards the proxy paths.
# Session/CSRF/clickjacking middleware are deliberately omitted: this is a
# stateless JSON gateway with no cookie auth and no HTML pages (the admin
# endpoints are csrf_exempt operator-only). SecurityMiddleware is kept for
# HTTPS redirect + HSTS behind a TLS-terminating proxy.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "ratelimiter.middleware.RateLimitMiddleware",
]

# Off by default so local HTTP dev is unaffected; enabled via env in production
# (see render.yaml). SECURE_PROXY_SSL_HEADER above lets these work behind Render.
SECURE_SSL_REDIRECT = _env_bool("SECURE_SSL_REDIRECT", "0")
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", "0")
SECURE_CONTENT_TYPE_NOSNIFF = True

ROOT_URLCONF = "gateway.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

WSGI_APPLICATION = "gateway.wsgi.application"

# No models -> no database needed. (Kept empty rather than sqlite so nothing
# accidentally depends on a DB in this stateless gateway.)
DATABASES = {}

USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# Rate limiter configuration
# --------------------------------------------------------------------------
# Where allowed requests are forwarded. In production point UPSTREAM_BASE_URL at
# your real backend. For the zero-config live demo we fall back to this service's
# OWN bundled demo upstream over loopback -- deliberately NOT the public URL:
# self-proxying via the edge would take an extra round-trip and, on a small
# worker pool, deadlock (a worker blocked on a call back into its own pool).
# Loopback keeps it in-process-fast and avoids the extra hop. $PORT is set by
# the platform (e.g. Render); locally it defaults to 8000.
_port = os.environ.get("PORT", "8000")
_default_upstream = f"http://127.0.0.1:{_port}/demo-upstream"
UPSTREAM_BASE_URL = os.environ.get("UPSTREAM_BASE_URL", _default_upstream)
UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "10"))

# Identifies this instance in responses (X-Gateway-Instance header). Lets the
# distributed demo prove which of several processes actually handled a request.
INSTANCE_ID = os.environ.get("INSTANCE_ID", "gw-single")

# Redis / algorithm / failure behaviour.
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
RATELIMIT_USE_FAKEREDIS = _env_bool("RATELIMIT_USE_FAKEREDIS", "1")
RATELIMIT_ALGORITHM = os.environ.get("RATELIMIT_ALGORITHM", "sliding_window")
# fail_open -> allow when Redis is unreachable; fail_closed -> reject.
RATELIMIT_FAILURE_MODE = os.environ.get("RATELIMIT_FAILURE_MODE", "fail_open")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "ratelimiter": {"handlers": ["console"], "level": "INFO"},
    },
}
