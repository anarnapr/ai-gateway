import asyncio
import io
import time
from pathlib import Path

from app.providers.base import ProviderResult
from app.providers.gemini.provider import GeminiProvider


def _poll_until_completed(client, batch_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/v1/jobs/{batch_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] == "completed":
            return body
        time.sleep(0.05)
    raise AssertionError(f"Batch {batch_id} did not complete within {timeout}s: {body}")


def _poll_until_terminal(client, batch_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/v1/jobs/{batch_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] in ("completed", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"Batch {batch_id} did not reach a terminal status within {timeout}s: {body}")


def test_text_batch_completes_in_order_with_metadata(jobs_api_client, monkeypatch):
    async def fake_generate(self, ctx):
        return ProviderResult(text=f"echo:{ctx.prompt_text}", input_tokens=1, output_tokens=1, total_tokens=2)

    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    resp = jobs_api_client.post(
        "/v1/jobs",
        json={
            "items": [
                {"item_id": "a", "prompt": "one", "metadata": {"pk": 1}},
                {"item_id": "b", "prompt": "two", "metadata": {"pk": 2}},
                {"item_id": "c", "prompt": "three"},
            ]
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["total"] == 3
    assert all(i["status"] == "queued" for i in body["items"])

    result = _poll_until_completed(jobs_api_client, body["batch_id"])

    assert result["counts"]["succeeded"] == 3
    assert [i["item_id"] for i in result["items"]] == ["a", "b", "c"]  # submit order
    assert result["items"][0]["text"] == "echo:one"
    assert result["items"][0]["metadata"] == {"pk": 1}
    assert result["items"][2]["metadata"] is None
    assert result["finished_at"] is not None


def test_media_item_flow(jobs_api_client, monkeypatch):
    async def fake_requires_upload(self, media_path):
        return False  # inline path; media presence is what we're testing

    async def fake_generate(self, ctx):
        assert len(ctx.media_paths) == 1
        return ProviderResult(text="saw media", total_tokens=3)

    monkeypatch.setattr(GeminiProvider, "requires_file_upload", fake_requires_upload)
    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    resp = jobs_api_client.post(
        "/v1/jobs", json={"items": [{"item_id": "reel1", "prompt": "describe", "has_media": True}]}
    )
    assert resp.status_code == 201
    batch_id = resp.json()["batch_id"]
    assert resp.json()["items"][0]["status"] == "awaiting_media"

    # Not queued yet — workers must not pick it up.
    status = jobs_api_client.get(f"/v1/jobs/{batch_id}").json()
    assert status["counts"]["awaiting_media"] == 1

    up = jobs_api_client.post(
        f"/v1/jobs/{batch_id}/items/reel1/media",
        files={"file": ("clip.mp4", io.BytesIO(b"\x00fakevid"), "video/mp4")},
    )
    assert up.status_code == 200
    assert up.json()["status"] == "queued"

    result = _poll_until_completed(jobs_api_client, batch_id)
    assert result["items"][0]["text"] == "saw media"

    # Worker cleaned the per-item upload dir.
    from app.config import get_settings

    jobs_uploads = Path(get_settings().uploads_dir) / "jobs" / batch_id
    assert not any(jobs_uploads.rglob("*")) if jobs_uploads.exists() else True


def test_media_url_item_flow(jobs_api_client, monkeypatch):
    """media_urls items skip the awaiting_media/upload round-trip entirely — queued
    immediately at submit, worker downloads before generating."""
    import app.jobs.worker as worker_module

    async def fake_download_media(url, dest_dir, *, max_bytes, timeout_seconds):
        path = Path(dest_dir) / "clip.mp4"
        path.write_bytes(b"\x00fakevid")
        return path

    async def fake_requires_upload(self, media_path):
        return False

    async def fake_generate(self, ctx):
        assert len(ctx.media_paths) == 1
        return ProviderResult(text="saw media via url", total_tokens=3)

    monkeypatch.setattr(worker_module, "download_media", fake_download_media)
    monkeypatch.setattr(GeminiProvider, "requires_file_upload", fake_requires_upload)
    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    resp = jobs_api_client.post(
        "/v1/jobs",
        json={"items": [{"item_id": "reel1", "prompt": "describe", "media_urls": ["https://cdn.example.com/reel1.mp4"]}]},
    )
    assert resp.status_code == 201
    # Queued immediately — no awaiting_media step, unlike has_media.
    assert resp.json()["items"][0]["status"] == "queued"

    result = _poll_until_completed(jobs_api_client, resp.json()["batch_id"])
    assert result["items"][0]["text"] == "saw media via url"

    from app.config import get_settings

    jobs_uploads = Path(get_settings().uploads_dir) / "jobs" / resp.json()["batch_id"]
    assert not any(jobs_uploads.rglob("*")) if jobs_uploads.exists() else True


def test_media_url_and_has_media_are_mutually_exclusive(jobs_api_client):
    resp = jobs_api_client.post(
        "/v1/jobs",
        json={"items": [{"prompt": "p", "has_media": True, "media_urls": ["https://cdn.example.com/a.jpg"]}]},
    )
    assert resp.status_code == 422


def test_media_urls_over_max_count_returns_422(jobs_api_client):
    jobs_api_client.app.state.settings.media_url_max_count = 2
    resp = jobs_api_client.post(
        "/v1/jobs",
        json={
            "items": [
                {
                    "prompt": "p",
                    "media_urls": [
                        "https://cdn.example.com/a.jpg",
                        "https://cdn.example.com/b.jpg",
                        "https://cdn.example.com/c.jpg",
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 422


def test_media_upload_conflicts_and_404s(jobs_api_client):
    resp = jobs_api_client.post(
        "/v1/jobs", json={"items": [{"item_id": "x", "prompt": "p"}]}  # has_media False
    )
    batch_id = resp.json()["batch_id"]

    # Item exists but is not awaiting media -> 409.
    up = jobs_api_client.post(
        f"/v1/jobs/{batch_id}/items/x/media",
        files={"file": ("a.mp4", io.BytesIO(b"v"), "video/mp4")},
    )
    assert up.status_code == 409

    # Unknown item / batch -> 404.
    assert (
        jobs_api_client.post(
            f"/v1/jobs/{batch_id}/items/nope/media",
            files={"file": ("a.mp4", io.BytesIO(b"v"), "video/mp4")},
        ).status_code
        == 404
    )
    assert jobs_api_client.get("/v1/jobs/doesnotexist").status_code == 404
    assert jobs_api_client.get(f"/v1/jobs/{batch_id}/items/nope").status_code == 404


def test_failed_items_carry_error_not_silent_drop(jobs_api_client, monkeypatch):
    async def always_fail(self, ctx):
        raise RuntimeError("500 broken pipe")

    monkeypatch.setattr(GeminiProvider, "generate", always_fail)

    resp = jobs_api_client.post("/v1/jobs", json={"items": [{"item_id": "f1", "prompt": "p"}]})
    batch_id = resp.json()["batch_id"]

    result = _poll_until_completed(jobs_api_client, batch_id, timeout=10.0)

    item = result["items"][0]
    assert item["status"] == "failed"
    assert item["error_code"] == "generate_failed"
    assert "broken pipe" in item["error"]
    assert item["attempts"] >= 1
    assert result["counts"]["failed"] == 1


def test_queue_full_returns_429_with_retry_after(api_client):
    # api_client fixture runs with JOBS_WORKER_CONCURRENCY=0 (frozen) — queued items stay.
    first = api_client.post("/v1/jobs", json={"items": [{"prompt": "p"}]})
    assert first.status_code == 201

    # Shrink the cap under the already-queued volume for the second submit.
    api_client.app.state.settings.jobs_max_queue_length = 1
    second = api_client.post("/v1/jobs", json={"items": [{"prompt": "q"}, {"prompt": "r"}]})

    assert second.status_code == 429
    assert second.json()["retry_after_seconds"] is not None
    assert "Retry-After" in second.headers


def test_submit_validation(jobs_api_client):
    # Empty items.
    assert jobs_api_client.post("/v1/jobs", json={"items": []}).status_code == 422
    # Item with neither prompt nor parts.
    assert jobs_api_client.post("/v1/jobs", json={"items": [{"item_id": "x"}]}).status_code == 422
    # Duplicate item ids.
    assert (
        jobs_api_client.post(
            "/v1/jobs", json={"items": [{"item_id": "d", "prompt": "a"}, {"item_id": "d", "prompt": "b"}]}
        ).status_code
        == 422
    )
    # Unknown provider.
    assert (
        jobs_api_client.post(
            "/v1/jobs", json={"provider": "nope", "items": [{"prompt": "a"}]}
        ).status_code
        == 422
    )


def test_sync_generate_still_works_with_workers_running(jobs_api_client, monkeypatch):
    async def fake_generate(self, ctx):
        return ProviderResult(text="sync ok", total_tokens=1)

    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    resp = jobs_api_client.post("/v1/generate", json={"prompt": "hi"})
    assert resp.status_code == 200
    assert resp.json()["text"] == "sync ok"


# ---------------------------------------------------------------------------
# Cancel (POST /v1/jobs/{batch_id}/cancel)
# ---------------------------------------------------------------------------


def test_cancel_batch_drops_queued_items_and_lets_running_ones_finish(jobs_api_client, monkeypatch):
    """4 worker slots, 6 items, but only 2 fixture API keys: the first 4 items get
    claimed (status -> running) almost immediately, but only 2 actually hold a key
    and are inside a slow generate call when cancel lands; the other 2 are marked
    running yet still waiting their turn on `acquire_key`. Cancelling here must:
      - drop the 2 still-QUEUED items straight out of the queue (never claimed), and
      - stop the 2 RUNNING-but-not-yet-generating items before they ever call
        acquire_key (they never even spend an attempt), and
      - let the 2 that already started generating finish their in-flight call.
    Net: only the pool's real concurrency (2) gets to finish; everything else is
    cancelled before spending any provider/key time — the whole point of this
    endpoint. The batch's overall terminal status reflects the cancel even though
    some items succeeded.
    """

    async def fake_generate(self, ctx):
        await asyncio.sleep(0.3)
        return ProviderResult(text=f"echo:{ctx.prompt_text}", total_tokens=1)

    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    resp = jobs_api_client.post(
        "/v1/jobs",
        json={"items": [{"item_id": f"i{n}", "prompt": f"p{n}"} for n in range(6)]},
    )
    assert resp.status_code == 201
    batch_id = resp.json()["batch_id"]

    # `running` is set the instant a worker claims an item (mark_running), before it
    # even waits on a key — poll for the count instead of guessing a sleep duration,
    # so the cancel always lands after exactly the 4 worker slots have claimed theirs.
    deadline = time.time() + 5.0
    running = 0
    while time.time() < deadline:
        body = jobs_api_client.get(f"/v1/jobs/{batch_id}").json()
        running = body["counts"]["running"]
        if running >= 4:
            break
        time.sleep(0.02)
    assert running == 4, f"expected 4 items running before cancel, got {running}"

    cancel_resp = jobs_api_client.post(f"/v1/jobs/{batch_id}/cancel")
    assert cancel_resp.status_code == 200

    result = _poll_until_terminal(jobs_api_client, batch_id)
    assert result["status"] == "cancelled"
    assert result["counts"]["cancelled"] == 4
    assert result["counts"]["succeeded"] == 2
    assert result["counts"]["failed"] == 0

    by_status = {i["status"] for i in result["items"]}
    assert by_status == {"succeeded", "cancelled"}
    cancelled_items = [i for i in result["items"] if i["status"] == "cancelled"]
    assert all(i["error_code"] == "cancelled" for i in cancelled_items)
    # The 2 that were "running" but hadn't started generating yet never spent an
    # attempt — they were caught before ever calling acquire_key.
    assert all(i["attempts"] == 0 for i in cancelled_items)


def test_cancel_unknown_batch_returns_404(jobs_api_client):
    resp = jobs_api_client.post("/v1/jobs/does-not-exist/cancel")
    assert resp.status_code == 404


def test_cancel_already_completed_batch_is_idempotent(jobs_api_client, monkeypatch):
    async def fake_generate(self, ctx):
        return ProviderResult(text="done", total_tokens=1)

    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    resp = jobs_api_client.post("/v1/jobs", json={"items": [{"item_id": "a", "prompt": "hi"}]})
    batch_id = resp.json()["batch_id"]
    completed = _poll_until_completed(jobs_api_client, batch_id)
    assert completed["counts"]["succeeded"] == 1

    cancel_resp = jobs_api_client.post(f"/v1/jobs/{batch_id}/cancel")
    assert cancel_resp.status_code == 200
    body = cancel_resp.json()
    # A batch that already finished on its own stays "completed" — cancel on a
    # terminal batch is a no-op, not a way to relabel history as "cancelled".
    assert body["status"] == "completed"
    assert body["counts"]["succeeded"] == 1
    assert body["counts"]["cancelled"] == 0
