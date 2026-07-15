import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio

import app.jobs.worker as worker_module
from app.errors import PoolExhaustedHTTPError
from app.jobs.store import JobStore
from app.jobs.worker import JobWorkerPool
from app.models.jobs import ItemStatus
from app.models.responses import GenerateResponse
from app.pool.redis_keys import RedisKeys


def _resp(text: str = "ok") -> GenerateResponse:
    return GenerateResponse(
        request_id="r", provider="gemini", model="m", text=text,
        input_tokens=3, output_tokens=2, total_tokens=5,
        api_key_suffix="abcd", attempts=1, latency_ms=10.0,
    )


@pytest_asyncio.fixture
async def store(fake_redis, settings) -> JobStore:
    # Fast retries so failure-path tests don't sleep for real.
    settings.jobs_retry_delay_seconds = 0.01
    settings.jobs_retry_max_delay_seconds = 0.02
    return JobStore(fake_redis, RedisKeys(settings.redis_key_prefix), settings)


def _pool(store, settings) -> JobWorkerPool:
    return JobWorkerPool(
        store=store,
        providers={"gemini": object()},
        pools={"gemini": object()},
        trackers={"gemini": object()},
        rate_limiters={"gemini": object()},
        usage_logger=object(),
        settings=settings,
    )


async def _submit_one(store, has_media: bool = False) -> tuple[str, str, str]:
    batch_id, _ = await store.create_batch(
        "gemini",
        [{"item_id": "it", "request_json": json.dumps({"provider": "gemini", "prompt": "p"}), "metadata": None, "has_media": has_media}],
    )
    if not has_media:
        b, item_id, entry = await store.claim_next()
        return b, item_id, entry
    return batch_id, "it", None  # caller attaches media then claims


@pytest.mark.asyncio
async def test_process_item_success_writes_result_and_cleans_media(store, settings, monkeypatch):
    b, item_id, _ = await _submit_one(store, has_media=True)

    # Matches the real per-item layout upload_item_media() writes to.
    media_dir = Path(settings.uploads_dir) / "jobs" / b / item_id
    media_dir.mkdir(parents=True)
    media_file = media_dir / "clip.mp4"
    media_file.write_bytes(b"vid")

    await store.attach_media_and_enqueue(b, item_id, str(media_file))
    _, _, entry = await store.claim_next()

    async def fake_run_generate(**kwargs):
        assert kwargs["media_path"] == str(media_file)
        assert kwargs["deadline_seconds"] == settings.jobs_item_deadline_seconds
        return _resp("described")

    monkeypatch.setattr(worker_module, "run_generate", fake_run_generate)

    await _pool(store, settings)._process_item(b, item_id, entry)

    item = await store.get_item(b, item_id)
    assert item["status"] == ItemStatus.SUCCEEDED.value
    assert item["text"] == "described"
    assert item["total_tokens"] == 5 and item["attempts"] == 1
    assert not media_dir.exists()  # per-item upload dir removed
    batch = await store.get_batch_status(b)
    assert batch["status"] == "completed"


@pytest.mark.asyncio
async def test_process_item_downloads_media_urls_before_generate_and_cleans_up(store, settings, monkeypatch):
    async def fake_download_media(url, dest_dir, *, max_bytes, timeout_seconds):
        path = Path(dest_dir) / "photo.jpg"
        path.write_bytes(b"fake-bytes")
        return path

    monkeypatch.setattr(worker_module, "download_media", fake_download_media)

    async def fake_run_generate(**kwargs):
        assert kwargs["media_path"] is None
        assert kwargs["media_paths"] and len(kwargs["media_paths"]) == 2
        return _resp("described 2 images")

    monkeypatch.setattr(worker_module, "run_generate", fake_run_generate)

    batch_id, _ = await store.create_batch(
        "gemini",
        [{
            "item_id": "it",
            "request_json": json.dumps({"provider": "gemini", "prompt": "p"}),
            "metadata": None,
            "has_media": False,
            "media_urls": ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"],
        }],
    )
    b, item_id, entry = await store.claim_next()

    await _pool(store, settings)._process_item(b, item_id, entry)

    item = await store.get_item(b, item_id)
    assert item["status"] == ItemStatus.SUCCEEDED.value
    assert item["text"] == "described 2 images"
    media_dir = Path(settings.uploads_dir) / "jobs" / b / item_id
    assert not media_dir.exists()  # downloaded files cleaned up


@pytest.mark.asyncio
async def test_process_item_media_url_download_failure_fails_item_without_retry(store, settings, monkeypatch):
    from app.media_fetch import MediaDownloadError

    calls = {"download": 0}

    async def fake_download_media(url, dest_dir, *, max_bytes, timeout_seconds):
        calls["download"] += 1
        raise MediaDownloadError("media_url returned HTTP 404")

    monkeypatch.setattr(worker_module, "download_media", fake_download_media)

    async def fail_if_called(**kwargs):
        raise AssertionError("run_generate should not be called when media download fails")

    monkeypatch.setattr(worker_module, "run_generate", fail_if_called)

    batch_id, _ = await store.create_batch(
        "gemini",
        [{
            "item_id": "it",
            "request_json": json.dumps({"provider": "gemini", "prompt": "p"}),
            "metadata": None,
            "has_media": False,
            "media_urls": ["https://cdn.example.com/missing.jpg"],
        }],
    )
    b, item_id, entry = await store.claim_next()

    await _pool(store, settings)._process_item(b, item_id, entry)

    item = await store.get_item(b, item_id)
    assert item["status"] == ItemStatus.FAILED.value
    assert item["error_code"] == "media_fetch_failed"
    assert "404" in item["error"]
    assert calls["download"] == 1  # not retried


