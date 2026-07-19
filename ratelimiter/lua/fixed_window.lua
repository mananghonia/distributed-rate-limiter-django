-- Fixed-window counter, executed atomically by Redis.
--
-- The whole point of doing this in Lua: Redis runs the script as ONE
-- indivisible unit, so the read (current count), the increment, and the
-- limit check cannot interleave with another instance's request. That is the
-- fix for the classic distributed read-then-write race, where two instances
-- both read "99", both write "100", and both wrongly allow the 100th+1 request.
--
-- KEYS[1] = counter key
-- ARGV[1] = limit
-- ARGV[2] = window seconds
-- returns {allowed(0/1), remaining, reset_after, retry_after}
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])

local current = redis.call('INCR', KEYS[1])
if current == 1 then
  -- First request of the window: start the expiry clock.
  redis.call('EXPIRE', KEYS[1], window)
end

local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then
  ttl = window
end

local allowed = 0
if current <= limit then
  allowed = 1
end

local remaining = limit - current
if remaining < 0 then
  remaining = 0
end

local retry_after = 0
if allowed == 0 then
  retry_after = ttl
end

return {allowed, remaining, ttl, retry_after}
