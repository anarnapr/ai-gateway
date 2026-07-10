from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any, Optional

from app.api.v1.generate import run_generate
from app.config import Settings
from app.errors import AllKeysDeadHTTPError, PoolExhaustedHTTPError
from app.jobs.store import JobStore
from app.models.requests import GenerateRequest
from app.tracking.usage_logger import UsageLogger

logger = logging.getLogger(__name__)


class JobWorkerPool:
    """In-process asyncio consumers of the Redis jobs queue. Started/stopped from the
    app lifespan (NOT FastAPI BackgroundTasks — those are response-scoped). Each item
    runs through the exact same run_generate pipeline as the sync endpoint, so key
    rotation, same-key media pinning, timeouts, tracking, and logging behave
    identically.
    """

    def __init__(
        self,
        *,
        store: JobStore,
        providers: dict[str, Any],
        pools: dict[str, Any],
        trackers: dict[str, Any],
        rate_limiters: dict[str, Any],
        usage_logger: UsageLogger,
        settings: Settings,
    ):
        self.store = store
        self.providers = providers
        self.pools = pools
        self.trackers = trackers
        self.rate_limiters = rate_limiters
        self.usage_logger = usage_logger
        self.settings = settings
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._stop.clear()
        for i in range(self.settings.jobs_worker_concurrency):
            self._tasks.append(asyncio.create_task(self._worker_loop(i), name=f"jobs-worker-{i}"))
        self._tasks.append(asyncio.create_task(self._reaper_loop(), name="jobs-reaper"))

    async def stop(self) -> None:
        """Graceful drain: workers finish (or requeue) their current item within the
        grace window, stragglers are cancelled (their CancelledError handler requeues).
        Must run BEFORE the Redis client closes — requeueing needs Redis.
        """
        self._stop.set()
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=self.settings.jobs_shutdown_grace_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._tasks = []

    # ---------- loops ----------

    async def _wait_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _worker_loop(self, worker_id: int) -> None:
        while not self._stop.is_set():
            claim = None
            try:
                claim = await self.store.claim_next()
                if claim is None:
                    await self._wait_or_stop(self.settings.jobs_poll_interval_seconds)
                    continue
                batch_id, item_id, entry = claim
                await self._process_item(batch_id, item_id, entry)
            except asyncio.CancelledError:
                if claim is not None:
                    batch_id, item_id, entry = claim
                    await self.store.requeue(entry, batch_id, item_id)
                raise
            except Exception:
                logger.exception("jobs worker %d: unexpected error", worker_id)
                if claim is not None:
                    batch_id, item_id, entry = claim
                    await self.store.finish_item(
                        batch_id, item_id, entry, success=False,
                        error="internal worker error", error_code="worker_error",
                    )

    async def _reaper_loop(self) -> None:
        # Boot sweep first: after a crash/restart, orphaned processing entries must
        # go back on the queue immediately, not one reaper interval later.
        while True:
            try:
                reaped = await self.store.reap_stale()
                if reaped:
                    logger.warning("jobs reaper: requeued %d stale item(s)", reaped)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("jobs reaper: sweep failed")
            if self._stop.is_set():
                return
            await self._wait_or_stop(self.settings.jobs_reaper_interval_seconds)

    # ---------- item processing ----------

    def _cleanup_media(self, media_path: Optional[str]) -> None:
        """Delete the per-item upload dir. Terminal outcomes only — never on
        requeue/cancel, the file must survive for the retry."""
        if not media_path:
            return
        shutil.rmtree(Path(media_path).parent, ignore_errors=True)

    async def _process_item(self, batch_id: str, item_id: str, entry: str) -> None:
        item = await self.store.get_item_raw(batch_id, item_id)
        if not item:
            # Item hash TTL-expired while queued — drop silently.
            await self.store.drop_entry(entry, batch_id, item_id)
            return

        req = GenerateRequest.model_validate_json(item["request"])
        media_path = item.get("media_path") or None

        provider = self.providers.get(req.provider)
        if provider is None:
            await self.store.finish_item(
                batch_id, item_id, entry, success=False,
                error=f"Unknown provider '{req.provider}'", error_code="unknown_provider",
            )
            self._cleanup_media(media_path)
            return

        await self.store.mark_running(batch_id, item_id)
        attempts = int(item.get("attempts", 0))
        capacity_retries = int(item.get("capacity_retries", 0))

        while True:
            try:
                resp = await run_generate(
                    request_id=f"{batch_id}:{item_id}:{attempts}",
                    req=req,
                    provider=provider,
                    pool=self.pools[req.provider],
                    tracker=self.trackers[req.provider],
                    rate_limiter=self.rate_limiters[req.provider],
                    usage_logger=self.usage_logger,
                    settings=self.settings,
                    media_path=media_path,
                    deadline_seconds=self.settings.jobs_item_deadline_seconds,
                )
                await self.store.finish_item(
                    batch_id, item_id, entry, success=True,
                    result_fields={
                        "text": resp.text,
                        "input_tokens": resp.input_tokens,
                        "output_tokens": resp.output_tokens,
                        "total_tokens": resp.total_tokens,
                        "api_key_suffix": resp.api_key_suffix,
                        "latency_ms": round(resp.latency_ms, 1),
                        "attempts": attempts + 1,
                    },
                )
                self._cleanup_media(media_path)
                return

            except (PoolExhaustedHTTPError, AllKeysDeadHTTPError) as e:
                # Capacity, not a defect in this item — separate retry budget so a
                # busy pool doesn't burn the item's real attempts.
                capacity_retries += 1
                await self.store.set_item_fields(batch_id, item_id, {"capacity_retries": capacity_retries})
                if capacity_retries > self.settings.jobs_capacity_max_retries or self._stop.is_set():
                    code = "pool_exhausted" if isinstance(e, PoolExhaustedHTTPError) else "all_keys_dead"
                    await self.store.finish_item(
                        batch_id, item_id, entry, success=False,
                        error=e.detail, error_code=code,
                    )
                    self._cleanup_media(media_path)
                    return
                delay = min(
                    getattr(e, "retry_after_seconds", self.settings.jobs_retry_delay_seconds),
                    self.settings.jobs_retry_max_delay_seconds,
                )
                # Refresh BEFORE sleeping so the reaper doesn't steal the item mid-wait.
                await self.store.refresh_lease(batch_id, item_id)
                await self._wait_or_stop(delay)

            except asyncio.CancelledError:
                raise  # _worker_loop's handler requeues

            except Exception as e:
                attempts += 1
                await self.store.set_item_fields(batch_id, item_id, {"attempts": attempts})
                if attempts >= self.settings.jobs_item_max_attempts or self._stop.is_set():
                    await self.store.finish_item(
                        batch_id, item_id, entry, success=False,
                        error=str(e), error_code="generate_failed",
                    )
                    self._cleanup_media(media_path)
                    return
                await self.store.refresh_lease(batch_id, item_id)
                await self._wait_or_stop(self.settings.jobs_retry_delay_seconds)
