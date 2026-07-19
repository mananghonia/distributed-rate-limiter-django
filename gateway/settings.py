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
ALLOWED_HOSTS = ["*"]  # This is an edge gateway; host filtering happens upstream.

# Deliberately minimal: this is a stateless edge gateway, not a CMS. No auth /
# contenttypes / ORM models -- all limiter state lives in Redis, so there's no
# database migration step to run.
INSTALLED_APPS = [
    "ratelimiter",
    "demo_upstream",
]

# NOTE: the RateLimitMiddleware is intentionally placed near the top so limiting
# happens before any heavier processing. It only guards the proxy paths.
MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
    "ratelimiter.middleware.RateLimitMiddleware",
]

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
# Where allowed requests are forwarded.
UPSTREAM_BASE_URL = os.environ.get(
    "UPSTREAM_BASE_URL", "http://127.0.0.1:8000/demo-upstream"
)
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
