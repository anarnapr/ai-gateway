import httpx
import pytest

from app.media_fetch import MediaDownloadError, download_media


_RealAsyncClient = httpx.AsyncClient


def _client_factory(handler):
    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*args, **kwargs)

    return _factory


@pytest.mark.asyncio
async def test_download_media_success(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8\xff\xe0fakejpeg")

    monkeypatch.setattr("app.media_fetch.httpx.AsyncClient", _client_factory(handler))

    path = await download_media(
        "https://cdn.example.com/photo.jpg", tmp_path, max_bytes=1024, timeout_seconds=5.0
    )
    assert path.exists()
    assert path.read_bytes() == b"\xff\xd8\xff\xe0fakejpeg"
    assert path.name == "photo.jpg"


@pytest.mark.asyncio
async def test_download_media_rejects_bad_scheme(tmp_path):
    with pytest.raises(MediaDownloadError, match="scheme"):
        await download_media("ftp://cdn.example.com/photo.jpg", tmp_path, max_bytes=1024, timeout_seconds=5.0)


@pytest.mark.asyncio
async def test_download_media_rejects_oversized_content_length(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200, headers={"content-length": "9999"}, content=b"x" * 10)

    monkeypatch.setattr("app.media_fetch.httpx.AsyncClient", _client_factory(handler))

    with pytest.raises(MediaDownloadError, match="max_bytes"):
        await download_media("https://cdn.example.com/big.mp4", tmp_path, max_bytes=1024, timeout_seconds=5.0)


@pytest.mark.asyncio
async def test_download_media_rejects_body_exceeding_cap_without_content_length(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"x" * 2048)  # no content-length header

    monkeypatch.setattr("app.media_fetch.httpx.AsyncClient", _client_factory(handler))

    with pytest.raises(MediaDownloadError, match="max_bytes"):
        await download_media("https://cdn.example.com/big.mp4", tmp_path, max_bytes=1024, timeout_seconds=5.0)


@pytest.mark.asyncio
async def test_download_media_rejects_non_200(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(404, content=b"not found")

    monkeypatch.setattr("app.media_fetch.httpx.AsyncClient", _client_factory(handler))

    with pytest.raises(MediaDownloadError, match="404"):
        await download_media("https://cdn.example.com/missing.jpg", tmp_path, max_bytes=1024, timeout_seconds=5.0)
