from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import redis.asyncio as redis

from app.pool.redis_keys import RedisKeys

# Long TTL relative to CallTracker's own quota-window keys (which only need to live a
# day or two) — these are for historical/offline analysis, not rate-limit enforcement.
STATS_TTL_SECONDS = 90 * 24 * 3600


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _day(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y%m%d")


async def record_call_outcome(r: redis.Redis, rk: RedisKeys, service: str, success: bool) -> None:
    key = rk.stats_calls(service, _today())
    pipe = r.pipeline(transaction=True)
    pipe.hincrby(key, "total", 1)
    pipe.hincrby(key, "success" if success else "failed", 1)
    pipe.expire(key, STATS_TTL_SECONDS)
    await pipe.execute()


async def record_failure_reason(r: redis.Redis, rk: RedisKeys, service: str, reason: str) -> None:
    key = rk.stats_failures_by_reason(service, _today())
    pipe = r.pipeline(transaction=True)
    pipe.hincrby(key, reason, 1)
    pipe.expire(key, STATS_TTL_SECONDS)
    await pipe.execute()


async def record_http_response(r: redis.Redis, rk: RedisKeys, error_type: str) -> None:
    key = rk.stats_http_responses(_today())
    pipe = r.pipeline(transaction=True)
    pipe.hincrby(key, error_type, 1)
    pipe.expire(key, STATS_TTL_SECONDS)
    await pipe.execute()


async def record_latency(r: redis.Redis, rk: RedisKeys, service: str, model: str, latency_ms: float) -> None:
    key = rk.stats_latency(service, model, _today())
    pipe = r.pipeline(transaction=True)
    pipe.hincrbyfloat(key, "sum_ms", latency_ms)
    pipe.hincrby(key, "count", 1)
    pipe.expire(key, STATS_TTL_SECONDS)
    await pipe.execute()


async def record_job_item_outcome(
    r: redis.Redis, rk: RedisKeys, success: bool, error_code: Optional[str]
) -> None:
    items_key = rk.stats_jobs_items(_today())
    pipe = r.pipeline(transaction=True)
    pipe.hincrby(items_key, "total", 1)
    pipe.hincrby(items_key, "succeeded" if success else "failed", 1)
    pipe.expire(items_key, STATS_TTL_SECONDS)
    await pipe.execute()

    if not success and error_code:
        codes_key = rk.stats_jobs_failures_by_code(_today())
        pipe2 = r.pipeline(transaction=True)
        pipe2.hincrby(codes_key, error_code, 1)
        pipe2.expire(codes_key, STATS_TTL_SECONDS)
        await pipe2.execute()


async def get_stats_summary(
    r: redis.Redis, rk: RedisKeys, service: str, models: list[str], days: int = 1
) -> dict:
    """Sums the last `days` UTC daily buckets (today first) into one report."""
    calls_total = calls_success = calls_failed = 0
    failure_reasons: dict[str, int] = {}
    http_responses: dict[str, int] = {}
    jobs_total = jobs_succeeded = jobs_failed = 0
    jobs_failure_codes: dict[str, int] = {}
    latency_by_model: dict[str, dict[str, float]] = {}

    for i in range(max(days, 1)):
        day = _day(i)

        calls = await r.hgetall(rk.stats_calls(service, day))
        calls_total += int(calls.get("total", 0))
        calls_success += int(calls.get("success", 0))
        calls_failed += int(calls.get("failed", 0))

        for reason, count in (await r.hgetall(rk.stats_failures_by_reason(service, day))).items():
            failure_reasons[reason] = failure_reasons.get(reason, 0) + int(count)

        for error_type, count in (await r.hgetall(rk.stats_http_responses(day))).items():
            http_responses[error_type] = http_responses.get(error_type, 0) + int(count)

        jobs = await r.hgetall(rk.stats_jobs_items(day))
        jobs_total += int(jobs.get("total", 0))
        jobs_succeeded += int(jobs.get("succeeded", 0))
        jobs_failed += int(jobs.get("failed", 0))

        for code, count in (await r.hgetall(rk.stats_jobs_failures_by_code(day))).items():
            jobs_failure_codes[code] = jobs_failure_codes.get(code, 0) + int(count)

        for model in models:
            lat = await r.hgetall(rk.stats_latency(service, model, day))
            if not lat:
                continue
            bucket = latency_by_model.setdefault(model, {"sum_ms": 0.0, "count": 0.0})
            bucket["sum_ms"] += float(lat.get("sum_ms", 0.0))
            bucket["count"] += float(lat.get("count", 0))

    avg_latency_ms_by_model = {
        model: round(b["sum_ms"] / b["count"], 1) for model, b in latency_by_model.items() if b["count"] > 0
    }

    return {
        "days": days,
        "calls": {
            "total": calls_total,
            "success": calls_success,
            "failed": calls_failed,
            "success_rate_pct": round(calls_success / calls_total * 100, 1) if calls_total else None,
        },
        "failure_reasons": failure_reasons,
        "http_responses": http_responses,
        "avg_latency_ms_by_model": avg_latency_ms_by_model,
        "jobs": {
            "items_total": jobs_total,
            "items_succeeded": jobs_succeeded,
            "items_failed": jobs_failed,
            "failed_by_error_code": jobs_failure_codes,
        },
    }
