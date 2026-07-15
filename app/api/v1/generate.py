from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile

from app.config import Settings
from app.deps import get_call_tracker, get_key_pool, get_provider, get_rate_limiter, get_settings, get_usage_logger
from app.errors import AllKeysDeadHTTPError, MediaFetchHTTPError, PoolExhaustedHTTPError
from app.media_fetch import MediaDownloadError, download_media
from app.models.enums import FailureReason
from app.models.requests import GenerateMediaUrlRequest, GenerateRequest
from app.models.responses import GenerateResponse
from app.pool.key_pool import AsyncAPIKeyPool
from app.pool.redis_keys import key_suffix
from app.providers.base import GenerateContext, Provider, UploadedMediaRef
from app.rate_limit.limiter import RateLimiter
from app.tracking.call_tracker import CallTracker
from app.tracking.usage_logger import UsageLogger

router = APIRouter()

async def _raise_pool_error(pool: AsyncAPIKeyPool, request_id: str, provider_name: str) -> None:
    status = await pool.get_pool_status()
    key_statuses = []
    for bucket_name, entries in status["keys"].items():
        for entry in entries:
            key_statuses.append(
                {
                    "suffix": entry["suffix"],
                    "status": bucket_name,
                    "reason": entry.get("last_reason"),
                    "retry_in_seconds": entry.get("retry_in_seconds", 0.0),
                    "failure_streak": entry.get("failure_streak"),
                }
            )

    if status["total_keys"] == 0 or status["permanently_blocked"] == status["total_keys"]:
        raise AllKeysDeadHTTPError(request_id=request_id, key_statuses=key_statuses)

    retry_after = status["next_retry_seconds"] or 30.0
    raise PoolExhaustedHTTPError(request_id=request_id, retry_after_seconds=retry_after, key_statuses=key_statuses)


