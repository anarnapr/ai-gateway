from __future__ import annotations

import math
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.models.responses import GenerateErrorResponse, KeyStatusEntry


class GatewayError(Exception):
    def __init__(self, error: str, detail: str, request_id: str = ""):
        self.error = error
        self.detail = detail
        self.request_id = request_id
        super().__init__(detail)


class PoolExhaustedHTTPError(GatewayError):
    """Every candidate key/model hit backoff within max_retries — caller should retry
    after retry_after_seconds. Maps to HTTP 429 with a Retry-After header, satisfying
    the requirement that a 429 response says when the key becomes useful again.
    """

    def __init__(self, request_id: str, retry_after_seconds: float, key_statuses: Optional[list] = None):
        super().__init__("rate_limited", "All candidate keys/models are in backoff.", request_id)
        self.retry_after_seconds = retry_after_seconds
        self.key_statuses = key_statuses or []


class JobsQueueFullHTTPError(GatewayError):
    """The jobs queue is at jobs_max_queue_length — caller should retry the submit
    later. Maps to HTTP 429 with retry_after_seconds body + Retry-After header
    (same hard product requirement as PoolExhausted)."""

    def __init__(self, request_id: str, retry_after_seconds: float):
        super().__init__("queue_full", "Jobs queue is full; retry the submit later.", request_id)
        self.retry_after_seconds = retry_after_seconds


class AllKeysDeadHTTPError(GatewayError):
    """Every configured key is dead_auth/dead_quota, or no keys are configured. Maps to
    HTTP 503 — this is a total outage, not a transient rate limit.
    """

    def __init__(self, request_id: str, key_statuses: Optional[list] = None):
        super().__init__("all_keys_dead", "No usable API keys are available.", request_id)
        self.key_statuses = key_statuses or []


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(PoolExhaustedHTTPError)
    async def _handle_pool_exhausted(request: Request, exc: PoolExhaustedHTTPError):
        body = GenerateErrorResponse(
            request_id=exc.request_id,
            error=exc.error,
            detail=exc.detail,
            retry_after_seconds=exc.retry_after_seconds,
            key_statuses=[KeyStatusEntry(**k) for k in exc.key_statuses],
        )
        headers = {"Retry-After": str(max(1, math.ceil(exc.retry_after_seconds)))}
        return JSONResponse(status_code=429, content=body.model_dump(), headers=headers)

    @app.exception_handler(JobsQueueFullHTTPError)
    async def _handle_queue_full(request: Request, exc: JobsQueueFullHTTPError):
        body = GenerateErrorResponse(
            request_id=exc.request_id,
            error=exc.error,
            detail=exc.detail,
            retry_after_seconds=exc.retry_after_seconds,
        )
        headers = {"Retry-After": str(max(1, math.ceil(exc.retry_after_seconds)))}
        return JSONResponse(status_code=429, content=body.model_dump(), headers=headers)

    @app.exception_handler(AllKeysDeadHTTPError)
    async def _handle_all_keys_dead(request: Request, exc: AllKeysDeadHTTPError):
        body = GenerateErrorResponse(
            request_id=exc.request_id,
            error=exc.error,
            detail=exc.detail,
            key_statuses=[KeyStatusEntry(**k) for k in exc.key_statuses],
        )
        return JSONResponse(status_code=503, content=body.model_dump())

    @app.exception_handler(GatewayError)
    async def _handle_generic(request: Request, exc: GatewayError):
        body = GenerateErrorResponse(request_id=exc.request_id, error=exc.error, detail=exc.detail)
        return JSONResponse(status_code=500, content=body.model_dump())
