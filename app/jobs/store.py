from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any, Optional

import redis.asyncio as redis

from app.config import Settings
from app.models.jobs import BatchStatus, ItemStatus
from app.pool.redis_keys import RedisKeys

_COUNTER_FIELDS = ("awaiting_media", "queued", "running", "succeeded", "failed")


def _entry(batch_id: str, item_id: str) -> str:
    return f"{batch_id}:{item_id}"


def _split_entry(entry: str) -> tuple[str, str]:
    batch_id, _, item_id = entry.partition(":")
    return batch_id, item_id


class JobStore:
    """All Redis reads/writes for the batch jobs queue. Redis is the only shared
    state (CLAUDE.md rule), so multiple gateway processes can produce/consume the
    same queue; per-item single-writer safety comes from the claim lease.
    """

    def __init__(self, redis_client: redis.Redis, redis_keys: RedisKeys, settings: Settings):
        self.redis = redis_client
        self.rk = redis_keys
        self.settings = settings

    # ---------- submit ----------

    async def create_batch(self, provider: str, items: list[dict[str, Any]]) -> tuple[str, list[tuple[str, str]]]:
        """items: [{item_id, request_json, metadata, has_media, media_urls}, ...]
        (item_id already assigned/deduped by the endpoint). Returns (batch_id,
        [(item_id, status)]). Items without media, and items with media_urls (the
        worker downloads those itself before generating), are enqueued immediately;
        only has_media items wait in awaiting_media until their multipart upload
        arrives.
        """
        batch_id = uuid.uuid4().hex
        now = time.time()
        # Abandoned batches (e.g. media never uploaded) self-expire at 2x result TTL.
        initial_ttl = self.settings.jobs_result_ttl_seconds * 2

        statuses: list[tuple[str, str]] = []
        awaiting = sum(1 for it in items if it["has_media"])
        queued = len(items) - awaiting

        pipe = self.redis.pipeline(transaction=True)
        pipe.hset(
            self.rk.jobs_batch(batch_id),
            mapping={
                "status": BatchStatus.PENDING.value,
                "provider": provider,
                "total": len(items),
                "created_at": now,
                "awaiting_media": awaiting,
                "queued": queued,
                "running": 0,
                "succeeded": 0,
                "failed": 0,
            },
        )
        pipe.expire(self.rk.jobs_batch(batch_id), initial_ttl)
        pipe.zadd(self.rk.jobs_all_batches(), {batch_id: now})

        for it in items:
            item_id = it["item_id"]
            status = ItemStatus.AWAITING_MEDIA if it["has_media"] else ItemStatus.QUEUED
            statuses.append((item_id, status.value))
            item_key = self.rk.jobs_item(batch_id, item_id)
            mapping = {
                "status": status.value,
                "request": it["request_json"],
                "has_media": int(it["has_media"]),
                "attempts": 0,
                "capacity_retries": 0,
                "created_at": now,
            }
            if it.get("metadata") is not None:
                mapping["metadata"] = json.dumps(it["metadata"])
            if it.get("media_urls"):
                mapping["media_urls"] = json.dumps(it["media_urls"])
            pipe.hset(item_key, mapping=mapping)
            pipe.expire(item_key, initial_ttl)
            pipe.rpush(self.rk.jobs_batch_items(batch_id), item_id)
            if not it["has_media"]:
                pipe.lpush(self.rk.jobs_queue(), _entry(batch_id, item_id))
        pipe.expire(self.rk.jobs_batch_items(batch_id), initial_ttl)
        await pipe.execute()
        return batch_id, statuses

    async def attach_media_and_enqueue(self, batch_id: str, item_id: str, media_path: str) -> None:
        pipe = self.redis.pipeline(transaction=True)
        pipe.hset(
            self.rk.jobs_item(batch_id, item_id),
            mapping={"status": ItemStatus.QUEUED.value, "media_path": media_path},
        )
        pipe.hincrby(self.rk.jobs_batch(batch_id), "awaiting_media", -1)
        pipe.hincrby(self.rk.jobs_batch(batch_id), "queued", 1)
        pipe.lpush(self.rk.jobs_queue(), _entry(batch_id, item_id))
        await pipe.execute()

    # ---------- claim / lifecycle (worker side) ----------

    async def claim_next(self) -> Optional[tuple[str, str, str]]:
        """Atomically move one entry queue->processing and set its lease.
        Returns (batch_id, item_id, entry) or None if the queue is empty.
        Non-blocking by design — no BLMOVE (fakeredis + deterministic shutdown).
        """
        entry = await self.redis.lmove(self.rk.jobs_queue(), self.rk.jobs_processing(), "RIGHT", "LEFT")
        if entry is None:
            return None
        batch_id, item_id = _split_entry(entry)
        await self.redis.set(
            self.rk.jobs_lease(batch_id, item_id),
            "1",
            ex=max(1, math.ceil(self.settings.jobs_lease_ttl_seconds)),
        )
        return batch_id, item_id, entry

    async def refresh_lease(self, batch_id: str, item_id: str) -> None:
        await self.redis.set(
            self.rk.jobs_lease(batch_id, item_id),
            "1",
            ex=max(1, math.ceil(self.settings.jobs_lease_ttl_seconds)),
        )

    async def get_item_raw(self, batch_id: str, item_id: str) -> dict[str, Any]:
        return await self.redis.hgetall(self.rk.jobs_item(batch_id, item_id))

    async def mark_running(self, batch_id: str, item_id: str) -> None:
        pipe = self.redis.pipeline(transaction=True)
        pipe.hset(
            self.rk.jobs_item(batch_id, item_id),
            mapping={"status": ItemStatus.RUNNING.value, "started_at": time.time()},
        )
        pipe.hincrby(self.rk.jobs_batch(batch_id), "queued", -1)
        pipe.hincrby(self.rk.jobs_batch(batch_id), "running", 1)
        pipe.hset(self.rk.jobs_batch(batch_id), "status", BatchStatus.RUNNING.value)
        await pipe.execute()

    async def set_item_fields(self, batch_id: str, item_id: str, mapping: dict[str, Any]) -> None:
        await self.redis.hset(self.rk.jobs_item(batch_id, item_id), mapping=mapping)

    async def finish_item(
        self,
        batch_id: str,
        item_id: str,
        entry: str,
        *,
        success: bool,
        result_fields: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> bool:
        """Terminal transition. Removes the processing entry + lease, writes the
        result, bumps counters, and marks the batch completed when the last item
        lands. Returns True if this call completed the batch.
        """
        status = ItemStatus.SUCCEEDED if success else ItemStatus.FAILED
        mapping: dict[str, Any] = {"status": status.value, "finished_at": time.time()}
        if result_fields:
            mapping.update({k: v for k, v in result_fields.items() if v is not None})
        if error:
            mapping["error"] = error[:500]
        if error_code:
            mapping["error_code"] = error_code

        pipe = self.redis.pipeline(transaction=True)
        pipe.hset(self.rk.jobs_item(batch_id, item_id), mapping=mapping)
        pipe.lrem(self.rk.jobs_processing(), 1, entry)
        pipe.delete(self.rk.jobs_lease(batch_id, item_id))
        pipe.hincrby(self.rk.jobs_batch(batch_id), "running", -1)
        pipe.hincrby(self.rk.jobs_batch(batch_id), status.value, 1)
        await pipe.execute()

        counts = await self.redis.hmget(self.rk.jobs_batch(batch_id), ["succeeded", "failed", "total"])
        succeeded, failed, total = (int(c) if c else 0 for c in counts)
        if total > 0 and succeeded + failed >= total:
            await self._complete_batch(batch_id)
            return True
        return False

    async def _complete_batch(self, batch_id: str) -> None:
        # Idempotent — a race between two finishing workers just rewrites the same
        # fields. Refresh all batch keys down to the result TTL now that it's done.
        ttl = self.settings.jobs_result_ttl_seconds
        pipe = self.redis.pipeline(transaction=True)
        pipe.hset(
            self.rk.jobs_batch(batch_id),
            mapping={"status": BatchStatus.COMPLETED.value, "finished_at": time.time()},
        )
        pipe.expire(self.rk.jobs_batch(batch_id), ttl)
        pipe.expire(self.rk.jobs_batch_items(batch_id), ttl)
        item_ids = await self.redis.lrange(self.rk.jobs_batch_items(batch_id), 0, -1)
        for item_id in item_ids:
            pipe.expire(self.rk.jobs_item(batch_id, item_id), ttl)
        await pipe.execute()

    async def requeue(self, entry: str, batch_id: str, item_id: str) -> None:
        """Put a claimed-but-unfinished item back (shutdown/cancel path). RPUSH so it
        is picked up next (consumers LMOVE from the RIGHT end)."""
        pipe = self.redis.pipeline(transaction=True)
        pipe.lrem(self.rk.jobs_processing(), 1, entry)
        pipe.rpush(self.rk.jobs_queue(), entry)
        pipe.delete(self.rk.jobs_lease(batch_id, item_id))
        # Item goes back to queued; undo mark_running's counter move if it ran.
        await pipe.execute()
        raw = await self.redis.hgetall(self.rk.jobs_item(batch_id, item_id))
        if raw.get("status") == ItemStatus.RUNNING.value:
            pipe = self.redis.pipeline(transaction=True)
            pipe.hset(self.rk.jobs_item(batch_id, item_id), "status", ItemStatus.QUEUED.value)
            pipe.hincrby(self.rk.jobs_batch(batch_id), "running", -1)
            pipe.hincrby(self.rk.jobs_batch(batch_id), "queued", 1)
            await pipe.execute()

    async def drop_entry(self, entry: str, batch_id: str, item_id: str) -> None:
        """Remove a processing entry whose item hash has expired (TTL GC)."""
        pipe = self.redis.pipeline(transaction=True)
        pipe.lrem(self.rk.jobs_processing(), 1, entry)
        pipe.delete(self.rk.jobs_lease(batch_id, item_id))
        await pipe.execute()

    async def reap_stale(self) -> int:
        """Requeue processing entries with no live lease (worker crashed mid-item).
        Safe across processes: the lease key is the liveness signal."""
        entries = await self.redis.lrange(self.rk.jobs_processing(), 0, -1)
        reaped = 0
        for entry in entries:
            batch_id, item_id = _split_entry(entry)
            if await self.redis.exists(self.rk.jobs_lease(batch_id, item_id)):
                continue
            if not await self.redis.exists(self.rk.jobs_item(batch_id, item_id)):
                await self.drop_entry(entry, batch_id, item_id)
                continue
            await self.requeue(entry, batch_id, item_id)
            reaped += 1
        return reaped

    async def queue_length(self) -> int:
        return await self.redis.llen(self.rk.jobs_queue())

    # ---------- status (API side) ----------

    async def list_batches(self) -> list[dict[str, Any]]:
        """Summary (no per-item detail) for every batch still tracked, newest first."""
        batch_ids = await self.redis.zrevrange(self.rk.jobs_all_batches(), 0, -1)
        summaries = []
        for batch_id in batch_ids:
            batch = await self.redis.hgetall(self.rk.jobs_batch(batch_id))
            if not batch:
                await self.redis.zrem(self.rk.jobs_all_batches(), batch_id)
                continue
            summaries.append(
                {
                    "batch_id": batch_id,
                    "status": batch.get("status", BatchStatus.PENDING.value),
                    "total": int(batch.get("total", 0)),
                    "counts": {f: int(batch.get(f, 0)) for f in _COUNTER_FIELDS},
                    "created_at": float(batch.get("created_at", 0.0)),
                    "finished_at": float(batch["finished_at"]) if batch.get("finished_at") else None,
                }
            )
        return summaries

    async def get_batch_status(self, batch_id: str) -> Optional[dict[str, Any]]:
        batch = await self.redis.hgetall(self.rk.jobs_batch(batch_id))
        if not batch:
            return None
        item_ids = await self.redis.lrange(self.rk.jobs_batch_items(batch_id), 0, -1)
        items = []
        for item_id in item_ids:
            raw = await self.redis.hgetall(self.rk.jobs_item(batch_id, item_id))
            items.append(self._item_result(item_id, raw))
        return {
            "batch_id": batch_id,
            "status": batch.get("status", BatchStatus.PENDING.value),
            "total": int(batch.get("total", 0)),
            "counts": {f: int(batch.get(f, 0)) for f in _COUNTER_FIELDS},
            "created_at": float(batch.get("created_at", 0.0)),
            "finished_at": float(batch["finished_at"]) if batch.get("finished_at") else None,
            "items": items,
        }

    async def get_item(self, batch_id: str, item_id: str) -> Optional[dict[str, Any]]:
        raw = await self.redis.hgetall(self.rk.jobs_item(batch_id, item_id))
        if not raw:
            return None
        return self._item_result(item_id, raw)

    @staticmethod
    def _item_result(item_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        def _opt_int(field: str) -> Optional[int]:
            v = raw.get(field)
            return int(v) if v not in (None, "") else None

        def _opt_float(field: str) -> Optional[float]:
            v = raw.get(field)
            return float(v) if v not in (None, "") else None

        return {
            "item_id": item_id,
            "status": raw.get("status", ItemStatus.QUEUED.value),
            "text": raw.get("text"),
            "error": raw.get("error"),
            "error_code": raw.get("error_code"),
            "attempts": int(raw.get("attempts", 0)),
            "input_tokens": _opt_int("input_tokens"),
            "output_tokens": _opt_int("output_tokens"),
            "total_tokens": _opt_int("total_tokens"),
            "api_key_suffix": raw.get("api_key_suffix"),
            "latency_ms": _opt_float("latency_ms"),
            "metadata": json.loads(raw["metadata"]) if raw.get("metadata") else None,
        }
