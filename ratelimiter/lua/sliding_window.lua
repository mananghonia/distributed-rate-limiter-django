-- Sliding-window *counter* -- the pragmatic production default.
--
-- Fixes the fixed-window boundary-burst flaw (100 requests at 11:00:59 plus 100
-- at 11:01:00 = 200 in two seconds) WITHOUT the memory cost of a sliding-window
-- log that stores a timestamp per request. It approximates the true trailing
-- rate by weighting the previous window's count by how much of it still overlaps
-- the trailing window:
--
--   estimated = current_count + previous_count * (1 - elapsed/window)
--
-- Stored as a single hash {wid, cur, prev}; runs atomically for the same
-- race-free reason as the fixed-window script.
--
-- KEYS[1] = hash key
-- ARGV[1] = limit
-- ARGV[2] = window seconds
-- ARGV[3] = now (float seconds)
-- returns {allowed(0/1), remaining, reset_after, retry_after}
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local window_id = math.floor(now / window)
local elapsed = now - (window_id * window)

local data = redis.call('HMGET', KEYS[1], 'wid', 'cur', 'prev')
local stored_wid = tonumber(data[1])
local cur = tonumber(data[2]) or 0
local prev = tonumber(data[3]) or 0

if stored_wid == nil then
  cur = 0
  prev = 0
elseif stored_wid == window_id then
  -- still in the same window, keep counts
elseif stored_wid == window_id - 1 then
  -- rolled over by exactly one window: last window's count becomes "previous"
  prev = cur
  cur = 0
else
  -- gap of two or more windows: nothing carries over
  prev = 0
  cur = 0
end

local weight = (window - elapsed) / window
local estimated = cur + (prev * weight)

local allowed = 0
if estimated + 1 <= limit then
  allowed = 1
  cur = cur + 1
end

redis.call('HSET', KEYS[1], 'wid', window_id, 'cur', cur, 'prev', prev)
redis.call('EXPIRE', KEYS[1], window * 2)

local used = cur + (prev * weight)
local remaining = math.floor(limit - used)
if remaining < 0 then
  remaining = 0
end

local reset_after = math.ceil(window - elapsed)
local retry_after = 0
if allowed == 0 then
  retry_after = reset_after
end

return {allowed, remaining, reset_after, retry_after}
