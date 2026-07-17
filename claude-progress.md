# claude-progress.md - Status

> Last updated: 2026-07-17 (Result cache + re-fetch endpoint — GET /v1/generate/result/{request_id})
> Status: Result cache complete; 89 tests green

## Current State
`ai-gateway` is a new standalone FastAPI microservice, extracted from
`services/support/ai/` in `socials-instagram`. v1 ships one concrete `Provider` (Gemini),
built behind a pluggable interface designed for future multi-provider support. Redis backs all shared
pool/quota state so cooldowns, in-flight caps, and rate limits are correct across
multiple worker processes/instances — the original implementation's class-level Python
dicts only worked within a single process.

## Completed Milestones
- [x] Repo scaffolded: FastAPI app, `pyproject.toml`, Docker/Docker Compose, `.env.example`.
- [x] `AsyncAPIKeyPool` — Redis-backed port of the original `APIKeyPool`: cooldown states
  (`available/in_use/short_cooldown/dead_auth/dead_quota/high_demand/rate_limited/tracker_limited`),
  exponential backoff with jitter, model fallback, atomic key leasing + in-flight cap via
  Lua scripts (`acquire_inflight.lua`, `reserve_rpm.lua`).
- [x] `Provider` abstraction (`app/providers/base.py`) + `GeminiProvider` — error-string
  classification and SDK calls isolated from pool/cooldown logic, so a second provider
  only needs to implement the ABC.
- [x] `CallTracker` — Redis-backed rpm/tpm/rpd quota enforcement, replacing the original's
  full-file-rewrite-per-call log. Fixed a real gap from the source repo: input/output
  tokens are now persisted separately (only `total_token_count` was stored before).
- [x] `UsageLogger` — append-only JSONL audit log, one file per UTC day
  (`tmp/ai/logs/calls-YYYY-MM-DD.jsonl`), O(1) per write.
- [x] HTTP API: `POST /v1/generate`, `POST /v1/generate/media` (multipart upload, since
  media is now a remote call not a local path), `GET /v1/pool/status[/all]`, `GET
  /v1/keys`, `GET /v1/usage/summary`, `GET /health[/ready]`.
- [x] `429` responses include `retry_after_seconds` + `Retry-After` header; `503` for
  total key exhaustion with per-key `key_statuses` (status + reason).
- [x] Hard 1-hour cooldown cap enforced at a single clamp point
  (`settings.clamped_dead_cooldown_seconds`).
- [x] `scripts/quota_dashboard.py` (rich CLI, ported from `quota_check.py`, now hits the
  HTTP API instead of reading local files) and `scripts/prune_logs.py`.
