# ai-gateway Agent Operating Manual

## What This Project Does
`ai-gateway` is a standalone FastAPI microservice that fronts a multi-key LLM provider
pool: cooldown/backoff/model-fallback state machine, rate limiting, usage tracking, and
durable logging — all shared via Redis so multiple worker processes/instances (and
eventually multiple calling repos) see consistent pool state. Extracted from
`services/support/ai/` in `socials-instagram` so the logic doesn't need to be duplicated
across projects. Built behind a `Provider` interface — Gemini is the first implementation,
not the only one; other providers can be added without touching the pool/tracker layer.

## Running It
```bash
cp .env.example .env        # set GEMINI_API_KEYS
docker compose up --build   # Redis + app together

# or locally:
redis-server &
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8080
```

## API Usage
```bash
curl -X POST localhost:8080/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Say hello in 3 words"}'

curl -X POST localhost:8080/v1/generate/media \
  -F 'payload={"prompt":"describe this image"};type=application/json' \
  -F 'file=@photo.jpg'

# Or skip the multipart upload entirely — gateway fetches the file(s) itself:
curl -X POST localhost:8080/v1/generate/media/url \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"describe this image","media_urls":["https://cdn.example.com/photo.jpg"]}'

# Re-fetch a past result without re-running generation (valid for RESULT_CACHE_TTL_SECONDS):
curl localhost:8080/v1/generate/result/{request_id}

curl localhost:8080/v1/pool/status
curl localhost:8080/v1/keys
curl localhost:8080/v1/usage/summary
curl localhost:8080/v1/capacity          # "should I submit more work right now" verdict
curl localhost:8080/v1/stats?days=7      # calls/failures/429s/latency for analysis
curl localhost:8080/health/ready

# Batch jobs (async, parallel across the key pool — poll for results):
curl -X POST localhost:8080/v1/jobs -H 'Content-Type: application/json' \
  -d '{"items": [
    {"item_id": "a", "prompt": "one"},
    {"item_id": "b", "prompt": "two", "has_media": true},
    {"item_id": "c", "prompt": "three", "media_urls": ["https://cdn.example.com/c.mp4"]}
  ]}'
curl -X POST localhost:8080/v1/jobs/{batch_id}/items/b/media -F 'file=@reel.mp4'  # only needed for has_media items — "c" above is already queued
curl localhost:8080/v1/jobs/{batch_id}
curl localhost:8080/v1/jobs   # list every batch still tracked (summary only)
```

## Project Structure
```
gemini/
├── app/
│   ├── main.py                # FastAPI app, lifespan (redis/provider/pool wiring)
│   ├── config.py               # env-var settings, hard 1h cooldown clamp
│   ├── api/v1/                 # generate, jobs, pool, keys, usage, health routers
│   ├── jobs/                    # batch jobs: JobStore (Redis queue/state) + JobWorkerPool
│   ├── models/                 # pydantic request/response models, shared enums
│   ├── providers/               # Provider ABC + registry + per-provider impls
│   │   └── gemini/              # GeminiProvider: SDK calls + error classification
│   ├── pool/                    # AsyncAPIKeyPool (Redis-backed), backoff formula, Lua scripts
│   ├── tracking/                 # CallTracker (rpm/tpm/rpd quota), UsageLogger (JSONL)
│   └── rate_limit/               # per-key throttle
├── config/models.yaml           # model priority / aliases / quota table (per provider)
├── scripts/                     # quota_dashboard.py (rich CLI), prune_logs.py
├── tests/                       # fakeredis + mocked SDK, no real Redis/keys needed
└── tmp/ai/logs, tmp/ai/uploads   # runtime state, gitignored
```

## Development Rules
- **Provider-agnostic core**: pool, tracker, rate limiter, and `/v1/*` routes must never
  reference "gemini" by name except as the default/only entry in `app/main.py`'s
  `provider_key_sources` dict and `config/models.yaml`. All Gemini-specific logic lives
  under `app/providers/gemini/`.
- **Redis is the single source of shared state.** Don't add process-local caches for
  anything that needs to be correct across multiple workers (cooldowns, in-flight count,
  usage counters) — see `app/pool/redis_keys.py` for the naming scheme.
- **Cooldowns never exceed 1 hour.** Always write cooldown TTLs through
  `settings.clamped_dead_cooldown_seconds`, never a raw duration.
- **Model-wide throttling must key off failure velocity, not "every key individually
  dead."** With a large pool, per-key `RATE_LIMIT`/`HIGH_DEMAND` cooldowns alone never
  get the pool to fall back to the next model — see
  `AsyncAPIKeyPool._maybe_trip_model_breaker()` in `app/pool/key_pool.py` and the
  `MODEL_CIRCUIT_BREAKER_*` settings.
- **`FailureReason.UNKNOWN` stays uncooled in `report_failure()`.** The jobs worker
  needs unclassified failures to propagate as a real exception (bounded item retries →
  `generate_failed`), not get absorbed into the pool's capacity-retry path. See the
  "Known Gotchas" entry in `CLAUDE.md` before touching this.