@pytest.mark.asyncio
async def test_process_item_fails_after_max_attempts(store, settings, monkeypatch):
    calls = {"n": 0}

    async def always_fail(**kwargs):
        calls["n"] += 1
        raise RuntimeError("500 kaput")

    monkeypatch.setattr(worker_module, "run_generate", always_fail)
    b, item_id, entry = await _submit_one(store)

    await _pool(store, settings)._process_item(b, item_id, entry)

    assert calls["n"] == settings.jobs_item_max_attempts
    item = await store.get_item(b, item_id)
    assert item["status"] == ItemStatus.FAILED.value
    assert item["error_code"] == "generate_failed"
    assert item["attempts"] == settings.jobs_item_max_attempts
    assert "kaput" in item["error"]


@pytest.mark.asyncio
async def test_pool_exhausted_uses_capacity_budget_not_attempts(store, settings, monkeypatch):
    settings.jobs_capacity_max_retries = 2
    calls = {"n": 0}

    async def exhausted_then_ok(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PoolExhaustedHTTPError(request_id="r", retry_after_seconds=0.01)
        return _resp("finally")

    monkeypatch.setattr(worker_module, "run_generate", exhausted_then_ok)
    b, item_id, entry = await _submit_one(store)

    await _pool(store, settings)._process_item(b, item_id, entry)

    item = await store.get_item(b, item_id)
    assert item["status"] == ItemStatus.SUCCEEDED.value
    assert item["attempts"] == 1  # capacity waits did NOT burn real attempts


@pytest.mark.asyncio
async def test_pool_exhausted_over_budget_fails_with_code(store, settings, monkeypatch):
    settings.jobs_capacity_max_retries = 1

    async def always_exhausted(**kwargs):
        raise PoolExhaustedHTTPError(request_id="r", retry_after_seconds=0.01)

    monkeypatch.setattr(worker_module, "run_generate", always_exhausted)
    b, item_id, entry = await _submit_one(store)

    await _pool(store, settings)._process_item(b, item_id, entry)

    item = await store.get_item(b, item_id)
    assert item["status"] == ItemStatus.FAILED.value
    assert item["error_code"] == "pool_exhausted"


@pytest.mark.asyncio
async def test_cancellation_requeues_item(store, settings, monkeypatch, fake_redis, redis_keys):
    started = asyncio.Event()

    async def hang_forever(**kwargs):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(worker_module, "run_generate", hang_forever)
    await store.create_batch(
        "gemini",
        [{"item_id": "it", "request_json": json.dumps({"provider": "gemini", "prompt": "p"}), "metadata": None, "has_media": False}],
    )

    pool = _pool(store, settings)
    task = asyncio.create_task(pool._worker_loop(0))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Item back on the queue, nothing lost, no lease held.
    assert await store.queue_length() == 1
    assert await fake_redis.llen(redis_keys.jobs_processing()) == 0


@pytest.mark.asyncio
async def test_expired_item_hash_is_dropped(store, settings, fake_redis, redis_keys):
    b, item_id, entry = await _submit_one(store)
    await fake_redis.delete(redis_keys.jobs_item(b, item_id))  # TTL expiry

    await _pool(store, settings)._process_item(b, item_id, entry)

    assert await fake_redis.llen(redis_keys.jobs_processing()) == 0
    assert await store.queue_length() == 0


@pytest.mark.asyncio
async def test_worker_pool_start_stop_drains(store, settings, monkeypatch):
    settings.jobs_worker_concurrency = 2
    settings.jobs_poll_interval_seconds = 0.01
    settings.jobs_reaper_interval_seconds = 0.05
    settings.jobs_shutdown_grace_seconds = 1.0

    async def quick(**kwargs):
        return _resp()

    monkeypatch.setattr(worker_module, "run_generate", quick)
    batch_id, _ = await store.create_batch(
        "gemini",
        [{"item_id": f"i{n}", "request_json": json.dumps({"provider": "gemini", "prompt": "p"}), "metadata": None, "has_media": False} for n in range(5)],
    )

    pool = _pool(store, settings)
    pool.start()
    deadline = asyncio.get_event_loop().time() + 5
    while asyncio.get_event_loop().time() < deadline:
        status = await store.get_batch_status(batch_id)
        if status["status"] == "completed":
            break
        await asyncio.sleep(0.02)
    await pool.stop()

    status = await store.get_batch_status(batch_id)
    assert status["status"] == "completed"
    assert status["counts"]["succeeded"] == 5
