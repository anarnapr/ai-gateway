-- Atomic prune + check + reserve against a per-key-model RPM cap, mirroring the original
-- APIKeyPool's per-key deque of request timestamps (60s sliding window).
-- KEYS[1] = rpm zset key (member = unique token, score = request timestamp)
-- ARGV[1] = now (float seconds)
-- ARGV[2] = rpm limit (int)
-- ARGV[3] = token (unique member id for this reservation)
-- returns {acquired (1/0), oldest_timestamp_in_window_or_-1}
local key = KEYS[1]
local now = tonumber(ARGV[1])
local rpm = tonumber(ARGV[2])
local token = ARGV[3]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - 60)

local count = redis.call('ZCARD', key)
if count >= rpm then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local oldest_ts = -1
    if oldest[2] then
        oldest_ts = tonumber(oldest[2])
    end
    return {0, oldest_ts}
end

redis.call('ZADD', key, now, token)
redis.call('EXPIRE', key, 120)
return {1, -1}
