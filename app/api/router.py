from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import generate, health, keys, pool, usage

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(generate.router, prefix="/v1", tags=["generate"])
api_router.include_router(pool.router, prefix="/v1", tags=["pool"])
api_router.include_router(keys.router, prefix="/v1", tags=["keys"])
api_router.include_router(usage.router, prefix="/v1", tags=["usage"])
