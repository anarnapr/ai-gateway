from __future__ import annotations

import time

from fastapi import APIRouter, Request

from app.deps import get_call_tracker, get_key_pool
from app.models.responses import KeyStatusEntry
from app.pool.redis_keys import key_suffix

router = APIRouter()


@router.get("/keys", response_model=list[KeyStatusEntry])
async def list_keys(request: Request, provider: str = "gemini"):
    """One row per configured key with its current status and reason — satisfies
    'return which API key is dead (and why)'.
    """
    pool = get_key_pool(request, provider)
    tracker = get_call_tracker(request, provider)

    model = await pool.get_available_model() or (pool.model_priority[0] if pool.model_priority else "")
    now = time.time()

    entries = []
    for api_key in pool.api_keys:
        status, retry_in = await pool.classify_key_status(api_key, model, now, tracker, service=provider)
        meta = await pool.get_effective_failure_meta(api_key, model)
        entries.append(
            KeyStatusEntry(
                suffix=key_suffix(api_key),
                status=status,
                reason=meta.get("reason"),
                retry_in_seconds=round(max(retry_in, 0.0), 1),
                failure_streak=int(meta["streak"]) if meta.get("streak") else None,
            )
        )
    return entries