- [x] 41 tests (`fakeredis` + mocked `google.genai`), all passing.
- [x] README, CLAUDE.md, AGENTS.md written.
- [x] **Batch jobs API** (`/v1/jobs`, 2026-07-10): async parallel processing. Submit N
  items in one JSON request (media uploaded per-item afterwards), Redis queue
  (`LMOVE` + lease + reaper — crash-safe, nothing lost), in-process asyncio
  `JobWorkerPool` (default 20 workers) reusing `run_generate`, poll
  `GET /v1/jobs/{batch_id}` for results in submit order. Server-side retries: 3
  attempts for real failures + separate 10-retry budget for pool-capacity waits
  honoring `retry_after_seconds`. Failed items return `error`/`error_code` — no more
  silent drops. Results expire after 24h. `max_in_flight` raised 4→27 (= key count;
  leases are per-key-exclusive so that's the true ceiling). 67 tests total.
- [x] **Model-wide circuit breaker** (2026-07-13): a real batch job on the 27-key pool
  sat hammering `gemini-3.1-flash-lite-preview` for 15+ minutes despite continuous 429s,
  never falling back to the next model in `model_priority`. Root cause: `RATE_LIMIT`/
  `HIGH_DEMAND` only ever cooled the *one* key that got the error
  (`cooldown_keymodel`); the model-wide blacklist only tripped once *every* key was
  individually `dead_auth`/`dead_quota`, which a live rate-limit storm on a large pool
  essentially never reaches — with 27 keys there's almost always some other key off
  cooldown, so `acquire_key()` kept "succeeding" locally while Google kept 429ing.
  Added `AsyncAPIKeyPool._maybe_trip_model_breaker()`: tracks RATE_LIMIT/HIGH_DEMAND
  failure *velocity* across the whole model (any key) in a Redis ZSET
  (`cooldown:model_events:{model}`), trips a short model-wide `cooldown_model()` once
  `MODEL_CIRCUIT_BREAKER_THRESHOLD` hits land within `MODEL_CIRCUIT_BREAKER_WINDOW_SECONDS`
  — pool falls back down `model_priority` in seconds, self-heals after
  `MODEL_CIRCUIT_BREAKER_COOLDOWN_SECONDS` (default 20s) instead of the old 1h dead-model
  cooldown. Considered also backing off `FailureReason.UNKNOWN` (currently a no-op in
  `report_failure()`, so unclassified errors like Gemini's transient "unable to process
  input image" 400 hot-loop with zero delay) — reverted after it broke
  `test_failed_items_carry_error_not_silent_drop`: cooling the key/model on UNKNOWN
  routes retries through `PoolExhaustedHTTPError`'s much larger capacity-retry budget
  instead of letting the jobs worker's bounded item-retry path report
  `generate_failed` quickly. Left as an intentional no-op — see CLAUDE.md gotchas. 69
  tests total.
- [x] **`GET /v1/jobs` list-all endpoint** (2026-07-14): there was previously no way to
  see every batch the gateway was handling without already knowing a `batch_id` — every
  read path (`get_batch_status`, `get_item`, `queue_length`) required one in hand. Added
  a `jobs:all_batches` Redis ZSET (scored by `created_at`), populated by `create_batch()`;
  `JobStore.list_batches()` reads it back newest-first and lazily `ZREM`s any batch_id
  whose `jobs_batch()` hash has already expired (same pattern as the reaper's
  `drop_entry`). Returns one summary row per batch (`status`, `total`, `counts`,
  timestamps) — no per-item detail, use the existing per-batch endpoint for that.

- [x] **`POST /v1/generate/media/url`** (2026-07-15): clients were pushing large media
  through `/v1/generate/media`'s multipart body just so the gateway could turn around
  and use it — wasteful on constrained client networks when the media already lives on
  a CDN. Added a JSON sibling endpoint that takes `media_urls` (list, so multiple CDN
  links can attach to one generate call) and downloads them server-side, concurrently
  (`app/media_fetch.py`, streamed via `httpx`, each capped by `MEDIA_URL_MAX_BYTES`/
  `MEDIA_URL_DOWNLOAD_TIMEOUT_SECONDS` even if the server lies about `Content-Length`;
  list length capped by `MEDIA_URL_MAX_COUNT`) into per-index subdirs under the
  per-request `UPLOADS_DIR` dir (so same-basename files from different CDNs don't
  collide), then hands off to the same generate pipeline. Kept as a new endpoint rather
  than an alternate mode on the existing one — multipart-form and JSON-body are
  different enough request shapes that branching one handler on payload type would've
  been messier than two thin handlers sharing `run_generate`. Deliberately no
  private-IP/SSRF allowlist in v1 (user chose scheme/size/timeout-only guarding); one
  failed url fails the whole request with `422 media_fetch_failed`, distinct from
  pool/quota errors (no partial-success silent-drop). Required generalizing
  `GenerateContext.media_path` (singular) → `media_paths: list[str]` plus per-path
  `uploaded_refs` tracking through `run_generate`'s upload/retry/cooldown logic and
  `GeminiProvider.generate`, so one call can mix File-API-uploaded and inline media in
  any order — `run_generate` still accepts a single `media_path` kwarg for the
  unchanged single-file callers (`/v1/generate/media`, jobs worker), normalized
  internally into the same list. 86 tests total (12 new: `tests/test_media_fetch.py`,
  `tests/test_api_generate_media_url.py`; 2 existing tests in
  `tests/test_api_generate_media.py`/`tests/test_api_jobs.py` updated for the
  `ctx.media_paths` list + `ctx.extra["uploaded_refs"]` dict rename).
- [x] **`media_urls` on batch job items** (2026-07-15): the multi-URL work above made
  clear that batch jobs — fan-out over many items — is actually the case where the
  multipart-upload network cost hurts most, more than the single-item sync endpoint.
  Added `JobItemSpec.media_urls: list[str]`, mutually exclusive with `has_media`
  (`422` if both set). Unlike `has_media` items, `media_urls` items skip
  `awaiting_media` entirely and are queued immediately at submit —
  `JobWorkerPool._download_item_media()` (`app/jobs/worker.py`) downloads them
  concurrently, once per `_process_item()` call, right before the existing retry loop,
  into `UPLOADS_DIR/jobs/{batch_id}/{item_id}/{index}/` (reusing `app/media_fetch.py`,
  same `MEDIA_URL_MAX_BYTES`/`_DOWNLOAD_TIMEOUT_SECONDS`/`_MAX_COUNT` limits as the sync
  endpoint). Per user's explicit choice: a download failure finishes the item
  immediately as `error_code: media_fetch_failed`, deliberately bypassing
  `jobs_item_max_attempts` — a bad url isn't a transient generate error and shouldn't
  burn/extend that retry budget (same non-retry stance the sync endpoint already took).
  Downloaded paths are NOT persisted to the item's Redis hash — only kept in the
  worker's local scope for that `_process_item()` call — so a crash + reaper requeue
  redownloads from scratch rather than resuming; accepted as fine since the source urls
  are still valid (see "Known Operational Notes"). `_cleanup_media` was generalized from
  "delete `Path(media_path).parent`" to "delete `UPLOADS_DIR/jobs/{batch_id}/{item_id}/`
  unconditionally" so it correctly cleans up whether media arrived via multipart upload
  or url download. 86 tests total (5 new: 2 in `tests/test_job_worker.py`, 1 rewritten
  there to match the real per-item dir layout instead of an ad-hoc `tmp_path`; 3 new in
  `tests/test_api_jobs.py`).
- [x] **`GET /v1/capacity` + `GET /v1/stats`** (2026-07-16): two new observability
  endpoints, both read-only, no new write paths. `/v1/capacity` answers "should I
  submit more work right now" in one call: key-pool headroom (`get_pool_status()`),
  global in-flight usage (new `AsyncAPIKeyPool.current_in_flight()` — reads the same
  `inflight:tokens` ZSET `acquire_inflight.lua` writes, pruning stale slots first so a
  crashed request doesn't count against capacity forever), and jobs-queue depth vs
  `jobs_max_queue_length`, rolled into `accepting_more_work: bool` + `reasons: []`.
  `/v1/stats?days=` aggregates day-scoped Redis hashes (new `app/tracking/stats.py`,
  90-day TTL, separate from `CallTracker`'s own short-TTL quota-window keys) into
  calls total/success/failed, failure-reason breakdown, HTTP response codes actually
  returned to callers, per-model average latency, and job item outcomes by
  `error_code`. Each counter is written from exactly one existing choke point
  (`CallTracker.record_call`, `AsyncAPIKeyPool.report_failure`, the 5 exception
  handlers in `app/errors.py`, `JobStore.finish_item`) rather than duplicated inline
  at call sites — verified live against a real `/v1/generate` call (5 internal
  attempts: 3 `auth_dead`, 1 `rate_limit`, 1 success — stats matched exactly) and a
  real batch job.
- [x] **Fixed unbounded `upload_media()` hang** (2026-07-16): found while investigating
  a live batch job stuck at 13/27 items "running" for 15+ minutes with zero Gemini
  calls in the logs — including plain-text items with no media at all. Root cause:
  `GeminiProvider.upload_media()` wrapped its blocking SDK call in `asyncio.to_thread`
  but never bounded it with `asyncio.wait_for`, unlike `generate()` right below it in
  the same file. A stalled `client.files.upload()` HTTP call could hang forever;
  because `asyncio.to_thread` shares one process-wide default executor thread pool,
  enough hung uploads eventually starved every other task waiting for a free thread —
  explaining why unrelated text-only items stalled too. Confirmed via Redis: the
  stuck items' lease TTL was ticking down with zero refreshes (69s → 54s over 15 real
  seconds), meaning the worker coroutine never reached its retry-sleep point — it was
  parked inside the very first `await run_generate(...)` call. Fixed by wrapping the
  whole upload (transfer + existing 600s ACTIVE-state poll) in
  `asyncio.wait_for(timeout=780s)`. Verified with a monkeypatched hang scenario
  (raises `TimeoutError` at the configured timeout instead of blocking).
- [x] **Result cache + re-fetch endpoint** (2026-07-17): every successful generate call
  (all three variants: `/v1/generate`, `/v1/generate/media`, `/v1/generate/media/url`)
  now stores the full `GenerateResponse` JSON in Redis for `RESULT_CACHE_TTL_SECONDS`
  (default 1h) under key `result:{request_id}`. A new `GET
  /v1/generate/result/{request_id}` endpoint lets clients re-fetch the exact same
  payload if the original HTTP response was lost in transit (network drop, client crash,
  intermittent connectivity) without re-running (expensive) generation. The cache write
  is best-effort — a Redis failure is caught, logged as a warning, and swallowed, so a
  Redis blip never converts a successful 200 generate into a 500. Set
  `RESULT_CACHE_TTL_SECONDS=0` to disable entirely. Implementation: new
  `_cache_and_return()` async helper in `app/api/v1/generate.py`; new
  `RedisKeys.result_cache(request_id)` in `app/pool/redis_keys.py`; new
  `result_cache_ttl_seconds` setting in `app/config.py`; new `get_redis_client()`
  dep helper; 3 new tests in `tests/test_api_generate.py` (re-fetch succeeds, 404 for
  unknown ID, TTL=0 disables cache). Job workers call `run_generate` without
  `redis_client` so the cache is skipped there (job results already live in `JobStore`).
  Client repos (`socials-instagram`, `socials-x`) gain a `fetch_result(request_id)`
  helper in their `ai_gateway_client.py`. 89 tests total.

## Bugs Found & Fixed During Verification
- [x] **Cooldown classification race**: `classify_key_status` inferred `dead_auth` vs
  `short_cooldown` from remaining cooldown *duration* against a threshold equal to the
  cooldown length itself (mirroring the original `APIKeyPool`'s own heuristic) — any
  elapsed time between `mark_cooldown()` and the next check pushed remaining duration
  below the threshold, misclassifying a fresh dead key as merely short-cooldown. Fixed to
  classify by stored failure `reason` first.
- [x] **Missing reason on dead keys**: `get_pool_status()` and `/v1/keys` only read
  per-model failure metadata (`failure_meta:{key_id}:{model}`), but `auth_dead` is
  recorded globally (`failure_meta:{key_id}`, no model) via `mark_cooldown()` — so dead
  keys showed `reason: null`. Added `get_effective_failure_meta()` (per-model, falling
  back to global) and wired both call sites to use it.
- [x] **Cross-key File API 403 killed the whole pool** (production incident, 2026-07-10):
  media flow uploaded the video with one key but generated with a different one; Gemini
  File refs are key/project-scoped, so generate got
  `403 "You do not have permission to access the File"`, which matched
  `permission_denied` in `_AUTH_MARKERS` → healthy key dead-cooled for 1h → cascade →
  "all keys busy, retry after ~3400s". Fixed three ways: upload+generate now pinned to
  ONE key per attempt (re-upload on key rotation); new `STALE_MEDIA` failure reason
  classified before the auth markers (never cools a key); `report_failure` no-ops on it.
- [x] **Hung generate never rotated keys**: callers sending no `timeout_seconds` meant
  no `asyncio.wait_for` around the SDK call — a slow/hung call blocked until the
  client's socket timeout, holding the key the whole time (observed 125s call).
  Added `default_generate_timeout_seconds` (90s) so hangs raise and the retry loop
  rotates to the next key, plus `request_deadline_seconds` (120s) — a total wall-clock
  budget across internal retries so the gateway 429s (with Retry-After) before the
  client's 150s read-timeout fires. Jobs workers override via `deadline_seconds=300`.

## Verified End-to-End (2026-07-10)
Ran the service against a real local Redis container and real Gemini API (with
intentionally invalid keys) to exercise the full failure path outside of mocks:
`/health`, `/health/ready`, `/v1/pool/status`, `/v1/keys` all correct on a fresh pool;
`POST /v1/generate` against invalid keys correctly classified both keys as `dead_auth`
(reason populated, TTL clamped to ≤3600s) and returned `503` with `key_statuses`; durable
JSONL log confirmed input/output/total token fields present on the (mocked, from an
earlier test run) success entry and full error detail captured on failures.

## Known Operational Notes
- `tmp/ai/logs/*` and `tmp/ai/uploads/*` are local runtime state (gitignored) — safe to
  inspect for debugging, safe to delete (uploads are per-request temp dirs cleaned up
  automatically; logs are append-only and pruned via `scripts/prune_logs.py`, not
  synchronously).
- Redis holds all pool/cooldown/quota state — flushing it (`FLUSHALL`) resets every key
  to a clean `available` state, useful for local testing.
- `ACQUIRE_KEY_MAX_WAIT_SECONDS` (default 10s) bounds how long a request will wait
  internally before the handler gives up and returns `429` — a deliberate departure from
  the original CLI's unbounded blocking wait, since this is now an HTTP request path.
- **Set `LEASE_TTL_MS=300000` for video workloads** — the default 120s key-lease TTL can
  expire mid-item on a ~2-minute reel, letting a second request lease the same key
  (non-fatal, but causes per-key RPM contention).
- Some configured keys have **zero regional quota** (`quota_limit_value: '0'`,
  `asia-southeast1`) — permanent per-project condition, not transient; those
  projects/keys should be dropped or moved to a supported region.
- The Gemini upload ACTIVE-poll (`provider.py::_upload_sync`) still discards and
  re-uploads the whole file when a poll `GET` hits a transient 429 — known
  inefficiency, harden later (retry the GET instead).

## Not Yet Done
- **`app/media_fetch.py` (shared by `/v1/generate/media/url` and `JobItemSpec.media_urls`)
  has no SSRF/private-IP guard** — only scheme + streamed size/timeout limits (user's
  explicit choice over IP-range blocking). Fine as long as neither is exposed to
  untrusted callers; add DNS-resolve + reject-private-range checks before either is.
- **`media_urls` batch job items redownload from scratch on crash + reaper requeue** —
  the downloaded paths only live in `JobWorkerPool._process_item()`'s local scope, not
  the item's Redis hash, so a worker crash mid-item means the next attempt refetches
  every url. Acceptable (urls are still valid; rare case) but a `media_paths` Redis
  field would avoid the wasted download if this turns out to matter in practice.
- **No test coverage yet for `GET /v1/jobs`** — the list-all endpoint (see milestone
  above) shipped without a corresponding entry in `tests/test_api_jobs.py`. 69 tests
  total is unchanged from before this endpoint was added.
- **No test coverage yet for `GET /v1/capacity` or `GET /v1/stats`** — both shipped
  verified only via live manual smoke-testing against the running container (real
  `/v1/generate` call, real batch job), not unit tests. 86 tests total is unchanged
  from before these endpoints were added.
- **`FailureReason.UNKNOWN` still gets zero cooldown** — Gemini's transient "unable to
  process input image" 400 (68 occurrences in one day's log) retries the same key/model
  back-to-back with no delay. Tried a short backoff, reverted (see the circuit-breaker
  entry above) because it conflicts with the jobs worker's fast-fail contract for
  genuinely permanent per-request failures. Needs a more surgical fix (e.g. distinguish
  "likely transient" vs "likely permanent" UNKNOWN messages) before revisiting.
- **`.env`'s `GEMINI_API_KEYS` mixes credential formats** — some entries are `AQ.Ab8...`
  (looks like a Google OAuth token, not a Gemini Developer API key), which
  `genai.Client(api_key=...)` rejects with `400 API_KEY_INVALID` every time. Already
  gets classified `AUTH_DEAD` and 1h-cooled correctly, so it's not a hot-loop, but it's
  dead weight in the pool — should be pruned to real `AIzaSy...` keys.
- **Pool's own RPM cap ignores the per-model `quota_table`** — `AsyncAPIKeyPool.rpm` is
  one flat value (`DEFAULT_RPM`, 15) applied to every model uniformly; the per-model
  rpm/tpm/rpd in `config/models.yaml` is only enforced by `CallTracker`, a separate
  mechanism. Not wired together — worth reconciling if per-model RPM limits diverge much
  from `DEFAULT_RPM`.
- Migrating `socials-instagram`'s batch fan-out callers (`instagram_learning.py`,
  `qualify_utils.py`, `stage1_context.py`, `idea_generator.py`) from client-side
  thread pools over sync `/v1/generate/media` to the new `/v1/jobs` submit→upload→poll
  flow. One-shot callers (`script_generator.py`, `scout_utils.py`,
  `stage2_analysis.py`) stay on the sync endpoint.
- Client-side retry-on-429 honoring `retry_after_seconds`: **done in `socials-x`**
  (`services/support/ai/ai_gateway_client.py`, retries ≤2× with capped sleeps, gives up
  immediately when `retry_after > 300s`); still missing in `socials-instagram`'s copy.
- Jobs API client helpers (`submit_batch` / `poll_batch`) in the caller repos.
- Harden the upload ACTIVE-poll against transient 429s (see Operational Notes).
- Cancel endpoint / webhooks for jobs (polling-only v1).
- Second provider (Anthropic, etc.) — only the `Provider` interface exists.
- No hot-reload of `config/models.yaml`.
- No production deployment topology beyond the dev `Dockerfile`/`docker-compose.yml`;
  batch media under `UPLOADS_DIR/jobs/` is host-local, so multi-host workers would need
  a shared volume.
