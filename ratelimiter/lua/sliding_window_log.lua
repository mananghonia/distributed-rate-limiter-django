-- Sliding-window *log* -- the perfectly-accurate (but memory-heavier) sibling of
-- the sliding-window counter. It stores a timestamp per request in a sorted set
-- and counts how many fall inside the trailing window. No boundary artifacts at
-- all: the count is always the exact number of requests in the last `window`
-- seconds. The cost is O(N) memory per client (one entry per request in-window),
-- which is why the counter approximation is usually preferred in production.
--
-- Atomic, like the others: prune-count-add happens as one indivisible unit.
--
-- KEYS[1] = sorted-set key
-- ARGV[1] = limit
-- ARGV[2] = window seconds
-- ARGV[3] = now (float seconds)
-- ARGV[4] = unique member id (so identical-timestamp requests don't collapse)
-- returns {allowed(0/1), remaining, reset_after, retry_after}
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local member = ARGV[4]

-- Drop everything older than the trailing window.
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)

local count = redis.call('ZCARD', KEYS[1])
local allowed = 0
if count < limit then
  allowed = 1
  redis.call('ZADD', KEYS[1], now, member)
  count = count + 1
end
redis.call('EXPIRE', KEYS[1], math.ceil(window))

local remaining = limit - count
if remaining < 0 then
  remaining = 0
end

-- reset_after: when the oldest in-window request ages out (freeing a slot).
local reset_after = math.ceil(window)
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
if oldest[2] then
  reset_after = math.ceil((tonumber(oldest[2]) + window) - now)
  if reset_after < 0 then
    reset_after = 0
  end
end

local retry_after = 0
if allowed == 0 then
  retry_after = reset_after
end

return {allowed, remaining, reset_after, retry_after}
