import time

import pytest

from app.models.enums import FailureReason, KeyStatus
from app.pool.key_pool import AsyncAPIKeyPool
from app.providers.base import FailureClassification
from tests.conftest import GEMINI_MODEL_PRIORITY


@pytest.mark.asyncio
async def test_cooldown_survives_new_pool_instance(fake_redis, settings):
    keys = "key-aaaa1111,key-bbbb2222"
    pool_a = AsyncAPIKeyPool(fake_redis, keys, GEMINI_MODEL_PRIORITY, settings)

    classification = FailureClassification(reason=FailureReason.RATE_LIMIT, scope="key_model")
    await pool_a.report_failure("key-aaaa1111", GEMINI_MODEL_PRIORITY[0], classification)

    # Simulate a process restart: brand-new pool object, same underlying Redis.
    pool_b = AsyncAPIKeyPool(fake_redis, keys, GEMINI_MODEL_PRIORITY, settings)
    status, retry_in = await pool_b.classify_key_status("key-aaaa1111", GEMINI_MODEL_PRIORITY[0], time.time())
    assert status == KeyStatus.RATE_LIMITED.value
    assert retry_in > 0

    # The other key is untouched.
    status_b, _ = await pool_b.classify_key_status("key-bbbb2222", GEMINI_MODEL_PRIORITY[0], time.time())
    assert status_b == KeyStatus.AVAILABLE.value


@pytest.mark.asyncio
async def test_acquire_and_release_key(key_pool: AsyncAPIKeyPool):
    key, model = await key_pool.acquire_key()
    assert key in key_pool.api_keys
    assert model == GEMINI_MODEL_PRIORITY[0]

    status, _ = await key_pool.classify_key_status(key, model, time.time())
    assert status == KeyStatus.IN_USE.value

    await key_pool.release_key(key)
    status_after, _ = await key_pool.classify_key_status(key, model, time.time())
    assert status_after == KeyStatus.AVAILABLE.value


@pytest.mark.asyncio
async def test_acquire_key_honors_pinned_model(key_pool: AsyncAPIKeyPool):
    pinned = GEMINI_MODEL_PRIORITY[-1]  # not the first-priority model
    key, model = await key_pool.acquire_key(model=pinned)
    assert key in key_pool.api_keys
    assert model == pinned


@pytest.mark.asyncio
async def test_acquire_key_pinned_model_does_not_fall_back(key_pool: AsyncAPIKeyPool):
    pinned = GEMINI_MODEL_PRIORITY[0]
    other_model = GEMINI_MODEL_PRIORITY[1]

    # Blacklist only the pinned model, leave the rest of the priority list healthy.
    for api_key in key_pool.api_keys:
        classification = FailureClassification(reason=FailureReason.QUOTA_EXHAUSTED, scope="key_model")
        await key_pool.report_failure(api_key, pinned, classification)

    # No pin: pool falls back to the next model in priority order as usual.
    key, model = await key_pool.acquire_key()
    assert model == other_model

    # Pinned: no candidate models at all (the one model asked for is cooled down),
    # so acquire_key gives up rather than silently substituting a different model.
    key2, model2 = await key_pool.acquire_key(model=pinned, max_wait_seconds=0.01)
    assert key2 is None
    assert model2 is None


@pytest.mark.asyncio
async def test_acquire_key_skips_leased_keys(key_pool: AsyncAPIKeyPool):
    key1, model1 = await key_pool.acquire_key()
    key2, model2 = await key_pool.acquire_key()
    assert key1 != key2
    await key_pool.release_key(key1)
    await key_pool.release_key(key2)


@pytest.mark.asyncio
async def test_auth_dead_marks_key_globally_dead(key_pool: AsyncAPIKeyPool):
    api_key = key_pool.api_keys[0]
    classification = FailureClassification(reason=FailureReason.AUTH_DEAD, scope="key")
    await key_pool.report_failure(api_key, GEMINI_MODEL_PRIORITY[0], classification)

    status, retry_in = await key_pool.classify_key_status(api_key, GEMINI_MODEL_PRIORITY[0], time.time())
    assert status == KeyStatus.DEAD_AUTH.value
    assert retry_in <= 3600.0


