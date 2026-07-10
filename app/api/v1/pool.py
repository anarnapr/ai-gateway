from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request

from app.deps import get_call_tracker, get_key_pool, get_provider
from app.models.responses import PoolStatusResponse

router = APIRouter()


@router.get("/pool/status", response_model=PoolStatusResponse)
async def pool_status(request: Request, provider: str = "gemini", model: Optional[str] = Query(default=None)):
    pool = get_key_pool(request, provider)
    tracker = get_call_tracker(request, provider)
    status = await pool.get_pool_status(model=model, tracker=tracker, service=provider)
    return PoolStatusResponse(**status)


@router.get("/pool/status/all")
async def pool_status_all(request: Request, provider: str = "gemini"):
    prov = get_provider(request, provider)
    pool = get_key_pool(request, provider)
    tracker = get_call_tracker(request, provider)

    results = {}
    for model in prov.model_priority():
        results[model] = await pool.get_pool_status(model=model, tracker=tracker, service=provider)
    return results
