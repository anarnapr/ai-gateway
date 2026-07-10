import io
import json
from pathlib import Path

from app.providers.base import ProviderResult, UploadedMediaRef
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


def test_generate_media_file_upload_pins_same_key_for_upload_and_generate(api_client, monkeypatch):
    """File API refs are key/project-scoped: uploading with key A then generating with
    key B yields a 403 "permission to access the File" that used to be misclassified as
    a dead key and cascade the whole pool. Upload and generate must share one key.
    """
    used_keys = {}

    async def fake_requires_upload(self, media_path):
        return True  # force the File API upload path

    async def fake_upload_media(self, media_path, api_key):
        used_keys["upload"] = api_key
        return UploadedMediaRef(name="files/abc123", handle=object())

    async def fake_generate(self, ctx):
        used_keys["generate"] = ctx.api_key
        assert ctx.extra.get("uploaded_ref") is not None
        return ProviderResult(text="described video", input_tokens=10, output_tokens=4, total_tokens=14)

    async def fake_delete(self, ref, api_key):
        return None

    monkeypatch.setattr(GeminiProvider, "requires_file_upload", fake_requires_upload)
    monkeypatch.setattr(GeminiProvider, "upload_media", fake_upload_media)
    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)
    monkeypatch.setattr(GeminiProvider, "delete_uploaded_media", fake_delete)

    fake_video = io.BytesIO(b"\x00\x00\x00\x18ftypmp42fake-video-bytes")
    resp = api_client.post(
        "/v1/generate/media",
        files={"file": ("clip.mp4", fake_video, "video/mp4")},
        data={"payload": json.dumps({"prompt": "describe this"})},
    )

    assert resp.status_code == 200
    assert resp.json()["text"] == "described video"
    assert used_keys["upload"] == used_keys["generate"]
