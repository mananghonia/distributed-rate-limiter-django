from django.urls import path

from . import views

# Stand-in "real backend" the gateway proxies to. In production UPSTREAM_BASE_URL
# would point at your actual service; this exists so the project runs end-to-end
# with zero external dependencies.
urlpatterns = [
    path("ping", views.ping, name="demo_ping"),
    path("echo", views.echo, name="demo_echo"),
    path("expensive", views.expensive, name="demo_expensive"),
]
