# CLAUDE.md - Architecture & Rules

## What This Is
`ai-gateway` is a standalone FastAPI microservice extracted from `services/support/ai/`
in `socials-instagram`. It wraps a multi-key LLM provider pool (cooldowns, exponential
backoff, model fallback, rate limiting) behind an HTTP API so multiple repos can share
one pool instead of vendoring the logic. Built behind a pluggable `Provider` interface —
Gemini is the only implementation in v1, but other providers (Anthropic, etc.) can be
added later without redesigning the pool/tracker layer.

## Commands
- **Install**: `pip install -e ".[dev]"`
- **Run**: `uvicorn app.main:app --reload --port 8080`
- **Run (Docker)**: `docker compose up --build` (starts Redis + the app)
- **Test**: `pytest -v` (uses `fakeredis` + mocked `google.genai` — no real Redis/keys needed)
- **Dashboard**: `python scripts/quota_dashboard.py --url http://localhost:8080 --watch`
- **Prune logs**: `python scripts/prune_logs.py --days 14`

## Code Style
- **Provider abstraction**: All provider-specific logic (model list, error-string
  classification, quota table, SDK calls) lives behind `app/providers/base.py`'s
  `Provider` ABC. The pool and tracker are provider-agnostic — never special-case a
  provider name outside `app/providers/<name>/`.
- **Redis is the only shared state.** No class-level Python dicts, no local JSON files
  for pool/cooldown state (that was the original in-process design's limitation — see
  README "Why Redis"). Any new shared counter/flag goes in `app/pool/redis_keys.py`.
  API keys are never stored raw in Redis — always hash via `key_id()`.
- **Config over hardcoding**: model priority lists, quota tables, and aliases belong in
  `config/models.yaml`, not in Python. No hot-reload in v1 — restart to pick up edits.
- **Async everywhere in the request path.** Blocking SDK calls (`google.genai`) must be
  wrapped in `asyncio.to_thread` (see `app/providers/gemini/provider.py`) so they don't
  block the event loop. **Also bound every such call with `asyncio.wait_for`** — a bare
  `to_thread` with no timeout can hang forever on a stalled network call, and since
  `to_thread` shares one process-wide default executor thread pool, enough hung calls
  eventually starve *unrelated* work too (see Known Gotchas: upload_media hang).

## Hard Constraints (do not relax without discussion)
- **1-hour cooldown cap**: every cooldown TTL write funnels through
  `settings.clamped_dead_cooldown_seconds` in `app/config.py`. Never write a raw
  `dead_cooldown_seconds` value to Redis without going through this clamp.
- **429 responses must include `retry_after_seconds`** (body) and a `Retry-After` header —
  this is a hard product requirement, not a nice-to-have.
- **`acquire_key()` must stay bounded** (`ACQUIRE_KEY_MAX_WAIT_SECONDS`, default 10s).
  This is an HTTP service — don't reintroduce the original CLI's unbounded blocking wait.
- **Input/output tokens must be persisted separately**, not just total. The source repo's
  `APICallTracker` only ever stored `total_token_count`; this was a fixed gap the service
  requirements explicitly call out — don't regress it.

## Batch Jobs (app/jobs/)
- Async queue: `POST /v1/jobs` → Redis queue → in-process asyncio `JobWorkerPool`
  (started/stopped in `app/main.py` lifespan — never FastAPI `BackgroundTasks`, those
  are response-scoped). Workers reuse `run_generate` from `app/api/v1/generate.py`;
  do not fork a second generation pipeline.
- **No blocking Redis list ops** (`BLPOP`/`BLMOVE`) in workers — fakeredis-based tests
  and graceful shutdown rely on the non-blocking `LMOVE` + poll design.
- Worker shutdown (`JobWorkerPool.stop()`) must run **before** `close_redis()` —
  requeueing in-flight items during drain needs Redis.
- Refresh the item lease (`JobStore.refresh_lease`) before any retry sleep, or the
  reaper will requeue an item a live worker still holds.
