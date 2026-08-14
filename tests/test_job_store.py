import json

import pytest
import pytest_asyncio

from app.jobs.store import JobStore
from app.models.jobs import BatchStatus, ItemStatus
from app.pool.redis_keys import RedisKeys


def _items(n: int, has_media: bool = False) -> list[dict]:
    return [
        {
            "item_id": f"item-{i}",
            "request_json": json.dumps({"provider": "gemini", "prompt": f"p{i}"}),
            "metadata": {"idx": i},
            "has_media": has_media,
        }
        for i in range(n)
    ]


@pytest_asyncio.fixture
async def store(fake_redis, settings) -> JobStore:
    return JobStore(fake_redis, RedisKeys(settings.redis_key_prefix), settings)


@pytest.mark.asyncio
async def test_create_batch_enqueues_text_items_in_order(store, fake_redis, redis_keys):
    batch_id, statuses = await store.create_batch("gemini", _items(3))

    assert [s for _, s in statuses] == [ItemStatus.QUEUED.value] * 3
    assert await store.queue_length() == 3
    # Submit order preserved in the batch items list.
    assert await fake_redis.lrange(redis_keys.jobs_batch_items(batch_id), 0, -1) == [
        "item-0",
        "item-1",
        "item-2",
    ]
    batch = await fake_redis.hgetall(redis_keys.jobs_batch(batch_id))
    assert batch["status"] == BatchStatus.PENDING.value
    assert int(batch["total"]) == 3 and int(batch["queued"]) == 3
    # TTLs set on batch + item keys (abandoned batches self-GC).
    assert await fake_redis.ttl(redis_keys.jobs_batch(batch_id)) > 0
    assert await fake_redis.ttl(redis_keys.jobs_item(batch_id, "item-0")) > 0


@pytest.mark.asyncio
async def test_media_items_wait_until_media_attached(store, fake_redis, redis_keys):
    batch_id, statuses = await store.create_batch("gemini", _items(1, has_media=True))

    assert statuses == [("item-0", ItemStatus.AWAITING_MEDIA.value)]
    assert await store.queue_length() == 0

    await store.attach_media_and_enqueue(batch_id, "item-0", "/tmp/x.mp4")

    assert await store.queue_length() == 1
    item = await fake_redis.hgetall(redis_keys.jobs_item(batch_id, "item-0"))
    assert item["status"] == ItemStatus.QUEUED.value
    assert item["media_path"] == "/tmp/x.mp4"
    batch = await fake_redis.hgetall(redis_keys.jobs_batch(batch_id))
    assert int(batch["awaiting_media"]) == 0 and int(batch["queued"]) == 1


@pytest.mark.asyncio
async def test_claim_moves_entry_and_sets_lease(store, fake_redis, redis_keys):
    batch_id, _ = await store.create_batch("gemini", _items(2))

    claim = await store.claim_next()
    assert claim is not None
    b, item_id, entry = claim
    assert b == batch_id and item_id == "item-0"  # FIFO: LPUSH + RIGHT-pop
    assert await store.queue_length() == 1
    assert await fake_redis.lrange(redis_keys.jobs_processing(), 0, -1) == [entry]
    assert await fake_redis.exists(redis_keys.jobs_lease(batch_id, item_id))


@pytest.mark.asyncio
async def test_claim_next_empty_queue_returns_none(store):
    assert await store.claim_next() is None


@pytest.mark.asyncio
async def test_finish_item_success_counters_and_completion(store, fake_redis, redis_keys):
    batch_id, _ = await store.create_batch("gemini", _items(2))

    for expect_completed in (False, True):
        b, item_id, entry = await store.claim_next()
        await store.mark_running(b, item_id)
        completed = await store.finish_item(
            b, item_id, entry, success=True, result_fields={"text": "hi", "total_tokens": 5}
        )
        assert completed is expect_completed

    batch = await fake_redis.hgetall(redis_keys.jobs_batch(batch_id))
    assert batch["status"] == BatchStatus.COMPLETED.value
    assert int(batch["succeeded"]) == 2 and int(batch["running"]) == 0
    assert await fake_redis.llen(redis_keys.jobs_processing()) == 0
    # Completion refreshed TTL down to result TTL (<= 24h, > 0).
    ttl = await fake_redis.ttl(redis_keys.jobs_batch(batch_id))
    assert 0 < ttl <= store.settings.jobs_result_ttl_seconds


