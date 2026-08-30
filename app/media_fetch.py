from __future__ import annotations

import asyncio
import mimetypes
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx


class MediaDownloadError(Exception):
    """Raised when a client-supplied media_url can't be fetched. Message is safe to
    surface to the caller as-is (no internal details leaked)."""


def _filename_from_url(url: str, content_type: str | None) -> str:
    name = Path(urlparse(url).path).name
    if name and "." in name:
        return name
    ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ".bin"
    return f"download{ext}"


async def download_media(url: str, dest_dir: Path, *, max_bytes: int, timeout_seconds: float) -> Path:
    """Stream a CDN url to dest_dir, enforcing max_bytes even if the server lies about
    (or omits) Content-Length. Raises MediaDownloadError on any failure."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise MediaDownloadError(f"Unsupported URL scheme: {parsed.scheme or '(none)'}")

    # httpx's `timeout` float bounds each individual connect/read/write operation, not
    # the transfer as a whole — a connection that trickles data in just under that
    # limit on every chunk read would otherwise never time out even though the overall
    # download can run indefinitely. Enforce a real wall-clock cap on top of that.
    deadline = time.monotonic() + timeout_seconds
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise MediaDownloadError(f"media_url returned HTTP {resp.status_code}")

                content_length = resp.headers.get("content-length")
                if content_length is not None and int(content_length) > max_bytes:
                    raise MediaDownloadError(
                        f"media_url content-length ({content_length}) exceeds max_bytes ({max_bytes})"
                    )

                dest_path = dest_dir / _filename_from_url(url, resp.headers.get("content-type"))
                written = 0
                with open(dest_path, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        if time.monotonic() > deadline:
                            raise MediaDownloadError(
                                f"media_url download exceeded {timeout_seconds}s overall"
                            )
                        written += len(chunk)
                        if written > max_bytes:
                            raise MediaDownloadError(
                                f"media_url body exceeds max_bytes ({max_bytes}) while streaming"
                            )
                        await asyncio.to_thread(f.write, chunk)

                if written == 0:
                    raise MediaDownloadError("media_url returned an empty body")

                return dest_path
    except httpx.TimeoutException as e:
        raise MediaDownloadError(f"Timed out fetching media_url after {timeout_seconds}s") from e
    except httpx.HTTPError as e:
        raise MediaDownloadError(f"Failed to fetch media_url: {e}") from e
