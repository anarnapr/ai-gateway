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