@pytest.mark.asyncio
async def test_finish_item_failure_records_error(store, fake_redis, redis_keys):
    batch_id, _ = await store.create_batch("gemini", _items(1))
    b, item_id, entry = await store.claim_next()
    await store.mark_running(b, item_id)

    completed = await store.finish_item(
        b, item_id, entry, success=False, error="boom" * 500, error_code="generate_failed"
    )

    assert completed is True
    item = await store.get_item(batch_id, item_id)
    assert item["status"] == ItemStatus.FAILED.value
    assert item["error_code"] == "generate_failed"
    assert len(item["error"]) <= 500


@pytest.mark.asyncio
async def test_reap_stale_requeues_dead_lease_entries(store, fake_redis, redis_keys):
    batch_id, _ = await store.create_batch("gemini", _items(1))
    b, item_id, entry = await store.claim_next()
    await store.mark_running(b, item_id)

    # Simulate worker crash: lease vanishes, entry stuck in processing.
    await fake_redis.delete(redis_keys.jobs_lease(b, item_id))

    reaped = await store.reap_stale()

    assert reaped == 1
    assert await store.queue_length() == 1
    assert await fake_redis.llen(redis_keys.jobs_processing()) == 0
    # Running counter rolled back to queued.
    item = await fake_redis.hgetall(redis_keys.jobs_item(batch_id, item_id))
    assert item["status"] == ItemStatus.QUEUED.value
    batch = await fake_redis.hgetall(redis_keys.jobs_batch(batch_id))
    assert int(batch["running"]) == 0 and int(batch["queued"]) == 1


@pytest.mark.asyncio
async def test_reap_stale_skips_live_leases_and_drops_expired_items(store, fake_redis, redis_keys):
    batch_id, _ = await store.create_batch("gemini", _items(2))
    b1, i1, e1 = await store.claim_next()  # live lease — untouched
    b2, i2, e2 = await store.claim_next()
    await fake_redis.delete(redis_keys.jobs_lease(b2, i2))
    await fake_redis.delete(redis_keys.jobs_item(b2, i2))  # simulate TTL expiry

    reaped = await store.reap_stale()

    assert reaped == 0  # live one skipped, expired one dropped (not requeued)
    assert await fake_redis.lrange(redis_keys.jobs_processing(), 0, -1) == [e1]
    assert await store.queue_length() == 0


@pytest.mark.asyncio
async def test_get_batch_status_shape(store):
    batch_id, _ = await store.create_batch("gemini", _items(2))
    b, item_id, entry = await store.claim_next()
    await store.mark_running(b, item_id)
    await store.finish_item(
        b,
        item_id,
        entry,
        success=True,
        result_fields={"text": "out", "input_tokens": 3, "output_tokens": 2, "total_tokens": 5, "api_key_suffix": "abcd", "latency_ms": 12.5, "attempts": 1},
    )

    status = await store.get_batch_status(batch_id)

    assert status["total"] == 2
    assert status["counts"]["succeeded"] == 1 and status["counts"]["queued"] == 1
    assert [i["item_id"] for i in status["items"]] == ["item-0", "item-1"]
    done = status["items"][0]
    assert done["text"] == "out" and done["total_tokens"] == 5 and done["metadata"] == {"idx": 0}
    assert status["status"] == BatchStatus.RUNNING.value  # not completed yet

    assert await store.get_batch_status("nope") is None
    assert await store.get_item(batch_id, "missing") is None


@pytest.mark.asyncio
async def test_purge_batch_removes_everything(store, fake_redis, redis_keys):
    batch_id, _ = await store.create_batch("gemini", _items(3))
    b, item_id, entry = await store.claim_next()  # item-0 now in processing + leased

    assert await store.purge_batch(batch_id) is True

    assert await fake_redis.exists(redis_keys.jobs_batch(batch_id)) == 0
    assert await fake_redis.exists(redis_keys.jobs_batch_items(batch_id)) == 0
    assert await fake_redis.exists(redis_keys.jobs_item(batch_id, "item-0")) == 0
    assert await fake_redis.exists(redis_keys.jobs_item(batch_id, "item-1")) == 0
    assert await fake_redis.exists(redis_keys.jobs_lease(b, item_id)) == 0
    assert await store.queue_length() == 0  # item-1, item-2 removed from queue
    assert await fake_redis.lrange(redis_keys.jobs_processing(), 0, -1) == []  # item-0 removed
    assert await fake_redis.zscore(redis_keys.jobs_all_batches(), batch_id) is None

    # Purging a batch that doesn't exist (or already purged) is a no-op, not an error.
    assert await store.purge_batch(batch_id) is False
    assert await store.purge_batch("never-existed") is False


