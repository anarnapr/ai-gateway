from __future__ import annotations

import asyncio
import time

import redis.asyncio as redis

from app.pool.redis_keys import RedisKeys, key_id


class RateLimiter:
    """Redis-backed port of the original per-key throttle: enforces a minimum interval
    between requests on the same key, plus an RPM cap, shared across worker processes.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        redis_keys: RedisKeys,
        rpm_limit: int = 12,
        min_interval_seconds: float = 5.0,
    ):
        self.redis = redis_client
        self.rk = redis_keys
        self.rpm_limit = max(1, int(rpm_limit))
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))

    def _last_request_key(self, kid: str) -> str:
        return f"{self.rk.prefix}:ratelimit:last:{kid}"

    def _window_key(self, kid: str) -> str:
        return f"{self.rk.prefix}:ratelimit:window:{kid}"

    async def wait_if_needed(self, api_key: str) -> float:
        kid = key_id(api_key)
        now = time.time()

        last_raw = await self.redis.get(self._last_request_key(kid))
        sleep_time = 0.0
        if last_raw:
            interval_wait = self.min_interval_seconds - (now - float(last_raw))
            if interval_wait > 0:
                sleep_time = max(sleep_time, interval_wait)

        window_key = self._window_key(kid)
        await self.redis.zremrangebyscore(window_key, "-inf", now - 60)
        count = await self.redis.zcard(window_key)
        if count >= self.rpm_limit:
            oldest = await self.redis.zrange(window_key, 0, 0, withscores=True)
            if oldest:
                rpm_wait = oldest[0][1] - (now - 60)
                if rpm_wait > 0:
                    sleep_time = max(sleep_time, rpm_wait)

        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

        request_time = time.time()
        await self.redis.zadd(window_key, {str(request_time): request_time})
        await self.redis.expire(window_key, 120)
        await self.redis.set(self._last_request_key(kid), request_time, ex=120)
        return max(0.0, sleep_time)
