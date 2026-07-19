from django.urls import include, path, re_path

from ratelimiter import views as rl_views
from ratelimiter.proxy import proxy_view

urlpatterns = [
    # Human-friendly landing page + favicon so browser visitors don't fall
    # through to the proxy catch-all.
    path("", rl_views.index, name="index"),
    path("favicon.ico", rl_views.favicon, name="favicon"),

    # Operational endpoints on the gateway itself (never proxied, never limited).
    path("healthz", rl_views.healthz, name="healthz"),
    path("metrics", rl_views.metrics, name="metrics"),
    path("admin/limits/<path:identity>", rl_views.admin_limit_state, name="admin_limit_state"),

    # The bundled demo backend the gateway can forward to out of the box.
    path("demo-upstream/", include("demo_upstream.urls")),

    # Everything else is treated as client traffic: rate-limited, then proxied
    # to the configured upstream. Keep this LAST -- it is the catch-all.
    re_path(r"^.*$", proxy_view, name="proxy"),
]