- **429 must carry retry guidance**: `retry_after_seconds` in the body and a
  `Retry-After` HTTP header, every time.
- **Keep `acquire_key()` bounded** (`ACQUIRE_KEY_MAX_WAIT_SECONDS`) — this is an HTTP
  service, not a batch job; don't hold connections open for long backoffs.
- **Batch jobs rules** (`app/jobs/`): workers are asyncio tasks started/stopped in the
  `app/main.py` lifespan (never FastAPI `BackgroundTasks`); they must reuse
  `run_generate` from `app/api/v1/generate.py`, never a second pipeline. No blocking
  Redis list ops (`BLPOP`/`BLMOVE`) — tests (fakeredis) and graceful shutdown depend on
  the non-blocking `LMOVE` + poll design. `JobWorkerPool.stop()` runs before
  `close_redis()`. Refresh the item lease before any retry sleep or the reaper steals
  the item. Batch media under `UPLOADS_DIR/jobs/` is the one non-Redis piece of shared
  state (host-local); delete only on terminal success/failure.
- **`JobItemSpec.media_urls`** (mutually exclusive with `has_media`) queues the item
  immediately — no `awaiting_media` wait, no follow-up multipart call.
  `JobWorkerPool._download_item_media()` fetches the urls (via `app/media_fetch.py`,
  same limits as the sync endpoint) right before the retry loop in `_process_item()`,
  once per call — NOT persisted to Redis, so a crash + reaper requeue just redownloads
  (acceptable; the url is still valid). A download failure finishes the item as
  `media_fetch_failed` immediately, deliberately outside the normal
  `jobs_item_max_attempts` retry budget — a bad url isn't a transient generate error,
  don't route it through that retry path (mirrors the sync endpoint's non-retry stance).
- **`/v1/generate/media/url`** (`app/media_fetch.py`) downloads one or more
  client-supplied CDN urls (`media_urls: list[str]`, capped by `media_url_max_count`)
  server-side instead of the caller pushing raw bytes through multipart, then feeds
  the whole batch through the same `run_generate`/`GenerateContext.media_paths`
  pipeline `/v1/generate/media` uses. Downloads run concurrently, each bounded by
  `media_url_max_bytes`/`media_url_download_timeout_seconds` (streamed, enforced even
  if the server lies about `Content-Length`) — one failed url fails the whole request
  (`422 media_fetch_failed`), it does not silently drop that file. No private-IP/SSRF
  allowlist in v1 (applies to both this endpoint and `JobItemSpec.media_urls`, same
  `app/media_fetch.py` code path) — trusts the same boundary as the rest of the
  gateway; add one before exposing either to untrusted callers.
- **`GenerateContext.media_paths` is a list, not a single path** — `Provider.generate()`
  implementations must iterate it (pairing each path with `ctx.extra["uploaded_refs"]`
  when a File-API ref exists for that path, else treating it as inline media) rather
  than assuming exactly one file. `run_generate()` still accepts a single `media_path`
  kwarg for existing single-file callers (`/v1/generate/media`, jobs worker) and
  normalizes it into the same list internally — a new provider only needs to handle
  the list form.
- **Result cache is best-effort** — `_cache_and_return()` in `app/api/v1/generate.py`
  catches any Redis write error, logs a warning, and returns the `GenerateResponse`
  normally. Never let a cache path convert a successful generation into a 500. The
  rule: if the write is purely for the client's benefit (re-fetch) and not for
  correctness (pool state, quotas), swallow the error.
- **New provider checklist**: implement `Provider` ABC (`app/providers/base.py`),
  register in `app/providers/registry.py`'s `_BUILDERS`, add a section to
  `config/models.yaml`, wire its key env var in `app/main.py`. Nothing else changes.
- **A caller-pinned `model` must mean exactly that model, no substitution.**
  `run_generate` (`app/api/v1/generate.py`) resolves `req.model` (alias lookup) and,
  only when the caller actually sent one, passes it into `pool.acquire_key(model=...)`.
  `AsyncAPIKeyPool._get_candidate_models()` (`app/pool/key_pool.py`) restricts candidate
  models to exactly that one when a pin is given — never falls back to
  `model_priority`. Omitting `model` keeps the full-fallback behavior. An unrecognized
  pinned model (not in `provider.model_priority()`) must fail fast with
  `UnknownModelHTTPError` (`422 unknown_model`) before any pool/key work, not reach the
  SDK. Don't reintroduce a path where `acquire_key()` is called without threading the
  caller's pin through — that silently substitutes whatever model the pool finds a key
  for, which is the bug this fixed.
- **Update the README** ("Recent changes" section) whenever a feature is added — this
  convention carries over from the source repo's CLAUDE.md.

## Testing
All tests run against `fakeredis` (no real Redis) and a monkeypatched `google.genai`
client (no real API keys/network). The `api_client` fixture in `tests/conftest.py`
redirects `LOG_DIR`/`UPLOADS_DIR` to pytest's `tmp_path` — reuse that pattern for any new
fixture that boots the app, to avoid polluting the real `tmp/ai/` directory.

```bash
pytest -v
```
