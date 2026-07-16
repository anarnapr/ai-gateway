from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.deps import get_call_tracker, get_provider
from app.redis_client import get_redis
from app.tracking.stats import get_stats_summary

router = APIRouter()


@router.get("/stats")
async def stats(
    request: Request,
    provider: str = "gemini",
    days: int = Query(default=1, ge=1, le=90),
) -> dict:
    """Aggregate call/failure/latency/job stats for offline analysis — how many calls,
    how many failed and why, how many 429/503/422 responses went back to callers, and
    per-model average latency, summed over the trailing `days` UTC days (default:
    today only, max 90 — matches STATS_TTL_SECONDS retention).
    """
    prov = get_provider(request, provider)
    tracker = get_call_tracker(request, provider)
    rk = tracker.rk

    return await get_stats_summary(get_redis(), rk, provider, prov.model_priority(), days=days)
