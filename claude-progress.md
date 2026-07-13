# claude-progress.md - Status

> Last updated: 2026-07-13 (model-wide circuit breaker shipped)
> Status: Batch jobs API complete; production rate-limit incident root-caused and fixed (69 tests green)

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
