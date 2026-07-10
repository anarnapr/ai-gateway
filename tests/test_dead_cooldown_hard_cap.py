import time

import pytest

from app.config import Settings
from app.pool.key_pool import AsyncAPIKeyPool
from app.pool.redis_keys import key_id


def test_settings_clamp_ignores_misconfigured_env():
    settings = Settings(gemini_api_keys="k1", dead_cooldown_seconds=7200.0)
    assert settings.clamped_dead_cooldown_seconds == 3600.0


@pytest.mark.asyncio
async def test_mark_cooldown_clamps_even_if_caller_requests_more(fake_redis, settings):
    settings.dead_cooldown_seconds = 7200.0  # misconfigured, should never leak past 3600
    pool = AsyncAPIKeyPool(
        redis_client=fake_redis,
        api_keys_string="key-aaaa1111",
        model_priority=["gemini-2.5-flash"],
        settings=settings,
    )
    api_key = "key-aaaa1111"

    await pool.mark_cooldown(api_key, seconds=7200.0, reason="auth_dead")

    now = time.time()
    remaining = await pool._read_cooldown(pool.rk.cooldown_key(key_id(api_key)), now)
    assert remaining <= 3600.0 + 1  # small tolerance for test execution time
