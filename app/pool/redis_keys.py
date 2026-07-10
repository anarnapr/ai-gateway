from __future__ import annotations

import hashlib

from app.config import get_settings


def key_id(api_key: str) -> str:
    """Never store raw API keys in Redis — use a stable short hash as the identifier."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def key_suffix(api_key: str) -> str:
    return api_key[-4:] if api_key else "????"


class RedisKeys:
    def __init__(self, prefix: str | None = None):
        self.prefix = prefix or get_settings().redis_key_prefix

    def cooldown_key(self, kid: str) -> str:
        return f"{self.prefix}:cooldown:key:{kid}"

    def cooldown_keymodel(self, kid: str, model: str) -> str:
        return f"{self.prefix}:cooldown:keymodel:{kid}:{model}"

    def cooldown_model(self, model: str) -> str:
        return f"{self.prefix}:cooldown:model:{model}"

    def failure_meta(self, kid: str, model: str = "") -> str:
        return f"{self.prefix}:failure_meta:{kid}:{model}" if model else f"{self.prefix}:failure_meta:{kid}"

    def leased(self, kid: str) -> str:
        return f"{self.prefix}:leased:{kid}"

    def inflight_tokens(self) -> str:
        return f"{self.prefix}:inflight:tokens"

    def usage_key(self, kid: str) -> str:
        return f"{self.prefix}:usage:key:{kid}"

    def usage_rpm(self, kid: str, model: str) -> str:
        return f"{self.prefix}:usage:rpm:{kid}:{model}"

    def usage_rpd(self, kid: str, model: str, yyyymmdd: str) -> str:
        return f"{self.prefix}:usage:rpd:{kid}:{model}:{yyyymmdd}"

    # --- Batch jobs API (async queue; see app/jobs/) ---

    def jobs_queue(self) -> str:
        """LIST of "{batch_id}:{item_id}" entries. LPUSH to produce, LMOVE RIGHT->LEFT
        into jobs_processing() to consume (non-blocking; workers poll)."""
        return f"{self.prefix}:jobs:queue"

    def jobs_processing(self) -> str:
        """LIST of entries currently held by a worker. Entries without a live lease
        are requeued by the reaper."""
        return f"{self.prefix}:jobs:processing"

    def jobs_lease(self, batch_id: str, item_id: str) -> str:
        """STRING with TTL — liveness marker for an in-flight item."""
        return f"{self.prefix}:jobs:lease:{batch_id}:{item_id}"

    def jobs_batch(self, batch_id: str) -> str:
        """HASH: status/total/provider/created_at/finished_at + HINCRBY counters
        (queued/awaiting_media/running/succeeded/failed)."""
        return f"{self.prefix}:jobs:batch:{batch_id}"

    def jobs_batch_items(self, batch_id: str) -> str:
        """LIST of item_ids in submit order."""
        return f"{self.prefix}:jobs:batch_items:{batch_id}"

    def jobs_item(self, batch_id: str, item_id: str) -> str:
        """HASH: status, request (GenerateRequest JSON), metadata JSON, media_path,
        attempts, capacity_retries, result fields, error, error_code, timestamps."""
        return f"{self.prefix}:jobs:item:{batch_id}:{item_id}"

    # --- CallTracker (quota enforcement, keyed by key *suffix* not kid, mirroring
    # the original APICallTracker which only ever saw the last-4-char suffix) ---

    def tracker_rpm(self, model: str, suffix: str) -> str:
        return f"{self.prefix}:tracker:rpm:{model}:{suffix}"

    def tracker_rpd(self, model: str, suffix: str, yyyymmdd: str) -> str:
        return f"{self.prefix}:tracker:rpd:{model}:{suffix}:{yyyymmdd}"

    def tracker_tokens_day(self, model: str, suffix: str, yyyymmdd: str) -> str:
        return f"{self.prefix}:tracker:tokens_day:{model}:{suffix}:{yyyymmdd}"

    def tracker_failures_day(self, model: str, suffix: str, yyyymmdd: str) -> str:
        return f"{self.prefix}:tracker:failures_day:{model}:{suffix}:{yyyymmdd}"
