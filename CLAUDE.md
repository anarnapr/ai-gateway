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
  block the event loop.

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

## Database / State
No SQL database — all shared state lives in Redis (see README "Redis data model" for the
full key reference). Durable audit logs are plain append-only JSONL files under
`tmp/ai/logs/calls-YYYY-MM-DD.jsonl` (gitignored, created at runtime).

## Not Yet Done
- The 7 caller files in `socials-instagram` that import `services/support/ai/*` directly
  still need migrating to call this service over HTTP — not part of this repo.
- Second provider implementation (Anthropic, etc.) — only the interface exists.
- No hot-reload of `config/models.yaml`.
