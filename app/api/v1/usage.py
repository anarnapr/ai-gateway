from __future__ import annotations

from fastapi import APIRouter, Request

from app.deps import get_call_tracker, get_key_pool
from app.pool.redis_keys import key_suffix

router = APIRouter()


@router.get("/usage/summary")
async def usage_summary(request: Request, provider: str = "gemini"):
    pool = get_key_pool(request, provider)
    tracker = get_call_tracker(request, provider)
    suffixes = [key_suffix(k) for k in pool.api_keys]
    return await tracker.get_quota_summary(suffixes)
