# Distributed Rate-Limiting Gateway (Django)

A standalone gateway that sits **in front of** one or more backend APIs. Every
request passes through it first: it decides whether the client has exceeded its
allowed request rate. If yes → `429 Too Many Requests`; if no → the request is
forwarded to the real backend and the response is returned.

The key word is **distributed**: the gateway runs as multiple identical
instances behind a load balancer, and the rate limit must hold **globally** even
though a client's requests land on different instances. That single constraint
is the whole point — local per-process counters break under horizontal scaling,
so the counting state is externalised into **Redis** and mutated with **atomic
Lua scripts** so concurrent instances can't overcount.

See [DESIGN.md](DESIGN.md) for the trade-offs (algorithm choice, the
race-condition and its fix, identity resolution, fail-open vs fail-closed).

---

## Quick start (single node, no Redis required)

The limiter falls back to an in-process `fakeredis` when no Redis server is
reachable, so you can run everything with zero infrastructure:

```bash
pip install -r requirements.txt
python manage.py runserver 127.0.0.1:8000
```

Then, in another terminal:

```bash
# Health + which backend/algorithm is active
curl http://127.0.0.1:8000/healthz

# Proxied request (anonymous tier = 20/min). Watch the headers.
curl -i http://127.0.0.1:8000/ping

# Trip the limit on an expensive endpoint (POST /expensive = 5/min)
for i in $(seq 1 7); do curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/expensive; done
# -> 200 200 200 200 200 429 429

# Use an API key to get the paid tier (10,000/min)
curl -i -H "X-API-Key: paid-key-456" http://127.0.0.1:8000/ping

# Observability
curl http://127.0.0.1:8000/metrics

# Inspect / reset a client's live limit state
curl http://127.0.0.1:8000/admin/limits/ip:127.0.0.1
curl -X DELETE http://127.0.0.1:8000/admin/limits/ip:127.0.0.1
```

> `fakeredis` is per-process, so single-node only. To prove the *distributed*
> property you need real Redis shared across instances — see below.

## Prove it's actually distributed

This is the centerpiece: run **multiple separate gateway processes** against
**one shared Redis** and show the limit holds *globally*.
`loadtest/distributed_demo.py` orchestrates it end to end -- it launches N real
gateway processes, waits for each to connect to Redis, fires concurrent traffic
round-robin across all of them, and reports the global vs per-instance counts.

You need a real Redis (the in-process fakeredis fallback is single-process and
cannot demonstrate this).

**Option A -- Docker** (Linux/Mac/Windows-with-Docker): full cluster with an
nginx load balancer:

```bash
docker compose up --build          # Redis + 3 instances + LB on :8080
python loadtest/distributed_demo.py --instances 3 --requests 300 --api-key free-key-123
```

**Option B -- Windows without Docker**: grab a portable Redis (no admin needed),
e.g. the tporadowski Redis-for-Windows zip, unzip it and run:

```bash
redis-server.exe --port 6399 --save "" --appendonly no
# then, in the project:
REDIS_URL=redis://127.0.0.1:6399/0 python loadtest/distributed_demo.py \
    --instances 3 --requests 300 --concurrency 60 --api-key free-key-123
```

### Actual result (3 processes, shared Redis, free tier = 100/min)

```
Per-instance traffic (proves load really spread across processes):
  gw-1: handled  100  |  allowed  33
  gw-2: handled  100  |  allowed  34
  gw-3: handled  100  |  allowed  33

  GLOBAL allowed:  100   <- equals the tier limit, NOT 100 x 3
  GLOBAL rejected: 200
PROOF: global limit held at 100 across all instances.
```

Now run the **exact same load with the in-memory backend** and watch it break --
each process counts in isolation, so the client gets 3x their limit:

```bash
... python loadtest/distributed_demo.py ... --algorithm memory
#   GLOBAL allowed:  300   <- limit x 3, the client escaped the limit
```

That contrast (100 vs 300) is the entire point of externalising state into Redis
with atomic Lua.

## Load test / proof it works

```
$ python loadtest/loadtest.py --url http://127.0.0.1:8000/ping \
      --requests 300 --concurrency 50 --api-key free-key-123

  total:     300
  allowed:   100   <- equals the configured free-tier limit (100/min), exactly
  rejected:  200   (429 Too Many Requests)
```

The allowed count equals the configured limit **exactly**, under 50 concurrent
threads — that's the atomic Lua script preventing the read-then-write overcount.

> Note on latency: on the Django dev server with the `fakeredis` + `lupa` Lua
> interpreter (and self-proxying inside one process) the per-request overhead is
> tens of ms. With real Redis and gunicorn the limiter hop is single-digit ms —
> the Lua script is a couple of O(1) Redis ops.

## Configuration

All via environment variables (see [.env.example](.env.example)):

| Variable | Default | Meaning |
| --- | --- | --- |
| `UPSTREAM_BASE_URL` | bundled demo | where allowed requests are forwarded |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis connection |
| `RATELIMIT_USE_FAKEREDIS` | `1` | fall back to in-process fakeredis if Redis is down |
| `RATELIMIT_ALGORITHM` | `sliding_window` | `fixed_window` \| `sliding_window` \| `token_bucket` \| `memory` |
| `RATELIMIT_FAILURE_MODE` | `fail_open` | `fail_open` \| `fail_closed` when Redis is unreachable |

`RATELIMIT_ALGORITHM=memory` selects the single-node in-memory baseline
(Phase 2) — useful for demonstrating *why* it breaks across instances.

Limit policy (tiers, per-endpoint overrides, API keys) lives as data in
[ratelimiter/config.py](ratelimiter/config.py) — swap it for a DB table or
config service without touching the checking logic.

## Tests

```bash
python manage.py test ratelimiter
```

Covers all three algorithms, the multi-dimensional config, 429 behaviour,
fail-open/closed, and the **concurrency/atomicity guarantee** (500 racing
requests → exactly `limit` allowed).

## Layout

```
gateway/            Django project (settings, urls, wsgi)
demo_upstream/      bundled dummy backend to proxy to (ping/echo/expensive)
ratelimiter/
  proxy.py          Phase 1: pass-through proxy
  backends/memory.py    Phase 2: single-node in-memory fixed window
  backends/redis_backend.py  Phase 3: distributed limiter
  lua/*.lua         atomic scripts: fixed_window, sliding_window, token_bucket
  config.py         Phase 4: identity resolution + tier/endpoint rules
  middleware.py     Phase 5: ties it together, 429 + RateLimit-* headers
  metrics.py        Phase 6: allowed/rejected/overhead counters
  limiter.py        backend selection + fail-open/closed
  views.py          /healthz, /metrics, /admin/limits/<identity>
loadtest/loadtest.py    concurrent load test
deploy/nginx.conf       load balancer for the docker cluster
```