async def run_generate(
    *,
    request_id: str,
    req: GenerateRequest,
    provider: Provider,
    pool: AsyncAPIKeyPool,
    tracker: CallTracker,
    rate_limiter: RateLimiter,
    usage_logger: UsageLogger,
    settings: Settings,
    media_path: Optional[str] = None,
    media_paths: Optional[list[str]] = None,
    deadline_seconds: Optional[float] = None,
) -> GenerateResponse:
    model = provider.resolve_model(req.model)
    max_retries = max(req.max_retries, pool.size() + 5)
    prompt_parts = req.parts_as_dicts()

    # media_path (single file) and media_paths (zero or more) are two ways of feeding
    # the same normalized `paths` list below — callers only ever pass one or the other.
    paths: list[str] = media_paths if media_paths else ([media_path] if media_path else [])
    requires_upload_by_path = {p: await provider.requires_file_upload(p) for p in paths}
    any_requires_upload = any(requires_upload_by_path.values())
    uploaded_refs: dict[str, UploadedMediaRef] = {}
    uploaded_ref_key: Optional[str] = None

    # Per-generate timeout: honor the caller's value, else fall back to the server
    # default so a slow/hung SDK call raises (and the loop rotates keys) instead of
    # blocking until the client socket dies.
    effective_timeout = (
        req.timeout_seconds
        if (req.timeout_seconds and req.timeout_seconds > 0)
        else settings.default_generate_timeout_seconds
    )

    start_time = time.monotonic()
    # Total wall-clock budget across all internal retries — respond 429 before the
    # caller's HTTP read-timeout fires rather than holding the connection open.
    # Job workers pass a wider deadline_seconds (no HTTP client is waiting there).
    deadline = start_time + (deadline_seconds or settings.request_deadline_seconds)

    try:
        for attempt in range(max_retries):
            leased_key: Optional[str] = None
            attempt_model = model

            if time.monotonic() >= deadline:
                await _raise_pool_error(pool, request_id, provider.name)

            try:
                acquire_wait = max(0.0, min(settings.acquire_key_max_wait_seconds, deadline - time.monotonic()))
                key, key_model = await pool.acquire_key(
                    tracker=tracker,
                    service=provider.name,
                    method="generate",
                    max_wait_seconds=acquire_wait,
                )
                if not key:
                    await _raise_pool_error(pool, request_id, provider.name)
                leased_key = key
                attempt_model = key_model or model

                # Gemini File API refs are scoped to the uploading key's project, so
                # upload and generate MUST use the same key — otherwise generate gets a
                # 403 "permission to access the File". (Re-)upload every file that needs
                # it under the currently-leased key whenever we don't hold refs for this
                # exact key yet.
                if any_requires_upload and uploaded_ref_key != key:
                    if uploaded_refs and uploaded_ref_key:
                        for stale_ref in uploaded_refs.values():
                            await provider.delete_uploaded_media(stale_ref, uploaded_ref_key)
                    uploaded_refs = {}
                    uploaded_ref_key = None

                    upload_failed = False
                    for p in paths:
                        if not requires_upload_by_path[p]:
                            continue
                        try:
                            uploaded_refs[p] = await provider.upload_media(p, key)
                            await tracker.record_call(
                                provider.name, "upload", attempt_model, key_suffix(key), True, "uploaded", total_tokens=0
                            )
                        except Exception as upload_err:
                            message = str(upload_err)
                            await tracker.record_call(
                                provider.name, "upload", attempt_model, key_suffix(key), False, message, total_tokens=0
                            )
                            classification = provider.classify_error(message)
                            await pool.report_failure(key, attempt_model, classification, tracker=tracker)
                            usage_logger.log_call(
                                request_id=request_id,
                                service=provider.name,
                                method="upload",
                                model=attempt_model,
                                quota_model=tracker.resolve_model(attempt_model),
                                api_key_suffix=key_suffix(key),
                                success=False,
                                input_tokens=None,
                                output_tokens=None,
                                total_tokens=None,
                                error=message,
                            )
                            upload_failed = True
                            break

                    if upload_failed:
                        # Drop whatever succeeded this attempt so the next attempt (same
                        # or different key) starts clean instead of mixing key scopes.
                        for ref in uploaded_refs.values():
                            await provider.delete_uploaded_media(ref, key)
                        uploaded_refs = {}
                        continue

                    uploaded_ref_key = key

                if rate_limiter:
                    await rate_limiter.wait_if_needed(key)

                ctx = GenerateContext(
                    prompt_text=req.prompt,
                    prompt_parts=prompt_parts,
                    media_paths=paths,
                    model=attempt_model,
                    api_key=key,
                    timeout_seconds=effective_timeout,
                    verbose=req.verbose,
                    request_id=request_id,
                    extra={"uploaded_refs": uploaded_refs} if uploaded_refs else {},
                )

                result = await provider.generate(ctx)

                await tracker.record_call(
                    provider.name,
                    "generate",
                    attempt_model,
                    key_suffix(key),
                    True,
                    result.text[:100],
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    total_tokens=result.total_tokens,
                )
                await pool.record_success(key, attempt_model)
                usage_logger.log_call(
                    request_id=request_id,
                    service=provider.name,
                    method="generate",
                    model=attempt_model,
                    quota_model=tracker.resolve_model(attempt_model),
                    api_key_suffix=key_suffix(key),
                    success=True,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    total_tokens=result.total_tokens,
                    response_preview=result.text[:200],
                    latency_ms=(time.monotonic() - start_time) * 1000,
                )

                return GenerateResponse(
                    request_id=request_id,
                    provider=provider.name,
                    model=attempt_model,
                    text=result.text,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    total_tokens=result.total_tokens,
                    api_key_suffix=key_suffix(key),
                    attempts=attempt + 1,
                    latency_ms=(time.monotonic() - start_time) * 1000,
                )

            except (AllKeysDeadHTTPError, PoolExhaustedHTTPError):
                raise
            except Exception as e:
                message = str(e)
                classification = provider.classify_error(message)
                if leased_key:
                    await tracker.record_call(
                        provider.name, "generate", attempt_model, key_suffix(leased_key), False, message, total_tokens=0
                    )
                    await pool.report_failure(leased_key, attempt_model, classification, tracker=tracker)
                    usage_logger.log_call(
                        request_id=request_id,
                        service=provider.name,
                        method="generate",
                        model=attempt_model,
                        quota_model=tracker.resolve_model(attempt_model),
                        api_key_suffix=key_suffix(leased_key),
                        success=False,
                        input_tokens=None,
                        output_tokens=None,
                        total_tokens=None,
                        error=message,
                    )

                if (
                    classification.reason in (FailureReason.STALE_MEDIA, FailureReason.AUTH_DEAD)
                    and uploaded_refs
                ):
                    # Stale/expired file ref(s) or a dead key — drop them so the next
                    # attempt re-uploads under a fresh key. STALE_MEDIA does not cool the
                    # key (see report_failure), so a cross-key 403 no longer kills the pool.
                    if uploaded_ref_key:
                        for ref in uploaded_refs.values():
                            await provider.delete_uploaded_media(ref, uploaded_ref_key)
                    uploaded_refs = {}
                    uploaded_ref_key = None
                elif classification.reason in (FailureReason.RATE_LIMIT, FailureReason.HIGH_DEMAND):
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                    if any_requires_upload and uploaded_refs:
                        if uploaded_ref_key:
                            for ref in uploaded_refs.values():
                                await provider.delete_uploaded_media(ref, uploaded_ref_key)
                        uploaded_refs = {}
                        uploaded_ref_key = None

                if attempt >= max_retries - 1:
                    raise
                continue
            finally:
                if leased_key:
                    await pool.release_key(leased_key)

        await _raise_pool_error(pool, request_id, provider.name)
        raise AllKeysDeadHTTPError(request_id=request_id)
    finally:
        if uploaded_refs and uploaded_ref_key:
            for ref in uploaded_refs.values():
                await provider.delete_uploaded_media(ref, uploaded_ref_key)


