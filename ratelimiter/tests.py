"""
Test suite for the rate-limiting gateway.

Covers each phase: proxy pass-through, single-node vs distributed limiting, the
three algorithms, multi-dimensional config, 429 client behaviour, the
concurrency/atomicity guarantee, and fail-open/closed.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from django.test import Client, SimpleTestCase, override_settings

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
        fake.content = b'{"ok": true}'
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