@pytest.mark.asyncio
async def test_quota_exhausted_blacklists_model_once_all_keys_exhausted(fake_redis, settings):
    keys = "key-aaaa1111,key-bbbb2222"
    pool = AsyncAPIKeyPool(fake_redis, keys, GEMINI_MODEL_PRIORITY, settings)
    model = GEMINI_MODEL_PRIORITY[0]
    classification = FailureClassification(reason=FailureReason.QUOTA_EXHAUSTED, scope="key_model")

    await pool.report_failure("key-aaaa1111", model, classification)
    candidates = await pool._get_candidate_models(time.time())
    assert model in candidates  # only one of two keys exhausted so far

    await pool.report_failure("key-bbbb2222", model, classification)
    candidates_after = await pool._get_candidate_models(time.time())
    assert model not in candidates_after  # both keys exhausted -> model blacklisted


@pytest.mark.asyncio
async def test_record_success_clears_failure_state(key_pool: AsyncAPIKeyPool):
    api_key = key_pool.api_keys[0]
    model = GEMINI_MODEL_PRIORITY[0]
    classification = FailureClassification(reason=FailureReason.RATE_LIMIT, scope="key_model")
    await key_pool.report_failure(api_key, model, classification)

    status, _ = await key_pool.classify_key_status(api_key, model, time.time())
    assert status == KeyStatus.RATE_LIMITED.value

    await key_pool.record_success(api_key, model)
    status_after, _ = await key_pool.classify_key_status(api_key, model, time.time())
    assert status_after == KeyStatus.AVAILABLE.value


@pytest.mark.asyncio
async def test_not_found_blacklists_model_for_all_keys(key_pool: AsyncAPIKeyPool):
    model = GEMINI_MODEL_PRIORITY[0]
    classification = FailureClassification(reason=FailureReason.NOT_FOUND, scope="model")
    await key_pool.report_failure(key_pool.api_keys[0], model, classification)

    candidates = await key_pool._get_candidate_models(time.time())
    assert model not in candidates


@pytest.mark.asyncio
async def test_rate_limit_trips_model_circuit_breaker_across_keys(fake_redis, settings):
    # Large pool: RATE_LIMIT only cools the one key that failed, so with many keys
    # _get_candidate_models() would otherwise keep finding a different "available" key
    # on the same saturated model almost indefinitely. The breaker should trip on
    # failure *velocity* across the model, well before every key is individually dead.
    settings.model_circuit_breaker_threshold = 3
    settings.model_circuit_breaker_window_seconds = 30.0
    settings.model_circuit_breaker_cooldown_seconds = 20.0
    keys = ",".join(f"key-{i:04d}" for i in range(10))
    pool = AsyncAPIKeyPool(fake_redis, keys, GEMINI_MODEL_PRIORITY, settings)
    model = GEMINI_MODEL_PRIORITY[0]
    classification = FailureClassification(reason=FailureReason.RATE_LIMIT, scope="key_model")

    for i in range(2):
        await pool.report_failure(f"key-{i:04d}", model, classification)
    candidates = await pool._get_candidate_models(time.time())
    assert model in candidates  # below threshold, and 8 of 10 keys still untouched

    await pool.report_failure("key-0002", model, classification)
    candidates_after = await pool._get_candidate_models(time.time())
    assert model not in candidates_after  # 3rd hit within the window trips the breaker

    fallback = await pool._get_candidate_models(time.time())
    assert GEMINI_MODEL_PRIORITY[1] in fallback  # next priority model still usable


@pytest.mark.asyncio
async def test_unknown_failure_does_not_cool_key_or_model(key_pool: AsyncAPIKeyPool):
    # Unclassified failures must stay a no-op here — the jobs worker relies on them
    # propagating as a real exception (item-level retry -> "generate_failed") rather
    # than being absorbed into the pool's much larger capacity-retry budget.
    api_key = key_pool.api_keys[0]
    model = GEMINI_MODEL_PRIORITY[0]
    classification = FailureClassification(reason=FailureReason.UNKNOWN, scope="key_model")
    await key_pool.report_failure(api_key, model, classification)

    status, retry_in = await key_pool.classify_key_status(api_key, model, time.time())
    assert status == KeyStatus.AVAILABLE.value
    assert retry_in == 0.0
