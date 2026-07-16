from __future__ import annotations

from fastapi import APIRouter, Request

from app.deps import get_call_tracker, get_job_store, get_key_pool, get_settings

router = APIRouter()


@router.get("/capacity")
async def capacity(request: Request, provider: str = "gemini") -> dict:
    """Single-call readiness signal for a caller deciding whether to submit more
    work — combines key-pool headroom, global in-flight usage, and jobs-queue
    headroom into one accepting_more_work verdict instead of making the caller
    reconcile /pool/status, /keys, and queue depth themselves.
    """
    settings = get_settings(request)
    pool = get_key_pool(request, provider)
    tracker = get_call_tracker(request, provider)
    store = get_job_store(request)

    pool_status = await pool.get_pool_status(tracker=tracker, service=provider)
    in_flight_current = await pool.current_in_flight()
    queue_length = await store.queue_length()

    reasons = []
    if pool_status["available"] == 0:
        reasons.append("no_keys_available")
    if in_flight_current >= settings.max_in_flight:
        reasons.append("in_flight_at_limit")
    if queue_length >= settings.jobs_max_queue_length:
        reasons.append("jobs_queue_full")

    return {
        "provider": provider,
        "model": pool_status["model"],
        "keys": {
            "total": pool_status["total_keys"],
            "available": pool_status["available"],
            "in_use": pool_status["in_use"],
            "cooling": pool_status["short_cooldown"],
            "dead": pool_status["permanently_blocked"],
        },
        "in_flight": {
            "current": in_flight_current,
            "limit": settings.max_in_flight,
        },
        "jobs_queue": {
            "queued": queue_length,
            "max": settings.jobs_max_queue_length,
            "remaining": max(settings.jobs_max_queue_length - queue_length, 0),
        },
        "accepting_more_work": not reasons,
        "reasons": reasons,
    }
