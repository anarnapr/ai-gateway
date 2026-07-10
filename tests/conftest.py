from __future__ import annotations

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeredis_aioredis

from app.config import Settings
from app.pool.key_pool import AsyncAPIKeyPool
from app.pool.redis_keys import RedisKeys
from app.tracking.call_tracker import CallTracker

GEMINI_MODEL_PRIORITY = [
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-flash-preview",
    "gemini-2.5-flash",
]
GEMINI_MODEL_ALIASES = {"gemini-3.1": "gemini-3.1-flash-preview"}
GEMINI_QUOTA_TABLE = {
    "gemini-3.1-flash-lite-preview": {"rpm": 15, "tpm": 1000000, "rpd": 500},
    "gemini-3.1-flash-preview": {"rpm": 15, "tpm": 1000000, "rpd": 500},
    "gemini-2.5-flash": {"rpm": 15, "tpm": 1000000, "rpd": 500},
}


@pytest_asyncio.fixture
async def fake_redis():
    client = fakeredis_aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        gemini_api_keys="key-aaaa1111,key-bbbb2222,key-cccc3333",
        redis_url="redis://localhost:6379/0",
        redis_key_prefix="testns",
        max_in_flight=4,
        default_rpm=15,
        dead_cooldown_seconds=3600.0,
    )


@pytest.fixture
def redis_keys(settings) -> RedisKeys:
    return RedisKeys(settings.redis_key_prefix)


@pytest_asyncio.fixture
async def key_pool(fake_redis, settings) -> AsyncAPIKeyPool:
    return AsyncAPIKeyPool(
        redis_client=fake_redis,
        api_keys_string=settings.gemini_api_keys,
        model_priority=GEMINI_MODEL_PRIORITY,
        settings=settings,
        rpm=settings.default_rpm,
    )


@pytest.fixture
def call_tracker(fake_redis, redis_keys) -> CallTracker:
    return CallTracker(
        redis_client=fake_redis,
        redis_keys=redis_keys,
        quota_table=GEMINI_QUOTA_TABLE,
        model_aliases=GEMINI_MODEL_ALIASES,
        service="gemini",
    )


@pytest.fixture
def api_client(monkeypatch, tmp_path):
    """A TestClient wired to a shared FakeRedis instance instead of a real Redis
    server, with a short acquire_key wait budget so pool-exhaustion tests run fast.
    Logs/uploads are redirected under pytest's tmp_path so test runs never write into
    the real tmp/ai/ directory a locally running dev server also reads from.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("GEMINI_API_KEYS", "key-aaaa1111,key-bbbb2222")
    monkeypatch.setenv("ACQUIRE_KEY_MAX_WAIT_SECONDS", "1.0")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))

    from app.config import get_settings

    get_settings.cache_clear()

    import app.redis_client as redis_client_module

    fake = fakeredis_aioredis.FakeRedis(decode_responses=True)
    redis_client_module._client = fake

    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as client:
        client.state_redis = fake  # convenience handle for tests that need direct access
        yield client

    redis_client_module._client = None
    get_settings.cache_clear()