@router.post("/generate", response_model=GenerateResponse)
async def generate(request: Request, req: GenerateRequest) -> GenerateResponse:
    provider = get_provider(request, req.provider)
    pool = get_key_pool(request, req.provider)
    tracker = get_call_tracker(request, req.provider)
    rate_limiter = get_rate_limiter(request, req.provider)
    usage_logger = get_usage_logger(request)
    settings: Settings = get_settings(request)

    request_id = uuid.uuid4().hex
    return await run_generate(
        request_id=request_id,
        req=req,
        provider=provider,
        pool=pool,
        tracker=tracker,
        rate_limiter=rate_limiter,
        usage_logger=usage_logger,
        settings=settings,
    )


@router.post("/generate/media", response_model=GenerateResponse)
async def generate_media(
    request: Request,
    file: UploadFile,
    payload: str = Form(...),
) -> GenerateResponse:
    req = GenerateRequest.model_validate(json.loads(payload))

    provider = get_provider(request, req.provider)
    pool = get_key_pool(request, req.provider)
    tracker = get_call_tracker(request, req.provider)
    rate_limiter = get_rate_limiter(request, req.provider)
    usage_logger = get_usage_logger(request)
    settings: Settings = get_settings(request)

    request_id = uuid.uuid4().hex
    upload_dir = Path(settings.uploads_dir) / request_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    media_path = upload_dir / (file.filename or "upload.bin")

    try:
        with open(media_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        return await run_generate(
            request_id=request_id,
            req=req,
            provider=provider,
            pool=pool,
            tracker=tracker,
            rate_limiter=rate_limiter,
            usage_logger=usage_logger,
            settings=settings,
            media_path=str(media_path),
        )
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)


@router.post("/generate/media/url", response_model=GenerateResponse)
async def generate_media_url(request: Request, req: GenerateMediaUrlRequest) -> GenerateResponse:
    """Same as /generate/media, but the caller sends one or more CDN urls instead of
    the raw bytes — the gateway downloads them server-side. Avoids clients pushing
    large media through the request body just to hand it back to us."""
    provider = get_provider(request, req.provider)
    pool = get_key_pool(request, req.provider)
    tracker = get_call_tracker(request, req.provider)
    rate_limiter = get_rate_limiter(request, req.provider)
    usage_logger = get_usage_logger(request)
    settings: Settings = get_settings(request)

    if len(req.media_urls) > settings.media_url_max_count:
        raise HTTPException(
            status_code=422,
            detail=f"media_urls exceeds media_url_max_count ({settings.media_url_max_count}).",
        )

    request_id = uuid.uuid4().hex
    upload_dir = Path(settings.uploads_dir) / request_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    async def _download_one(index: int, url: str) -> Path:
        # One subdir per url so same-named files from different CDNs (e.g. two
        # "image.jpg") don't collide.
        dest_dir = upload_dir / str(index)
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            return await download_media(
                url,
                dest_dir,
                max_bytes=settings.media_url_max_bytes,
                timeout_seconds=settings.media_url_download_timeout_seconds,
            )
        except MediaDownloadError as e:
            raise MediaFetchHTTPError(request_id=request_id, detail=f"{url}: {e}") from e

    try:
        media_paths = await asyncio.gather(
            *(_download_one(i, url) for i, url in enumerate(req.media_urls))
        )

        return await run_generate(
            request_id=request_id,
            req=req,
            provider=provider,
            pool=pool,
            tracker=tracker,
            rate_limiter=rate_limiter,
            usage_logger=usage_logger,
            settings=settings,
            media_paths=[str(p) for p in media_paths],
        )
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)
