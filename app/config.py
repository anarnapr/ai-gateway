from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


HARD_MAX_COOLDOWN_SECONDS = 3600.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_keys: str = ""

    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = "aiservice"

    # Key leases are per-key-exclusive (SET NX leased:{kid}), so the useful ceiling is
    # the key count — with 27 keys, 27 means "the whole pool may be busy at once".
    max_in_flight: int = 27
    default_rpm: int = 15

    rate_limit_min_interval_seconds: float = 5.0
    rate_limit_rpm: int = 12

    dead_cooldown_seconds: float = 3600.0
    long_term_threshold_seconds: float = 3600.0

    # Model-wide circuit breaker: per-key cooldowns alone don't move a large pool off
    # a saturated model fast — with N keys, RATE_LIMIT/HIGH_DEMAND only cool one key at
    # a time, so acquire_key() keeps finding a different "available" key on the same
    # model long after the provider is clearly throttling it as a whole. Track failure
    # *velocity* across the model (any key) instead: once `model_circuit_breaker_threshold`
    # RATE_LIMIT/HIGH_DEMAND hits land within `model_circuit_breaker_window_seconds`
    # (regardless of which key), cooldown_model() trips for a short
    # `model_circuit_breaker_cooldown_seconds` so the model drops out of
    # _get_candidate_models() and the pool falls back down model_priority immediately —
    # then self-heals and retries this model again after the short window.
    model_circuit_breaker_threshold: int = 4
    model_circuit_breaker_window_seconds: float = 30.0
    model_circuit_breaker_cooldown_seconds: float = 20.0

    models_config_path: str = "config/models.yaml"

    log_dir: str = "tmp/ai/logs"
    uploads_dir: str = "tmp/ai/uploads"
    log_full_payloads: bool = False

    lease_ttl_ms: int = 120_000
    inflight_slot_ttl_seconds: float = 300.0

    # Bounded internal wait inside acquire_key() before giving up and letting the HTTP
    # handler respond 429 with an accurate Retry-After. Deliberately short: unlike the
    # original CLI/batch context (which blocked with an unbounded while-True loop), an
    # HTTP request should fail fast with retry guidance rather than hold the connection
    # open for a potentially 30+ minute backoff.
    acquire_key_max_wait_seconds: float = 10.0

    # Server-side generate timeout applied when the caller does NOT send its own
    # timeout_seconds. Without this, a hung/slow SDK call never raises, so the retry
    # loop never rotates to another key — the request just blocks until the client's
    # socket read-timeout kills it. A timeout turns "stuck on key A" into "fail fast,
    # rotate to key B".
    default_generate_timeout_seconds: float = 90.0

    # Total wall-clock budget for one /generate request across all internal retries.
    # The handler stops retrying and returns 429 (with Retry-After) once this is hit,
    # so the gateway responds before the caller's HTTP read-timeout fires. Keep this
    # below the client's http timeout (client uses (timeout or 120)+30 = 150s).
    request_deadline_seconds: float = 120.0

    # --- Media-by-URL (POST /v1/generate/media/url) ---
    # Client sends a CDN URL instead of the raw file, so the gateway fetches it
    # server-side. Bounded by scheme/size/timeout only (no private-IP/SSRF allowlist
    # in v1 — this endpoint is assumed to sit behind the same trust boundary as the
    # rest of the gateway; revisit if it's ever exposed to untrusted callers).
    media_url_max_bytes: int = 50 * 1024 * 1024  # 50MB
    media_url_download_timeout_seconds: float = 30.0
    media_url_max_count: int = 10  # cap on media_urls per request; downloads run concurrently

    # --- Transient result cache (GET /v1/generate/result/{request_id}) ---
    # Completed GenerateResponse JSON is stored in Redis under result:{request_id}
    # for this many seconds after a successful /generate (any variant). Lets clients
    # re-fetch the result if the original response was lost in transit. Set to 0 to
    # disable caching entirely.
    result_cache_ttl_seconds: int = 3600  # 1 hour

    # --- Batch jobs API (app/jobs/) ---
    # Async queue: POST /v1/jobs enqueues items in Redis, an in-process asyncio worker
    # pool drains them through run_generate, clients poll GET /v1/jobs/{batch_id}.
    jobs_worker_concurrency: int = 20  # <= max_in_flight; leaves headroom for sync callers
    jobs_poll_interval_seconds: float = 1.0  # idle-queue poll interval
    # Per-attempt wall clock for one job item. Wider than request_deadline_seconds:
    # a reel is a ~50MB upload + a 60-125s generate and has no HTTP client waiting.
    jobs_item_deadline_seconds: float = 300.0
    jobs_item_max_attempts: int = 3  # job-level retries for real generate failures
    jobs_capacity_max_retries: int = 10  # separate budget for pool-exhausted waits
    jobs_retry_delay_seconds: float = 10.0
    jobs_retry_max_delay_seconds: float = 60.0
    # Must exceed worst-case item hold time (attempts * item deadline + retry sleeps),
    # or the reaper steals items from live workers.
    jobs_lease_ttl_seconds: float = 1200.0
    jobs_reaper_interval_seconds: float = 60.0
    jobs_result_ttl_seconds: int = 86_400  # finished batches readable for 24h
    jobs_max_queue_length: int = 1000  # submit -> 429 + Retry-After beyond this
    jobs_max_items_per_batch: int = 200
    jobs_shutdown_grace_seconds: float = 5.0

    @property
    def clamped_dead_cooldown_seconds(self) -> float:
        return min(self.dead_cooldown_seconds, HARD_MAX_COOLDOWN_SECONDS)


@lru_cache
def get_settings() -> Settings:
    return Settings()