@pytest.mark.asyncio
async def test_purge_batch_leaves_other_batches_untouched(store, fake_redis, redis_keys):
    stale_id, _ = await store.create_batch("gemini", _items(2))
    healthy_id, _ = await store.create_batch("gemini", _items(2))

    await store.purge_batch(stale_id)

    assert await fake_redis.exists(redis_keys.jobs_batch(healthy_id)) == 1
    assert await store.queue_length() == 2  # only healthy_id's two items remain
    remaining = await fake_redis.lrange(redis_keys.jobs_queue(), 0, -1)
    assert all(entry.startswith(healthy_id) for entry in remaining)


@pytest.mark.asyncio
async def test_purge_pending_only_touches_batches_with_inflight_work(store, fake_redis, redis_keys):
    # stuck_id: one item claimed (simulates a killed worker leaving processing+lease
    # behind) — this is the exact scenario a killed app container produces.
    stuck_id, _ = await store.create_batch("gemini", _items(1))
    await store.claim_next()

    # done_id: fully completed, nothing left in queue/processing — must survive.
    done_id, _ = await store.create_batch("gemini", _items(1))
    b, item_id, entry = await store.claim_next()
    await store.mark_running(b, item_id)
    await store.finish_item(b, item_id, entry, success=True, result_fields={"text": "ok"})

    purged = await store.purge_pending()

    assert purged == [stuck_id]
    assert await fake_redis.exists(redis_keys.jobs_batch(stuck_id)) == 0
    assert await fake_redis.exists(redis_keys.jobs_batch(done_id)) == 1  # untouched
    assert await store.queue_length() == 0
    assert await fake_redis.lrange(redis_keys.jobs_processing(), 0, -1) == []


# --- queue purge on container stop/start (app.main lifespan) ---------------------


@pytest.mark.asyncio
async def test_purge_queue_helper_drops_pending_batches(store, fake_redis, redis_keys):
    from app.main import _purge_queue

    batch_id, _ = await store.create_batch("gemini", _items(3))
    assert await store.queue_length() == 3

    await _purge_queue(store, "shutdown")

    assert await store.queue_length() == 0
    assert await fake_redis.exists(redis_keys.jobs_batch(batch_id)) == 0
    assert await fake_redis.zscore(redis_keys.jobs_all_batches(), batch_id) is None


@pytest.mark.asyncio
async def test_purge_queue_helper_swallows_redis_errors(store, monkeypatch):
    """A dead Redis must not abort boot or shutdown — that's exactly when the app
    still has to come up (or go down) cleanly."""
    from app.main import _purge_queue

    async def _boom():
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(store, "purge_pending", _boom)
    await _purge_queue(store, "startup")  # must not raise


@pytest.mark.asyncio
async def test_queue_survives_shutdown_when_purge_disabled(store, fake_redis, redis_keys):
    """Durable queue is still the default: opting in is required, and with the flags
    off the lifespan never calls _purge_queue, so enqueued work survives a restart.
    Asserts the declared field defaults rather than a live Settings() — the deployed
    compose file sets these env vars, so a live instance is environment-dependent.
    """
    from app.config import Settings

    assert Settings.model_fields["jobs_purge_queue_on_shutdown"].default is False
    assert Settings.model_fields["jobs_purge_queue_on_startup"].default is False

    batch_id, _ = await store.create_batch("gemini", _items(2))
    assert await store.queue_length() == 2
    assert await fake_redis.exists(redis_keys.jobs_batch(batch_id)) == 1


@pytest.mark.asyncio
async def test_purge_on_startup_clears_queue_left_by_a_hard_kill(
    monkeypatch, tmp_path, fake_redis
):
    """Simulates exit 137: items are still in Redis from a killed container, and the
    next boot starts clean because the shutdown hook never ran."""
    import app.redis_client as redis_client_module
    from app.config import get_settings

    monkeypatch.setenv("GEMINI_API_KEYS", "key-aaaa1111")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("JOBS_PURGE_QUEUE_ON_STARTUP", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(redis_client_module, "_client", fake_redis)

    settings = get_settings()
    rk = RedisKeys(settings.redis_key_prefix)
    orphan_store = JobStore(fake_redis, rk, settings)
    batch_id, _ = await orphan_store.create_batch("gemini", _items(2))
    assert await orphan_store.queue_length() == 2

    from app.main import _purge_queue

    assert settings.jobs_purge_queue_on_startup is True
    await _purge_queue(orphan_store, "startup")

    assert await orphan_store.queue_length() == 0
    assert await fake_redis.exists(rk.jobs_batch(batch_id)) == 0
    get_settings.cache_clear()
