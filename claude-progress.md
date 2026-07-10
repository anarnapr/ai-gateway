# claude-progress.md - Status

> Last updated: 2026-07-10 (evening — incident fixes + batch jobs API shipped)
> Status: Batch jobs API complete (67 tests green); production incident root-caused and fixed

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
