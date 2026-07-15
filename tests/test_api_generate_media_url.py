from pathlib import Path

import app.api.v1.generate as generate_module
from app.media_fetch import MediaDownloadError
from app.providers.base import ProviderResult
from app.providers.gemini.provider import GeminiProvider


def test_generate_media_url_success_and_cleans_up(api_client, monkeypatch, tmp_path):
    async def fake_download_media(url, dest_dir, *, max_bytes, timeout_seconds):
        assert url == "https://cdn.example.com/photo.jpg"
        path = Path(dest_dir) / "photo.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
        return path

    async def fake_requires_upload(self, media_path):
        return False

    async def fake_generate(self, ctx):
        assert len(ctx.media_paths) == 1
        return ProviderResult(text="described image", input_tokens=10, output_tokens=4, total_tokens=14)

    monkeypatch.setattr(generate_module, "download_media", fake_download_media)
    monkeypatch.setattr(GeminiProvider, "requires_file_upload", fake_requires_upload)
    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    from app.config import get_settings

    uploads_dir = Path(get_settings().uploads_dir)

    resp = api_client.post(
        "/v1/generate/media/url",
        json={"prompt": "describe this", "media_urls": ["https://cdn.example.com/photo.jpg"]},
    )

    assert resp.status_code == 200
    assert resp.json()["text"] == "described image"
    assert list(uploads_dir.iterdir()) == []


def test_generate_media_url_multiple_urls_downloaded_and_passed_to_provider(api_client, monkeypatch):
    urls = [
        "https://cdn.example.com/one.jpg",
        "https://cdn.example.com/two.jpg",
        "https://other-cdn.example.com/one.jpg",  # same basename as the first, different host
    ]

    async def fake_download_media(url, dest_dir, *, max_bytes, timeout_seconds):
        path = Path(dest_dir) / "one.jpg" if url.endswith("one.jpg") else Path(dest_dir) / "two.jpg"
        path.write_bytes(b"fake-bytes-for-" + url.encode())
        return path

    async def fake_requires_upload(self, media_path):
        return False

    seen_paths = []

    async def fake_generate(self, ctx):
        seen_paths.extend(ctx.media_paths)
        return ProviderResult(text="described 3 images", input_tokens=10, output_tokens=4, total_tokens=14)

    monkeypatch.setattr(generate_module, "download_media", fake_download_media)
    monkeypatch.setattr(GeminiProvider, "requires_file_upload", fake_requires_upload)
    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    resp = api_client.post(
        "/v1/generate/media/url",
        json={"prompt": "describe these", "media_urls": urls},
    )

    assert resp.status_code == 200
    assert resp.json()["text"] == "described 3 images"
    # 3 distinct files written (no collision despite two sharing the "one.jpg" basename)
    assert len(seen_paths) == 3
    assert len(set(seen_paths)) == 3


def test_generate_media_url_one_bad_url_fails_whole_request(api_client, monkeypatch):
    async def fake_download_media(url, dest_dir, *, max_bytes, timeout_seconds):
        if "missing" in url:
            raise MediaDownloadError("media_url returned HTTP 404")
        path = Path(dest_dir) / "ok.jpg"
        path.write_bytes(b"fake-bytes")
        return path

    monkeypatch.setattr(generate_module, "download_media", fake_download_media)

    resp = api_client.post(
        "/v1/generate/media/url",
        json={
            "prompt": "describe these",
            "media_urls": ["https://cdn.example.com/ok.jpg", "https://cdn.example.com/missing.jpg"],
        },
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "media_fetch_failed"
    assert "404" in body["detail"]


def test_generate_media_url_download_failure_returns_422(api_client, monkeypatch):
    async def fake_download_media(url, dest_dir, *, max_bytes, timeout_seconds):
        raise MediaDownloadError("media_url returned HTTP 404")

    monkeypatch.setattr(generate_module, "download_media", fake_download_media)

    resp = api_client.post(
        "/v1/generate/media/url",
        json={"prompt": "describe this", "media_urls": ["https://cdn.example.com/missing.jpg"]},
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "media_fetch_failed"
    assert "404" in body["detail"]


def test_generate_media_url_requires_media_urls_field(api_client):
    resp = api_client.post("/v1/generate/media/url", json={"prompt": "describe this"})
    assert resp.status_code == 422


def test_generate_media_url_rejects_empty_media_urls_list(api_client):
    resp = api_client.post(
        "/v1/generate/media/url", json={"prompt": "describe this", "media_urls": []}
    )
    assert resp.status_code == 422


def test_generate_media_url_rejects_too_many_urls(api_client):
    api_client.app.state.settings.media_url_max_count = 2

    resp = api_client.post(
        "/v1/generate/media/url",
        json={
            "prompt": "describe these",
            "media_urls": [
                "https://cdn.example.com/a.jpg",
                "https://cdn.example.com/b.jpg",
                "https://cdn.example.com/c.jpg",
            ],
        },
    )

    assert resp.status_code == 422