- `GET /v1/jobs` lists every tracked batch (summary only, no items) via the
  `jobs:all_batches` ZSET (`RedisKeys.jobs_all_batches()`), scored by `created_at`.
  `create_batch()` `ZADD`s into it; nothing ever explicitly removes a member on
  completion/expiry — `JobStore.list_batches()` lazily `ZREM`s any batch_id whose
  `jobs_batch()` hash has already TTL'd out. Any new way of creating a batch must
  also `ZADD` here or it won't show up in the list endpoint.
- Uploaded batch media lives under `UPLOADS_DIR/jobs/{batch_id}/{item_id}/` on the
  local filesystem — the ONE piece of shared state outside Redis. Fine single-host;
  multi-host workers need a shared volume. The worker deletes the dir only on terminal
  success/failure, never on requeue.

## Known Gotchas
- **Classify by stored reason, not cooldown duration.** `classify_key_status`'s global
  cooldown branch used to infer `dead_auth` vs `short_cooldown` purely from remaining TTL
  magnitude (mirroring the original `APIKeyPool`) — this races the clock, since remaining
  duration ticks down from the moment `mark_cooldown()` runs. Always check the stored
  `failure_meta` reason first; only fall back to duration heuristics if no reason is
  recorded.
- **Global vs per-model failure metadata**: `mark_cooldown()` (used for `auth_dead`)
  writes to `failure_meta:{key_id}` (no model). Per-model failures (`rate_limit`,
  `high_demand`, `quota_exhausted`) write to `failure_meta:{key_id}:{model}`. Anything
  that needs "why is this key blocked" (pool status, `/v1/keys`) must check both via
  `AsyncAPIKeyPool.get_effective_failure_meta()` — don't read the per-model hash alone.
- **Test log isolation**: the `api_client` pytest fixture redirects `LOG_DIR`/`UPLOADS_DIR`
  to `tmp_path`. If you add a new fixture that boots the app, do the same — otherwise
  test runs will write into (and pollute) the real `tmp/ai/logs/` a local dev server
  also reads from.
- **Per-key cooldown alone doesn't move a large pool off a saturated model.**
  `RATE_LIMIT`/`HIGH_DEMAND` only ever cooled the one key that failed
  (`cooldown_keymodel`) — with a big key pool, `acquire_key()` kept finding a different
  "available" key on the same externally-throttled model almost indefinitely, since a
  model-wide cooldown previously only tripped once *every* key was individually
  `dead_auth`/`dead_quota`. `AsyncAPIKeyPool._maybe_trip_model_breaker()`
  (`app/pool/key_pool.py`) now trips a short model-wide `cooldown_model()` off failure
  *velocity* — `model_circuit_breaker_threshold` RATE_LIMIT/HIGH_DEMAND hits across any
  key within `model_circuit_breaker_window_seconds` — so the pool falls back down
  `model_priority` in seconds instead of never. Self-heals after
  `model_circuit_breaker_cooldown_seconds`.
- **`FailureReason.UNKNOWN` must stay a no-op in `report_failure()`.** Tempting to add a
  cooldown there too (unclassified errors can hot-loop with zero backoff), but the jobs
  worker (`app/jobs/worker.py`) relies on unclassified `run_generate` failures
  propagating as a real exception — bounded item-level retries, then reported as
  `generate_failed`. Cooling the key/model there instead routes retries through
  `PoolExhaustedHTTPError`'s much larger capacity-retry budget, which just delays
  reporting a real (often permanent, request-shaped) failure. Covered by
  `tests/test_key_pool.py::test_unknown_failure_does_not_cool_key_or_model` — don't
  regress it without re-reading `test_failed_items_carry_error_not_silent_drop` first.
