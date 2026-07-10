from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class GenerateResponse(BaseModel):
    request_id: str
    provider: str
    model: str
    text: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    api_key_suffix: str
    attempts: int
    latency_ms: float


class KeyStatusEntry(BaseModel):
    suffix: str
    status: str
    reason: Optional[str] = None
    retry_in_seconds: float = 0.0
    failure_streak: Optional[int] = None


class GenerateErrorResponse(BaseModel):
    request_id: str
    error: str
    detail: str
    retry_after_seconds: Optional[float] = None
    key_statuses: Optional[list[KeyStatusEntry]] = None


class PoolStatusResponse(BaseModel):
    model: str
    total_keys: int
    available: int
    in_use: int
    short_cooldown: int
    permanently_blocked: int
    in_flight_limit: int
    next_retry_seconds: Optional[float] = None
    keys: dict


class HealthResponse(BaseModel):
    status: str
    redis_ok: Optional[bool] = None
    keys_configured: Optional[int] = None
