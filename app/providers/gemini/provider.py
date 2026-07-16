from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import re
import time
from typing import Optional, Union

from google import genai
from google.genai import types

from app.providers.base import (
    FailureClassification,
    GenerateContext,
    Provider,
    ProviderResult,
    UploadedMediaRef,
)
from app.providers.gemini.errors import classify_gemini_error

_UPLOAD_POLL_TIMEOUT_SECONDS = 600
_UPLOAD_POLL_INTERVAL_SECONDS = 5
# Bounds the whole upload_media() call (initial transfer + the ACTIVE-state poll
# above) — without this, a stalled client.files.upload() HTTP call can hang forever,
# since (unlike generate()) nothing here previously wrapped it in asyncio.wait_for.
# +180s buffer over the poll budget covers the raw upload transfer itself.
_UPLOAD_TOTAL_TIMEOUT_SECONDS = _UPLOAD_POLL_TIMEOUT_SECONDS + 180


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, model_priority: list[str], model_aliases: dict[str, str], quota_table: dict[str, dict]):
        self._model_priority = model_priority
        self._model_aliases = model_aliases
        self._quota_table = quota_table

    def model_priority(self) -> list[str]:
        return self._model_priority

    def model_aliases(self) -> dict[str, str]:
        return self._model_aliases

    def quota_table(self) -> dict[str, dict]:
        return self._quota_table

    def classify_error(self, error: str) -> FailureClassification:
        return classify_gemini_error(error)

    async def requires_file_upload(self, media_path: str) -> bool:
        mime_type = mimetypes.guess_type(media_path)[0] or ""
        return "video" in mime_type or os.path.getsize(media_path) > 10 * 1024 * 1024

    # ---------- media upload (File API) ----------

    def _upload_sync(self, media_path: str, api_key: str) -> UploadedMediaRef:
        client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
        base_filename = os.path.basename(media_path)
        sanitized_display_name = re.sub(r"\s*\(.*?\)|\s*\[.*?\]", "", base_filename).strip()

        uploaded = client.files.upload(file=media_path, config={"display_name": sanitized_display_name})

        start_time = time.time()
        while time.time() - start_time < _UPLOAD_POLL_TIMEOUT_SECONDS:
            file_status = client.files.get(name=uploaded.name)
            if file_status.state == "ACTIVE":
                return UploadedMediaRef(name=file_status.name, handle=file_status)
            if file_status.state == "FAILED":
                raise RuntimeError(f"Gemini file upload failed for {uploaded.display_name}")
            time.sleep(_UPLOAD_POLL_INTERVAL_SECONDS)

        raise TimeoutError(f"Gemini file upload timed out waiting for ACTIVE state: {media_path}")

    async def upload_media(self, media_path: str, api_key: str) -> Optional[UploadedMediaRef]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._upload_sync, media_path, api_key),
                timeout=_UPLOAD_TOTAL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Gemini file upload timed out after {_UPLOAD_TOTAL_TIMEOUT_SECONDS:.0f}s: {media_path}"
            ) from exc

    async def delete_uploaded_media(self, ref: UploadedMediaRef, api_key: str) -> None:
        def _delete():
            try:
                client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
                client.files.delete(name=ref.name)
            except Exception:
                pass

        await asyncio.to_thread(_delete)

    # ---------- content parts ----------

    def _create_inline_media_part(self, media_path: str):
        mime_type = mimetypes.guess_type(media_path)[0] or "application/octet-stream"
        if not mime_type.startswith("image/") and not mime_type.startswith("audio/"):
            return None
        with open(media_path, "rb") as f:
            data = f.read()
        return types.Part.from_bytes(data=data, mime_type=mime_type)

    def _convert_parts(self, parts: list[Union[str, dict]]):
        new_parts = []
        for p in parts:
            if isinstance(p, dict) and "inline_data" in p:
                try:
                    data = base64.b64decode(p["inline_data"]["data"])
                    new_parts.append(types.Part.from_bytes(data=data, mime_type=p["inline_data"]["mime_type"]))
                except Exception:
                    new_parts.append(str(p))
            else:
                new_parts.append(p)
        return new_parts

    # ---------- generate ----------

    def _generate_sync(self, ctx: GenerateContext, content_to_send: list):
        client = genai.Client(api_key=ctx.api_key, http_options={"api_version": "v1beta"})
        return client.models.generate_content(model=ctx.model, contents=content_to_send)

    async def generate(self, ctx: GenerateContext) -> ProviderResult:
        content_to_send: list = []
        if ctx.prompt_parts:
            content_to_send.extend(self._convert_parts(ctx.prompt_parts))

        uploaded_refs: dict[str, UploadedMediaRef] = ctx.extra.get("uploaded_refs") or {}
        for media_path in ctx.media_paths:
            ref = uploaded_refs.get(media_path)
            if ref is not None:
                content_to_send.append(ref.handle)
            else:
                inline_part = self._create_inline_media_part(media_path)
                if inline_part:
                    content_to_send.append(inline_part)

        if ctx.prompt_text:
            content_to_send.append(ctx.prompt_text)

        try:
            if ctx.timeout_seconds and ctx.timeout_seconds > 0:
                response = await asyncio.wait_for(
                    asyncio.to_thread(self._generate_sync, ctx, content_to_send),
                    timeout=ctx.timeout_seconds,
                )
            else:
                response = await asyncio.to_thread(self._generate_sync, ctx, content_to_send)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"Gemini request timed out after {ctx.timeout_seconds:.1f}s.") from exc

        input_tokens = None
        output_tokens = None
        total_tokens = None
        if hasattr(response, "usage_metadata"):
            usage = response.usage_metadata
            total_tokens = getattr(usage, "total_token_count", None)
            input_tokens = getattr(usage, "prompt_token_count", None)
            output_tokens = getattr(usage, "candidates_token_count", None)

        result_text = response.text.strip().replace("\n", " ")
        return ProviderResult(
            text=result_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
