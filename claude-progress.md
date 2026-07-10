# claude-progress.md - Status

> Last updated: 2026-07-10
> Status: Initial extraction complete, verified end-to-end against real Redis + real Gemini API

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

## Not Yet Done
- Migrating the 7 caller files in `socials-instagram`
  (`script_generator.py`, `instagram_learning.py`, `qualify_utils.py`, `scout_utils.py`,
  `idea_generator.py`, `stage1_context.py`, `stage2_analysis.py`) to call this service
  over HTTP instead of importing `services/support/ai/*` directly. Likely via a thin
  client library that preserves the old `(text, token_count)` tuple contract.
- Second provider (Anthropic, etc.) — only the `Provider` interface exists.
- No hot-reload of `config/models.yaml`.
- No production deployment topology beyond the dev `Dockerfile`/`docker-compose.yml`.
