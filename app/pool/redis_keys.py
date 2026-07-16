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

    def model_failure_events(self, model: str) -> str:
        """ZSET of recent RATE_LIMIT/HIGH_DEMAND failure timestamps for this model,
        across all keys — feeds the model-wide circuit breaker (see
        Settings.model_circuit_breaker_*). Distinct from cooldown_model(), which is the
        breaker's trip switch; this is the signal that decides when to trip it."""
        return f"{self.prefix}:cooldown:model_events:{model}"

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

    def jobs_all_batches(self) -> str:
        """ZSET of all batch_ids scored by created_at, for GET /jobs (list-all).
        Members aren't removed when a batch's own keys expire — list_batches()
        lazily ZREMs any member whose jobs_batch() hash has already expired."""
        return f"{self.prefix}:jobs:all_batches"

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

    # --- Aggregate stats (GET /v1/stats) — day-scoped HASHes, long TTL (see
    # app/tracking/stats.py STATS_TTL_SECONDS), separate from CallTracker's own
    # short-TTL quota-window keys above since these are for historical analysis,
    # not rate-limit enforcement. ---

    def stats_calls(self, service: str, yyyymmdd: str) -> str:
        """HASH: total, success, failed — every record_call() bumps this."""
        return f"{self.prefix}:stats:calls:{service}:{yyyymmdd}"

    def stats_failures_by_reason(self, service: str, yyyymmdd: str) -> str:
        """HASH: FailureReason.value -> count. Bumped in report_failure()."""
        return f"{self.prefix}:stats:failure_reasons:{service}:{yyyymmdd}"

    def stats_http_responses(self, yyyymmdd: str) -> str:
        """HASH: GatewayError.error -> count (rate_limited/queue_full/
        media_fetch_failed/all_keys_dead/internal_error) — the HTTP-boundary view,
        distinct from failure_reasons above which is per-key-attempt, not per-response."""
        return f"{self.prefix}:stats:http_responses:{yyyymmdd}"

    def stats_latency(self, service: str, model: str, yyyymmdd: str) -> str:
        """HASH: sum_ms, count — for computing average generate latency per model."""
        return f"{self.prefix}:stats:latency:{service}:{model}:{yyyymmdd}"

    def stats_jobs_items(self, yyyymmdd: str) -> str:
        """HASH: total, succeeded, failed — every JobStore.finish_item() bumps this."""
        return f"{self.prefix}:stats:jobs_items:{yyyymmdd}"

    def stats_jobs_failures_by_code(self, yyyymmdd: str) -> str:
        """HASH: error_code -> count, for finished (failed) job items."""
        return f"{self.prefix}:stats:jobs_failure_codes:{yyyymmdd}"