- **A caller-supplied `model` was silently ignored until 2026-07-19.** `run_generate`
  computed `model = provider.resolve_model(req.model)` but never passed it to
  `pool.acquire_key()`, whose signature had no `model` param at all —
  `_get_candidate_models()` always iterated the full `model_priority` list, so
  `attempt_model = key_model or model` was always overwritten by whatever model the pool
  happened to find a key for. A client asking for one specific model could silently get
  a different one back. Fixed: `acquire_key(model=...)` restricts candidate selection to
  exactly that model (no fallback) when the caller actually sent one; omitting `model`
  is unchanged (full fallback). Also added `UnknownModelHTTPError` (`422 unknown_model`)
  for a pinned model not in `model_priority`. Covered by
  `tests/test_key_pool.py::test_acquire_key_honors_pinned_model` /
  `test_acquire_key_pinned_model_does_not_fall_back` and
  `tests/test_api_generate.py::test_generate_with_pinned_model_uses_that_model` /
  `test_generate_with_unknown_model_returns_422`. Any future change to `acquire_key()`
  must keep threading the caller's model through — don't let this regress silently.
- **`redis.from_url()`'s connection pool defaults to 100 max connections** (redis-py
  default, not unlimited) — this service's fan-out (`acquire_key()` gathers a
  `leased:*` check per configured key, per candidate model, times
  `jobs_worker_concurrency` parallel workers, plus sync traffic) can exceed that under
  load, raising `MaxConnectionsError` on ordinary Redis calls. Now sized via
  `settings.redis_max_connections` (default 200, `app/redis_client.py`) instead of the
  library default — raise it further for large key pools / high worker concurrency
  rather than leaving it unset.

## Observability (app/api/v1/capacity.py, stats.py, app/tracking/stats.py)
- **`GET /v1/capacity`** — single-call readiness signal for a caller deciding whether to
  submit more work: key-pool headroom (`AsyncAPIKeyPool.get_pool_status()`), global
  in-flight usage (`AsyncAPIKeyPool.current_in_flight()`, reads the same
  `inflight:tokens` ZSET `acquire_inflight.lua` writes, pruning stale slots first), and
  jobs-queue headroom (`JobStore.queue_length()` vs `jobs_max_queue_length`). Returns
  `accepting_more_work: bool` + `reasons: []`. Don't let this drift from the actual
  enforcement points above — if a new capacity constraint is added elsewhere, add it
  here too or the signal becomes misleading.
- **`GET /v1/stats`** — day-scoped counters for offline analysis (calls total/success/
  failed, failure reason breakdown, HTTP response codes, per-model avg latency, job
  item outcomes), summed over a trailing N-day window (`days` query param, ≤90 =
  `stats.STATS_TTL_SECONDS`). Every counter is written from exactly one existing choke
  point, not scattered across call sites: `CallTracker.record_call()` (calls +
  latency), `AsyncAPIKeyPool.report_failure()` (failure reasons — unconditional, even
  for `STALE_MEDIA`/`UNKNOWN` which are no-ops for cooldown purposes), the exception
  handlers in `app/errors.py` (HTTP-boundary response codes — distinct signal from
  failure reasons, since one HTTP 429 can follow many per-key failures), and
  `JobStore.finish_item()` (job item outcomes). Any new failure/response/outcome path
  must call the matching `app/tracking/stats.py` function from its own choke point,
  not reimplement counting inline.

## Database / State
No SQL database — all shared state lives in Redis (see README "Redis data model" for the
full key reference). Durable audit logs are plain append-only JSONL files under
`tmp/ai/logs/calls-YYYY-MM-DD.jsonl` (gitignored, created at runtime).

**Result cache** (`result:{request_id}` STRING, TTL=`RESULT_CACHE_TTL_SECONDS`): every
successful `run_generate` call writes the full `GenerateResponse` JSON to Redis
(best-effort — a Redis failure is logged/swallowed, never turns a 200 into a 500). The
`GET /v1/generate/result/{request_id}` endpoint reads it back. Set
`RESULT_CACHE_TTL_SECONDS=0` to disable.

## Not Yet Done
- The 7 caller files in `socials-instagram` that import `services/support/ai/*` directly
  still need migrating to call this service over HTTP — not part of this repo.
- Second provider implementation (Anthropic, etc.) — only the interface exists.
- No hot-reload of `config/models.yaml`.
