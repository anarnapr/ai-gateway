from __future__ import annotations

from fastapi import Request

from app.config import Settings
from app.pool.key_pool import AsyncAPIKeyPool
from app.providers.base import Provider
from app.providers.registry import ProviderRegistry
from app.rate_limit.limiter import RateLimiter
from app.tracking.call_tracker import CallTracker
from app.tracking.usage_logger import UsageLogger


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_provider_registry(request: Request) -> ProviderRegistry:
    return request.app.state.provider_registry


def get_provider(request: Request, provider_name: str = "gemini") -> Provider:
    provider = get_provider_registry(request).get(provider_name)
    if provider is None:
        raise ValueError(f"Unknown provider: {provider_name}")
    return provider


def get_key_pool(request: Request, provider_name: str = "gemini") -> AsyncAPIKeyPool:
    return request.app.state.pools[provider_name]


def get_call_tracker(request: Request, provider_name: str = "gemini") -> CallTracker:
    return request.app.state.trackers[provider_name]


def get_rate_limiter(request: Request, provider_name: str = "gemini") -> RateLimiter:
    return request.app.state.rate_limiters[provider_name]


def get_usage_logger(request: Request) -> UsageLogger:
    return request.app.state.usage_logger


def get_job_store(request: Request):
    return request.app.state.job_store
