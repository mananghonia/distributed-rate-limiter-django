"""
Test suite for the rate-limiting gateway.

Covers each phase: proxy pass-through, single-node vs distributed limiting, the
three algorithms, multi-dimensional config, 429 client behaviour, the
concurrency/atomicity guarantee, and fail-open/closed.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from django.test import Client, RequestFactory, SimpleTestCase, override_settings

from .backends.redis_backend import RedisRateLimiter
from .config import ClientIdentity, resolve_identity, resolve_rule
from .redis_client import get_redis, reset_redis_for_tests
from .types import LimitRule


def _fresh_redis():
    """A clean fakeredis-backed limiter store for each test."""
    reset_redis_for_tests()
    r = get_redis()
    r.flushall()
    return r


class AlgorithmTests(SimpleTestCase):
    def setUp(self):
        self.redis = _fresh_redis()

    def test_fixed_window_allows_up_to_limit_then_denies(self):
        lim = RedisRateLimiter(self.redis, "fixed_window")
        rule = LimitRule(limit=3, window_seconds=60, name="t")
        verdicts = [lim.check("rl:{a}:t", rule).allowed for _ in range(5)]
        self.assertEqual(verdicts, [True, True, True, False, False])

    def test_sliding_window_allows_up_to_limit_then_denies(self):
        lim = RedisRateLimiter(self.redis, "sliding_window")
        rule = LimitRule(limit=3, window_seconds=60, name="t")
        verdicts = [lim.check("rl:{b}:t", rule).allowed for _ in range(5)]
        self.assertEqual(verdicts, [True, True, True, False, False])

    def test_sliding_window_log_allows_up_to_limit_then_denies(self):
        lim = RedisRateLimiter(self.redis, "sliding_window_log")
        rule = LimitRule(limit=3, window_seconds=60, name="t")
        verdicts = [lim.check("rl:{e}:t", rule).allowed for _ in range(5)]
        self.assertEqual(verdicts, [True, True, True, False, False])

    def test_token_bucket_allows_burst_up_to_capacity(self):
        lim = RedisRateLimiter(self.redis, "token_bucket")
        # capacity(burst)=5 so 5 back-to-back requests should pass, 6th fails.
        rule = LimitRule(limit=60, window_seconds=60, burst=5, name="t")
        verdicts = [lim.check("rl:{c}:t", rule).allowed for _ in range(6)]
        self.assertEqual(verdicts.count(True), 5)
        self.assertFalse(verdicts[-1])

    def test_result_reports_remaining_and_reset(self):
        lim = RedisRateLimiter(self.redis, "fixed_window")
        rule = LimitRule(limit=10, window_seconds=60, name="t")
        res = lim.check("rl:{d}:t", rule)
        self.assertEqual(res.limit, 10)
        self.assertEqual(res.remaining, 9)
        self.assertTrue(0 < res.reset_after <= 60)


class ConcurrencyTests(SimpleTestCase):
    """The centerpiece: prove the atomic script prevents overcounting when many
    requests race for the same counter (the read-then-write race)."""

    def setUp(self):
        self.redis = _fresh_redis()

    def test_no_overcount_under_concurrency(self):
        lim = RedisRateLimiter(self.redis, "fixed_window")
        limit = 100
        rule = LimitRule(limit=limit, window_seconds=60, name="race")
        total_requests = 500
        results = []
        lock = threading.Lock()

        def worker():
            allowed = lim.check("rl:{race}:race", rule).allowed
            with lock:
                results.append(allowed)

        with ThreadPoolExecutor(max_workers=64) as pool:
            for _ in range(total_requests):
                pool.submit(worker)

        allowed_count = sum(results)
        # Atomicity guarantee: EXACTLY `limit` allowed, never more.
        self.assertEqual(allowed_count, limit)


class ConfigTests(SimpleTestCase):
    def test_api_key_maps_to_tier(self):
        req = mock.Mock()
        req.headers = {"X-API-Key": "paid-key-456"}
        req.GET = {}
        req.META = {}
        identity = resolve_identity(req)
        self.assertEqual(identity.tier, "paid")
        self.assertEqual(identity.key, "apikey:paid-key-456")

    def test_anonymous_falls_back_to_ip(self):
        req = mock.Mock()
        req.headers = {}
        req.GET = {}
        req.META = {"REMOTE_ADDR": "9.9.9.9"}
        identity = resolve_identity(req)
        self.assertEqual(identity.tier, "anonymous")
        self.assertEqual(identity.key, "ip:9.9.9.9")

    def test_endpoint_override_beats_tier_rule(self):
        req = mock.Mock()
        req.method = "POST"
        req.path = "/expensive"
        identity = ClientIdentity(key="ip:1.1.1.1", tier="paid")
        rule = resolve_rule(req, identity)
        self.assertEqual(rule.name, "expensive-post")
        self.assertEqual(rule.limit, 5)


@override_settings(RATELIMIT_ALGORITHM="sliding_window", RATELIMIT_USE_FAKEREDIS=True)
class MiddlewareHttpTests(SimpleTestCase):
    """End-to-end through the middleware. Upstream calls are mocked so we test
    limiting behaviour without a live upstream."""

    def setUp(self):
        from . import limiter

        _fresh_redis()
        limiter.reset_limiter_for_tests()
        self.client = Client()

    def _mock_upstream(self):
        fake = mock.Mock()
        fake.status_code = 200
        # proxy relays raw, still-encoded bytes via .raw.read(decode_content=False)
        fake.raw.read.return_value = b'{"ok": true}'
        fake.headers = {"Content-Type": "application/json"}
        return mock.patch("ratelimiter.proxy.requests.request", return_value=fake)

    def test_allowed_request_is_proxied_with_headers(self):
        with self._mock_upstream():
            resp = self.client.get("/ping", HTTP_X_API_KEY="free-key-123")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["RateLimit-Limit"], "100")
        self.assertEqual(resp["RateLimit-Remaining"], "99")

    def test_over_limit_returns_429_with_retry_after(self):
        # anonymous tier limit is 20; blow past it.
        with self._mock_upstream():
            last = None
            for _ in range(25):
                last = self.client.get("/ping")
        self.assertEqual(last.status_code, 429)
        self.assertIn("Retry-After", last)
        self.assertEqual(last["RateLimit-Remaining"], "0")


@override_settings(RATELIMIT_ALGORITHM="sliding_window")
class FailureModeTests(SimpleTestCase):
    def setUp(self):
        from . import limiter

        limiter.reset_limiter_for_tests()

    def _boom_limiter(self):
        boom = mock.Mock()
        boom.check.side_effect = RuntimeError("redis down")
        return boom

    @override_settings(RATELIMIT_FAILURE_MODE="fail_open")
    def test_fail_open_allows_when_backend_down(self):
        from . import limiter

        with mock.patch.object(limiter, "get_limiter", return_value=self._boom_limiter()):
            result, ok = limiter.check_limit("k", LimitRule(5, 60, name="t"))
        self.assertTrue(result.allowed)
        self.assertFalse(ok)

    @override_settings(RATELIMIT_FAILURE_MODE="fail_closed")
    def test_fail_closed_denies_when_backend_down(self):
        from . import limiter

        with mock.patch.object(limiter, "get_limiter", return_value=self._boom_limiter()):
            result, ok = limiter.check_limit("k", LimitRule(5, 60, name="t"))
        self.assertFalse(result.allowed)
        self.assertFalse(ok)


@override_settings(UPSTREAM_BASE_URL="http://upstream.test/api")
class ProxyTests(SimpleTestCase):
    """The proxy layer -- including regressions for the two bugs found in
    production (compressed-body relay, Accept-Encoding negotiation)."""

    def setUp(self):
        self.rf = RequestFactory()

    def _fake_upstream(self, body=b'{"ok":true}', status=200, headers=None):
        fake = mock.Mock()
        fake.status_code = status
        # proxy relays raw bytes via .raw.read(decode_content=False)
        fake.raw.read.return_value = body
        fake.headers = headers or {"Content-Type": "application/json"}
        return fake

    def test_forwards_method_path_body_and_relays_status(self):
        from ratelimiter import proxy

        req = self.rf.post("/things/1", data=b"payload",
                           content_type="application/octet-stream")
        with mock.patch("ratelimiter.proxy.requests.request",
                        return_value=self._fake_upstream(b"CREATED", 201)) as m:
            resp = proxy.proxy_view(req)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.content, b"CREATED")
        kwargs = m.call_args.kwargs
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["url"], "http://upstream.test/api/things/1")
        self.assertEqual(kwargs["data"], b"payload")

    def test_relays_compressed_body_verbatim(self):
        # Regression: a gzipped upstream body must pass through untouched, with
        # Content-Encoding preserved and Content-Length recomputed for the bytes
        # we actually send. (The original bug returned compressed bytes with the
        # encoding header stripped, so clients saw garbage.)
        import gzip

        from ratelimiter import proxy

        payload = b'{"real":"json","n":1}'
        gzipped = gzip.compress(payload)
        headers = {"Content-Type": "application/json",
                   "Content-Encoding": "gzip",
                   "Content-Length": "99999"}  # upstream's length -- must be dropped
        req = self.rf.get("/data")
        with mock.patch("ratelimiter.proxy.requests.request",
                        return_value=self._fake_upstream(gzipped, 200, headers)):
            resp = proxy.proxy_view(req)
        self.assertEqual(resp.content, gzipped)               # bytes untouched
        self.assertEqual(resp["Content-Encoding"], "gzip")     # header preserved
        # upstream's stale Content-Length must NOT be relayed (Django recomputes
        # it from the actual bytes at finalization).
        self.assertNotEqual(resp.get("Content-Length"), "99999")
        self.assertEqual(gzip.decompress(resp.content), payload)     # inflatable

    def test_strips_hop_by_hop_headers(self):
        from ratelimiter import proxy

        headers = {"Content-Type": "application/json",
                   "Transfer-Encoding": "chunked",
                   "Connection": "keep-alive"}
        req = self.rf.get("/x")
        with mock.patch("ratelimiter.proxy.requests.request",
                        return_value=self._fake_upstream(b"{}", 200, headers)):
            resp = proxy.proxy_view(req)
        self.assertNotIn("Transfer-Encoding", resp)
        self.assertNotIn("Connection", resp)
        self.assertEqual(resp["Content-Type"], "application/json")

    def test_forwards_only_client_accept_encoding(self):
        # Regression: never request an encoding the client didn't ask for.
        from ratelimiter import proxy

        req = self.rf.get("/x")  # client sends no Accept-Encoding
        with mock.patch("ratelimiter.proxy.requests.request",
                        return_value=self._fake_upstream()) as m:
            proxy.proxy_view(req)
        self.assertEqual(m.call_args.kwargs["headers"]["Accept-Encoding"], "identity")

        req2 = self.rf.get("/x", HTTP_ACCEPT_ENCODING="gzip")
        with mock.patch("ratelimiter.proxy.requests.request",
                        return_value=self._fake_upstream()) as m2:
            proxy.proxy_view(req2)
        self.assertEqual(m2.call_args.kwargs["headers"]["Accept-Encoding"], "gzip")

    def test_upstream_timeout_returns_504(self):
        import requests as rq

        from ratelimiter import proxy

        req = self.rf.get("/x")
        with mock.patch("ratelimiter.proxy.requests.request", side_effect=rq.Timeout()):
            resp = proxy.proxy_view(req)
        self.assertEqual(resp.status_code, 504)

    def test_upstream_connection_error_returns_502(self):
        import requests as rq

        from ratelimiter import proxy

        req = self.rf.get("/x")
        with mock.patch("ratelimiter.proxy.requests.request",
                        side_effect=rq.ConnectionError()):
            resp = proxy.proxy_view(req)
        self.assertEqual(resp.status_code, 502)


class AdminAuthTests(SimpleTestCase):
    """The operator-only admin endpoints must not be open on a public deploy."""

    def setUp(self):
        _fresh_redis()
        self.client = Client()

    @override_settings(RATELIMIT_ADMIN_TOKEN="s3cret", DEBUG=False)
    def test_requires_token_when_configured(self):
        self.assertEqual(self.client.get("/admin/limits/ip:1.2.3.4").status_code, 403)
        ok = self.client.get("/admin/limits/ip:1.2.3.4", HTTP_X_ADMIN_TOKEN="s3cret")
        self.assertEqual(ok.status_code, 200)

    @override_settings(RATELIMIT_ADMIN_TOKEN="s3cret", DEBUG=False)
    def test_wrong_token_denied(self):
        r = self.client.get("/admin/limits/ip:1.2.3.4", HTTP_X_ADMIN_TOKEN="nope")
        self.assertEqual(r.status_code, 403)

    @override_settings(RATELIMIT_ADMIN_TOKEN="", DEBUG=False)
    def test_denied_in_production_when_no_token_set(self):
        self.assertEqual(self.client.get("/admin/limits/ip:1.2.3.4").status_code, 403)

    @override_settings(RATELIMIT_ADMIN_TOKEN="", DEBUG=True)
    def test_allowed_in_debug_without_token(self):
        self.assertEqual(self.client.get("/admin/limits/ip:1.2.3.4").status_code, 200)

    @override_settings(RATELIMIT_ADMIN_TOKEN="", DEBUG=True)
    def test_inspects_sorted_set_key_without_error(self):
        # Regression: sliding_window_log stores a ZSET; the admin view must not
        # blindly GET it (WRONGTYPE -> 500).
        redis = get_redis()
        lim = RedisRateLimiter(redis, "sliding_window_log")
        lim.check("rl:{ip:5.5.5.5}:default", LimitRule(3, 60, name="default"))
        resp = self.client.get("/admin/limits/ip:5.5.5.5")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("rl:{ip:5.5.5.5}:default", resp.json()["keys"])
