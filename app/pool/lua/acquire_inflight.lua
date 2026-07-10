-- KEYS[1] = inflight zset key
-- ARGV[1] = now (float seconds)
-- ARGV[2] = max_in_flight (int)
-- ARGV[3] = slot_ttl_seconds (float) -- stale-slot safety net if a worker dies mid-request
-- ARGV[4] = token (unique member id for this request's slot)
-- returns 1 if acquired, 0 if pool is full
local key = KEYS[1]
local now = tonumber(ARGV[1])
local max_in_flight = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local token = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - ttl)

local count = redis.call('ZCARD', key)
if count >= max_in_flight then
    return 0
end

redis.call('ZADD', key, now, token)
return 1
