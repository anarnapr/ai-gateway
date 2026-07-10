from __future__ import annotations

import logging
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.config import get_settings
from app.errors import GatewayError, register_exception_handlers
from app.jobs.store import JobStore
from app.jobs.worker import JobWorkerPool
from app.logging_conf import configure_logging
from app.pool.key_pool import AsyncAPIKeyPool
from app.pool.redis_keys import RedisKeys
from app.providers.registry import ProviderRegistry
from app.rate_limit.limiter import RateLimiter
from app.redis_client import close_redis, get_redis
from app.tracking.call_tracker import CallTracker
from app.tracking.usage_logger import UsageLogger

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_dir)
    Path(settings.uploads_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)

    redis_client = get_redis()
    rk = RedisKeys(settings.redis_key_prefix)
    registry = ProviderRegistry(settings.models_config_path)
    usage_logger = UsageLogger(settings.log_dir, log_full_payloads=settings.log_full_payloads)

    pools: dict[str, AsyncAPIKeyPool] = {}
    trackers: dict[str, CallTracker] = {}
    rate_limiters: dict[str, RateLimiter] = {}

    # v1: only "gemini" is wired to an env var of keys. A future provider adds its own
    # keys env var + registration line here (ProviderRegistry already loads its config
    # from models.yaml automatically).
    provider_key_sources = {"gemini": settings.gemini_api_keys}

    for name in registry.names():
        provider = registry.get(name)
        keys_string = provider_key_sources.get(name, "")
        pools[name] = AsyncAPIKeyPool(
            redis_client=redis_client,
            api_keys_string=keys_string,
            model_priority=provider.model_priority(),
            settings=settings,
            rpm=settings.default_rpm,
        )
        trackers[name] = CallTracker(
            redis_client=redis_client,
            redis_keys=rk,
            quota_table=provider.quota_table(),
            model_aliases=provider.model_aliases(),
            service=name,
        )
        rate_limiters[name] = RateLimiter(
            redis_client=redis_client,
            redis_keys=rk,
            rpm_limit=settings.rate_limit_rpm,
            min_interval_seconds=settings.rate_limit_min_interval_seconds,
        )

    app.state.settings = settings
    app.state.provider_registry = registry
    app.state.pools = pools
    app.state.trackers = trackers
    app.state.rate_limiters = rate_limiters
    app.state.usage_logger = usage_logger

    # Batch jobs: Redis-backed queue + in-process asyncio worker pool.
    job_store = JobStore(redis_client, rk, settings)
    job_worker_pool = JobWorkerPool(
        store=job_store,
        providers={name: registry.get(name) for name in registry.names()},
        pools=pools,
        trackers=trackers,
        rate_limiters=rate_limiters,
        usage_logger=usage_logger,
        settings=settings,
    )
    job_worker_pool.start()
    app.state.job_store = job_store
    app.state.job_worker_pool = job_worker_pool

    logger.info("Gateway started. Providers: %s", list(pools.keys()))
    for name, pool in pools.items():
        logger.info("Provider '%s': %d key(s) configured.", name, pool.size())
    logger.info("Jobs worker pool started (%d workers).", settings.jobs_worker_concurrency)

    yield

    # Drain/cancel workers BEFORE closing Redis — requeueing in-flight items needs it.
    await job_worker_pool.stop()
    await close_redis()


app = FastAPI(title="ai-gateway", version="0.1.0", lifespan=lifespan)
app.include_router(api_router)
register_exception_handlers(app)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, GatewayError):
        raise exc
    request_id = getattr(request.state, "request_id", "unknown")
    tb = traceback.format_exc()
    logger.error("Unhandled exception on %s: %s\n%s", request.url.path, exc, tb)
    try:
        usage_logger: UsageLogger = request.app.state.usage_logger
        usage_logger.log_error(request_id=request_id, message=str(exc), traceback_str=tb)
    except Exception:
        pass
    return JSONResponse(status_code=500, content={"error": "internal_error", "detail": str(exc)})
