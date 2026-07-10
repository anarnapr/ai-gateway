from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.models.responses import HealthResponse
from app.redis_client import ping

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/health/ready", response_model=HealthResponse)
async def health_ready(request: Request, response: Response) -> HealthResponse:
    redis_ok = await ping()
    pools = getattr(request.app.state, "pools", {})
    keys_configured = sum(pool.size() for pool in pools.values())

    if not redis_ok or keys_configured == 0:
        response.status_code = 503

    return HealthResponse(
        status="ok" if redis_ok and keys_configured > 0 else "not_ready",
        redis_ok=redis_ok,
        keys_configured=keys_configured,
    )
