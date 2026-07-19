# Deploying the gateway (live demo)

This deploys a **live, real-Redis demo** of the gateway on Render's free tier: a
public URL where rate limiting, `429`s, and `RateLimit-*` headers work against a
real managed Redis. The code is already deploy-ready — `render.yaml` describes
the whole stack.

> The free plan runs a **single instance**, so this demonstrates rate limiting
> live, but not the *multi-instance distributed proof* (that needs >1 instance —
> run it locally or via `docker compose`, see the README). The demo still uses
> real Redis, so it's the genuine atomic-Lua path, not the fakeredis fallback.

## What the blueprint provisions

- **rate-limiter-gateway** — the Django app (Docker), gunicorn on `$PORT`
- **rate-limiter-kv** — a managed Redis (Key Value) store, wired in via `REDIS_URL`
- Secure prod settings: `DEBUG=0`, a Render-generated `SECRET_KEY`,
  `RATELIMIT_USE_FAKEREDIS=0`, HSTS on. Host allow-listing uses Render's hostname
  automatically.
- `UPSTREAM_BASE_URL` is left unset on purpose — the app defaults it to its own
  `RENDER_EXTERNAL_URL` + `/demo-upstream`, so allowed requests return `200`.

## Steps (the part that needs your login)

1. Push is already done — the repo is at
   `github.com/mananghonia/distributed-rate-limiter-django`.
2. Go to **https://dashboard.render.com** and sign in (GitHub login is easiest).
3. **New +  →  Blueprint**.
4. Connect the `distributed-rate-limiter-django` repo. Render detects
   `render.yaml` and shows the two resources.
5. Click **Apply**. Render builds the Docker image, provisions Redis, and wires
   `REDIS_URL` automatically. First build takes ~3–5 min.
6. When it's live you get a URL like `https://rate-limiter-gateway.onrender.com`.

> If your Render account shows the datastore as **"Redis"** rather than
> **"Key Value"**, change `type: keyvalue` to `type: redis` in `render.yaml`
> (property stays `connectionString`), commit, and re-sync the blueprint.

## Verify it's live

```bash
BASE=https://rate-limiter-gateway.onrender.com   # your actual URL

# Redis should report "up" (real managed Redis, not fakeredis)
curl $BASE/healthz

# Trip the expensive-endpoint limit (5/min) and see a real 429 + Retry-After
for i in $(seq 1 7); do
  curl -s -o /dev/null -w "%{http_code} " -X POST $BASE/expensive
done; echo

# Inspect the rate-limit headers
curl -i $BASE/ping

# Metrics
curl $BASE/metrics
```

> Free instances **cold-start**: after ~15 min idle the first request can take
> 30–60s while the container wakes. Subsequent requests are fast. Mention this if
> you demo it live.

## Going beyond the demo (protect a real API)

To put this in front of an actual backend instead of the demo upstream, set one
env var on the gateway service in the Render dashboard:

```
UPSTREAM_BASE_URL = https://your-real-api.example.com
```

Then every request to the gateway is rate-limited and forwarded to your API.
For real production you'd also want: a paid instance (no cold starts), Redis with
persistence/HA, and a deliberate `RATELIMIT_FAILURE_MODE` decision for what
happens if Redis is unavailable.
