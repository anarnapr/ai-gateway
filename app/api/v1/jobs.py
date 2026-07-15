from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile

from app.deps import get_job_store, get_provider_registry, get_settings
from app.errors import JobsQueueFullHTTPError
from app.models.jobs import (
    BatchStatusResponse,
    BatchSummary,
    ItemStatus,
    JobItemBrief,
    JobItemResult,
    JobSubmitRequest,
    JobSubmitResponse,
)
from app.models.requests import GenerateRequest

router = APIRouter()

_QUEUE_FULL_RETRY_AFTER_SECONDS = 30.0


@router.post("/jobs", response_model=JobSubmitResponse, status_code=201)
async def submit_batch(request: Request, req: JobSubmitRequest) -> JobSubmitResponse:
    settings = get_settings(request)
    store = get_job_store(request)

    if len(req.items) > settings.jobs_max_items_per_batch:
        raise HTTPException(
            status_code=422,
            detail=f"Batch exceeds jobs_max_items_per_batch ({settings.jobs_max_items_per_batch}).",
        )
    if get_provider_registry(request).get(req.provider) is None:
        raise HTTPException(status_code=422, detail=f"Unknown provider: {req.provider}")
    for spec in req.items:
        if spec.media_urls and len(spec.media_urls) > settings.media_url_max_count:
            raise HTTPException(
                status_code=422,
                detail=f"Item '{spec.item_id}' media_urls exceeds media_url_max_count ({settings.media_url_max_count}).",
            )

    enqueueable = sum(1 for it in req.items if not it.has_media)
    if await store.queue_length() + enqueueable > settings.jobs_max_queue_length:
        raise JobsQueueFullHTTPError(
            request_id=uuid.uuid4().hex, retry_after_seconds=_QUEUE_FULL_RETRY_AFTER_SECONDS
        )

    items = []
    for spec in req.items:
        # Persist each item as a complete GenerateRequest (batch defaults merged in)
        # so the worker just parses it and calls run_generate — no re-derivation.
        gen_req = GenerateRequest(
            provider=req.provider,
            prompt=spec.prompt,
            parts=spec.parts,
            model=spec.model or req.model,
            timeout_seconds=spec.timeout_seconds,
        )
        items.append(
            {
                "item_id": spec.item_id or uuid.uuid4().hex,
                "request_json": gen_req.model_dump_json(exclude_none=True),
                "metadata": spec.metadata,
                "has_media": spec.has_media,
                "media_urls": spec.media_urls,
            }
        )

    batch_id, statuses = await store.create_batch(req.provider, items)
    return JobSubmitResponse(
        batch_id=batch_id,
        total=len(items),
        items=[JobItemBrief(item_id=i, status=ItemStatus(s)) for i, s in statuses],
    )


@router.post("/jobs/{batch_id}/items/{item_id}/media")
async def upload_item_media(request: Request, batch_id: str, item_id: str, file: UploadFile) -> dict:
    settings = get_settings(request)
    store = get_job_store(request)

    item = await store.get_item_raw(batch_id, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Unknown batch or item.")
    if item.get("status") != ItemStatus.AWAITING_MEDIA.value:
        raise HTTPException(
            status_code=409, detail=f"Item is '{item.get('status')}', not awaiting_media."
        )

    # Per-item dir; the worker deletes it after terminal success/failure.
    upload_dir = Path(settings.uploads_dir) / "jobs" / batch_id / item_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    media_path = upload_dir / (file.filename or "upload.bin")
    with open(media_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    await store.attach_media_and_enqueue(batch_id, item_id, str(media_path))
    return {"item_id": item_id, "status": ItemStatus.QUEUED.value}


@router.get("/jobs", response_model=list[BatchSummary])
async def list_batches(request: Request) -> list[BatchSummary]:
    summaries = await get_job_store(request).list_batches()
    return [BatchSummary(**s) for s in summaries]


@router.get("/jobs/{batch_id}", response_model=BatchStatusResponse)
async def get_batch(request: Request, batch_id: str) -> BatchStatusResponse:
    status = await get_job_store(request).get_batch_status(batch_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Unknown or expired batch.")
    return BatchStatusResponse(**status)


@router.get("/jobs/{batch_id}/items/{item_id}", response_model=JobItemResult)
async def get_item(request: Request, batch_id: str, item_id: str) -> JobItemResult:
    item = await get_job_store(request).get_item(batch_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown or expired batch/item.")
    return JobItemResult(**item)
