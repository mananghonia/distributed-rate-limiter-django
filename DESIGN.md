# Design & Trade-offs

A one-page reasoning doc for the distributed rate-limiting gateway. This is the
part that signals judgement rather than just working code — it captures the
"why" behind each decision and the questions the project prepares you to answer.

## 1. Why state must be externalised (the core decision)

Each instance **cannot** just count in its own memory. Run 3 instances with a
limit of 100/min and a client spreading requests across them gets ~300/min,
because each process only sees a third of the traffic. Local state does not
survive horizontal scaling, so the counting state must live in a shared store
that every instance reads and writes — that's Redis.

The single-node in-memory limiter (`backends/memory.py`) is kept in the repo on
purpose: it's the baseline that makes this failure concrete. Set
`RATELIMIT_ALGORITHM=memory`, run two instances, and watch the global limit
break.

## 2. The race condition and its fix (the centerpiece)

Moving the counter to Redis introduces a **read-then-write race**. If instance A
reads the counter as 99, and instance B also reads 99 before either writes back
100, both allow the request and the limit is exceeded — two instances, same
millisecond, both think they're request #100.

The fix is **atomicity**: the read, the increment, and the limit check must be
one indivisible operation. We do this with **Lua scripts that Redis executes
atomically server-side** (`ratelimiter/lua/*.lua`). Redis runs a script to
completion as a single unit — no other command interleaves — so the check-and-
increment can't race regardless of how many instances hit the same key at once.

The concurrency test (`ConcurrencyTests.test_no_overcount_under_concurrency`)
fires 500 concurrent requests at a limit of 100 and asserts **exactly** 100 are
allowed. The load test reproduces the same result over HTTP under 50 threads.

> Alternative without Lua: `INCR` + `EXPIRE` is atomic per-command, and works
> for fixed-window. But sliding-window and token-bucket need to read several
> fields, compute, and conditionally write — multiple commands that must be
> atomic *together*. That's exactly what a Lua script (or a `MULTI`/`WATCH`
> optimistic-locking retry loop) gives you. Lua is chosen for being simpler and
> contention-free versus retry loops.

## 3. Algorithm choice

| Algorithm | How it counts | Trade-off |
| --- | --- | --- |
| **Fixed window** | requests per calendar window; `INCR` + expiry | Simplest, but the **boundary burst** flaw: 100 requests at 11:00:59 + 100 at 11:01:00 = 200 in two seconds. |
| **Sliding window log** | a timestamp per request, count the trailing 60s | Perfectly accurate, but stores every timestamp → memory-heavy. |
| **Sliding window counter** | current count + previous window count × overlap fraction | **The pragmatic default.** Fixes the boundary burst without the memory of a full log. What most production systems use. |
| **Token bucket** | tokens refill at a steady rate up to a cap; each request spends one | Best when you want to **allow bursts** up to a ceiling while capping the sustained rate. Why API companies favour it. |

**Default: sliding window counter.** In one sentence: *it fixes the fixed-window
boundary burst without the memory cost of a full log.* Token bucket is offered as
a second implementation for when bursts should be explicitly allowed (the `paid`
tier sets `burst=200`).

All three are implemented and switchable via `RATELIMIT_ALGORITHM`, so they can
be compared directly.

## 4. Identity: what defines "a client"?

| Option | Pro | Con |
| --- | --- | --- |
| **IP address** | zero client effort | breaks behind shared NAT / corporate proxies (many users → one bucket); spoofable via `X-Forwarded-For` unless the edge is trusted |
| **API key** | accurate, per-customer, enables tiering | requires clients to authenticate |
| **User ID** | most precise | requires a session/auth layer |

We **prefer the API key** (`X-API-Key`) and fall back to **IP** for anonymous
traffic. The key also selects the tier (free/paid), which drives the limit.
Limits are additionally **per-endpoint**: an expensive `POST` gets a tighter
budget than a cheap `GET`. Redis keys are namespaced `rl:{identity}:{rule}` so
different policies for the same client never share a counter (the `{}` braces
also keep a key on one node under Redis Cluster hash-slotting).

## 5. Client-facing behaviour

A well-behaved limiter tells clients how to back off instead of just slamming the
door. Modelled on GitHub's / Stripe's headers:

- `RateLimit-Limit` — the ceiling for the window
- `RateLimit-Remaining` — requests left before rejection
- `RateLimit-Reset` — seconds until the window resets
- `Retry-After` — (on `429`) seconds to wait before retrying

These ride on both allowed responses and the `429`.

## 6. Fail-open vs fail-closed (no universally right answer)

If Redis goes down, do we **allow** all traffic (available, but unprotected) or
**reject** all traffic (protected, but the API is effectively down)?

- **Fail-open (default)** — a limiter is usually a guard in front of an otherwise
  healthy backend; taking the whole API down to protect it is normally the worse
  outcome.
- **Fail-closed** — appropriate when the thing behind the limiter genuinely
  cannot survive unbounded traffic (e.g. a fragile third-party dependency you're
  shielding, or abuse/DDoS protection).

It's a config flag (`RATELIMIT_FAILURE_MODE`) precisely because the right call
depends on what you're protecting. Both paths are tested.

## 7. Scaling further

- **Redis is the bottleneck / SPOF.** Mitigations: Redis replication + Sentinel
  or Cluster; shard keys by identity (the hash-tag `{}` keeps a client's keys on
  one slot); or a two-tier scheme where each instance keeps a small local budget
  and reconciles with Redis periodically (trades a little accuracy for far fewer
  round-trips).
- **Latency.** Each check is one round-trip running an O(1) script — single-digit
  ms. Pipelining and connection pooling keep it flat under load.
- **Hot keys.** A single very high-volume client can hot-spot one Redis node;
  local pre-aggregation or approximate counting relieves it.

## Questions this project prepares you for

- Design a rate limiter.
- How do you share state across instances?
- How do you prevent race conditions in a distributed counter? *(atomic Lua)*
- Fixed vs sliding window trade-offs?
- What happens when Redis dies? *(fail-open vs fail-closed)*
- How would you scale this to a million users?
