-- Token bucket -- best when you want to allow bursts up to a cap while capping
-- the sustained rate. Tokens refill at a steady rate (limit/window per second)
-- up to `capacity` (the burst ceiling); each request spends one token. This is
-- why API companies favour it: a client can briefly burst up to `capacity`,
-- then is smoothed to the refill rate.
--
-- Runs atomically so the refill-compute-spend sequence can't race across
-- instances.
--
-- KEYS[1] = hash key {tokens, ts}
-- ARGV[1] = capacity (burst ceiling)
-- ARGV[2] = refill_rate (tokens per second)
-- ARGV[3] = now (float seconds)
-- ARGV[4] = ttl seconds (idle expiry for the bucket)
-- returns {allowed(0/1), remaining, reset_after, retry_after}
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

-- Lazily refill for the time elapsed since we last touched the bucket.
local elapsed = now - ts
if elapsed < 0 then
  elapsed = 0
end
tokens = math.min(capacity, tokens + (elapsed * refill_rate))
ts = now

local allowed = 0
if tokens >= 1 then
  allowed = 1
  tokens = tokens - 1
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', ts)
redis.call('EXPIRE', KEYS[1], ttl)

local remaining = math.floor(tokens)
local retry_after = 0
if allowed == 0 then
  retry_after = math.ceil((1 - tokens) / refill_rate)
end
local reset_after = math.ceil((capacity - tokens) / refill_rate)

return {allowed, remaining, reset_after, retry_after}
