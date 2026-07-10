import io
import json
from pathlib import Path

from app.providers.base import ProviderResult
from app.providers.gemini.provider import GeminiProvider


def test_generate_media_success_and_cleans_up_upload(api_client, monkeypatch, tmp_path):
    async def fake_requires_upload(self, media_path):
        return False  # small image -> inline part path, no File API upload needed

    async def fake_generate(self, ctx):
        assert ctx.media_path is not None
        return ProviderResult(text="described image", input_tokens=10, output_tokens=4, total_tokens=14)

    monkeypatch.setattr(GeminiProvider, "requires_file_upload", fake_requires_upload)
    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    from app.config import get_settings

    settings = get_settings()
    uploads_dir = Path(settings.uploads_dir)

    fake_image = io.BytesIO(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
    resp = api_client.post(
        "/v1/generate/media",
        files={"file": ("photo.jpg", fake_image, "image/jpeg")},
        data={"payload": json.dumps({"prompt": "describe this"})},
    )

    assert resp.status_code == 200
    assert resp.json()["text"] == "described image"

    # Cleanup verified: no leftover per-request upload directories.
    assert list(uploads_dir.iterdir()) == []
