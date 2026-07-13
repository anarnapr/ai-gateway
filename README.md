# ai-gateway

Standalone LLM gateway microservice: multi-key pool, parallel-worker-safe rate limiting,
per-key cooldown/backoff tracking, and usage logging — over HTTP, so any repo can call it
instead of vendoring the pool/backoff logic locally. Provider-agnostic by design — Gemini
is the first implementation, not the only one.

Originally extracted from `services/support/ai/` in the `socials-instagram` repo, where
this logic was duplicated across projects. v1 ships one concrete `Provider` (Gemini)
behind a pluggable interface so other LLM providers (Anthropic, etc.) can be added later
without redesigning the pool/tracking layer.

## Architecture

```
                      ┌─────────────────────────────┐
   HTTP client  ───►  │        FastAPI app          │
 (any repo/service)   │  app/api/v1/{generate,...}  │
                      └───────────────┬─────────────┘
                                      │
              ┌───────────────────────┼────────────────────────┐
              ▼                       ▼                        ▼
    ┌───────────────────┐  ┌──────────────────────┐  ┌──────────────────┐
    │  ProviderRegistry  │  │  AsyncAPIKeyPool      │  │   CallTracker     │
    │  (config/models    │  │  (cooldowns, backoff, │  │  (rpm/tpm/rpd     │
    │  .yaml)             │  │  in-flight cap, model │  │  quota + usage    │
    │  → GeminiProvider   │  │  fallback)            │  │  counters)        │
    └─────────┬──────────┘  └──────────┬────────────┘  └────────┬─────────┘
              │                        │                        │
              │                        └──────────┬─────────────┘
              ▼                                   ▼
     google-genai SDK                          Redis (shared state,
     (blocking calls run                       correct across multiple
     via asyncio.to_thread)                     worker processes/instances)

   Durable audit log → tmp/ai/logs/calls-YYYY-MM-DD.jsonl (UsageLogger, append-only)
```

Why Redis: the original in-process pool used class-level Python dicts + a
`threading.Semaphore`, which only gives correct cooldown/rate-limit/in-flight-cap
behavior within a single process. Running this as a real multi-worker service (multiple
`uvicorn` workers, or multiple instances) requires that state to be shared — Redis is
that shared store. See the data model reference below.

## Quickstart

```bash
# 1. Start Redis (or use docker-compose, see below)
redis-server &

# 2. Configure
cp .env.example .env
# edit .env: set GEMINI_API_KEYS to a comma-separated list of Gemini API keys

# 3. Install
pip install -e ".[dev]"

# 4. Run
uvicorn app.main:app --reload --port 8080
```

Or via Docker Compose (starts Redis + the app together):

```bash
cp .env.example .env   # set GEMINI_API_KEYS
docker compose up --build
```

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_API_KEYS` | *(required)* | Comma-separated Gemini API keys. **This is the only canonical env var name** — the source repo had a confusing `GEMINI_API`/`GEMINI_API_KEY` split; do not reintroduce that here. |
| `REDIS_URL` | `redis://localhost:6379/0` | Shared pool/quota state. |
| `REDIS_KEY_PREFIX` | `aiservice` | Namespace prefix for all Redis keys this service writes. |
| `MAX_IN_FLIGHT` | `4` | Global concurrent-request cap across all keys/workers, Redis-coordinated. |
| `DEFAULT_RPM` | `15` | Pool's own per-key RPM cap (separate from the per-model quota table in `config/models.yaml`). |
| `RATE_LIMIT_MIN_INTERVAL_SECONDS` | `5.0` | Minimum spacing between requests on the same key. |
| `RATE_LIMIT_RPM` | `12` | Secondary per-key RPM throttle. |
| `DEAD_COOLDOWN_SECONDS` | `3600.0` | Requested cooldown length for dead/exhausted keys. **Always clamped to ≤3600s regardless of this value** — see "Hard 1-hour cap" below. |
| `MODEL_CIRCUIT_BREAKER_THRESHOLD` | `4` | RATE_LIMIT/HIGH_DEMAND hits across any key, within the window below, that trips a short model-wide cooldown. |
| `MODEL_CIRCUIT_BREAKER_WINDOW_SECONDS` | `30.0` | Rolling window the threshold above counts over. |
| `MODEL_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `20.0` | How long the model is dropped from candidates once the breaker trips — short and self-healing, distinct from the 1h `DEAD_COOLDOWN_SECONDS` clamp. |
| `ACQUIRE_KEY_MAX_WAIT_SECONDS` | `10.0` | How long a request will wait internally for a key to free up before the HTTP handler gives up and returns `429` with `Retry-After`. Deliberately short — see design note below. |
| `MODELS_CONFIG_PATH` | `config/models.yaml` | Model priority/aliases/quota table per provider. |
| `LOG_DIR` | `tmp/ai/logs` | Durable JSONL call log + error log + app log. |
| `UPLOADS_DIR` | `tmp/ai/uploads` | Scratch space for `/v1/generate/media` uploads; cleaned up per-request. |
| `LOG_FULL_PAYLOADS` | `false` | When true, also persist full request/response payloads per request under `tmp/ai/logs/requests/`. Off by default (prompts/media may be large or sensitive). |

## API reference

### `POST /v1/generate`

```bash
curl -s -X POST localhost:8080/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Say hello in 3 words"}' | jq .
```

Body: `{provider?, prompt?, parts?, model?, max_retries?, timeout_seconds?, verbose?, metadata?}`
(one of `prompt`/`parts` required — `422` otherwise).

Response `200`:
```json
{
  "request_id": "...", "provider": "gemini", "model": "gemini-2.5-flash",
  "text": "...", "input_tokens": 12, "output_tokens": 8, "total_tokens": 20,
  "api_key_suffix": "a1b2", "attempts": 1, "latency_ms": 812.4
}
```

### `POST /v1/generate/media`

Multipart upload — `file` (the media) + `payload` (JSON-encoded `GenerateRequest` as a
form field). The service streams the upload to a per-request temp dir, feeds it to the
provider (inline part for small images/audio, Gemini File API for video / >10MB), and
deletes the temp file whether the request succeeds or fails.

```bash
curl -s -X POST localhost:8080/v1/generate/media \
  -F 'payload={"prompt":"describe this image"};type=application/json' \
  -F 'file=@photo.jpg' | jq .
```

### Batch jobs API (async, parallel)

For fan-out workloads (e.g. "describe these 36 reels"), don't hold 36 HTTP connections —
submit a batch, let the gateway's internal worker pool (`JOBS_WORKER_CONCURRENCY`, default
20 asyncio workers) process items in parallel across the whole key pool, and poll:

1. **`POST /v1/jobs`** — JSON `{provider?, model?, items: [{item_id?, prompt|parts,
   model?, timeout_seconds?, metadata?, has_media?}]}` → `201 {batch_id, total, items}`.
   Text-only items are queued immediately; `has_media: true` items wait in
   `awaiting_media`. Queue full → `429` with `retry_after_seconds` + `Retry-After`.
2. **`POST /v1/jobs/{batch_id}/items/{item_id}/media`** — multipart `file`, one call per
   media item. Flips the item to `queued`; processing starts immediately (no need to
   finish all uploads first). `409` if the item isn't awaiting media.
3. **`GET /v1/jobs/{batch_id}`** — `{status, counts, items: [...]}` in submit order.
   Poll until `status == "completed"`. Succeeded items carry `text`/token counts;
   failed items carry `error` + `error_code` (`generate_failed` | `pool_exhausted` |
   `all_keys_dead`) — items are never silently dropped. Results expire after 24h
   (`JOBS_RESULT_TTL_SECONDS`).
4. `GET /v1/jobs/{batch_id}/items/{item_id}` — single-item view (debugging).

Item lifecycle: `awaiting_media → queued → running → succeeded | failed`. Each item runs
through the same `run_generate` pipeline as the sync endpoint (key rotation, same-key
media pinning, timeouts, tracking) with a wider per-attempt deadline
(`JOBS_ITEM_DEADLINE_SECONDS`, default 300s — no HTTP client is waiting). Failed attempts
retry server-side: real failures up to `JOBS_ITEM_MAX_ATTEMPTS` (3), pool-capacity waits
on a separate `JOBS_CAPACITY_MAX_RETRIES` (10) budget honoring `retry_after_seconds`.

Reliability: queue and all job state live in Redis (`jobs:queue` → `LMOVE` →
`jobs:processing` + per-item lease). A crashed worker's items are requeued by a reaper
task (boot sweep + every `JOBS_REAPER_INTERVAL_SECONDS`). Graceful shutdown drains or
requeues in-flight items — nothing is lost. Note: uploaded media files are host-local
(`UPLOADS_DIR`) — the one piece of state outside Redis; multi-host workers would need a
shared volume.

Deploy tip for video workloads: set `LEASE_TTL_MS=300000` — the default 120s key-lease
TTL can expire mid-item on a 2-minute reel.

### Error responses

- **`429`** — every candidate key/model is in backoff within `max_retries` / the wait
  budget. Body includes `retry_after_seconds` (min remaining cooldown across keys) **and**
  an HTTP `Retry-After` header is set to the same value, rounded up — this is what
  answers "how much time until this key is useful again."
- **`503`** — every configured key is `dead_auth`/`dead_quota`, or no keys are
  configured at all. Body includes `key_statuses` (which key, which status, why).
- **`422`** — validation error.
- **`500`** — unexpected error, logged to `tmp/ai/logs/errors-*.log`.

### Pool / key / usage inspection

- `GET /v1/pool/status?model=` — bucketed key counts for one model (available, in_use,
  short_cooldown, dead_auth, dead_quota, rate_limited, high_demand, tracker_limited).
- `GET /v1/pool/status/all` — the above for every model in priority order.
- `GET /v1/keys` — one row per configured key: `status`, `reason`, `retry_in_seconds` —
  this is "which API key is dead, and why."
- `GET /v1/usage/summary` — today's rpm/rpd/token usage per model, from the quota table.
- `GET /health`, `GET /health/ready` (checks Redis connectivity + at least one key configured).

Or run the dashboard: `python scripts/quota_dashboard.py --url http://localhost:8080 --watch`

## Design notes

**Hard 1-hour cap.** Every code path that writes a cooldown TTL to Redis funnels through
`AsyncAPIKeyPool`'s single clamp point (`settings.clamped_dead_cooldown_seconds =
min(dead_cooldown_seconds, 3600.0)`), so a misconfigured env var or a future provider bug
computing a larger backoff can never block a key for more than an hour.

**Why `ACQUIRE_KEY_MAX_WAIT_SECONDS` is short.** The original CLI/batch code blocked
inside `get_key()` with an unbounded `while True` loop — fine for a long-running scraper
process. This is now an HTTP request path with a client holding a connection open, so
`acquire_key()` gives up after a short, bounded wait and lets the handler respond `429`
with an accurate `retry_after_seconds` instead of holding the connection for a
potentially 30+ minute backoff.

**Model circuit breaker.** Per-key cooldowns alone don't scale down a large pool fast:
`RATE_LIMIT`/`HIGH_DEMAND` only cools the *one* key that got the error, so with e.g. 27
keys `acquire_key()` keeps finding a different "available" key on the same
externally-throttled model almost indefinitely — the old model-wide blacklist only
tripped once *every* key was individually `dead_auth`/`dead_quota`, which a live
rate-limit storm rarely reaches. `AsyncAPIKeyPool._maybe_trip_model_breaker()` instead
watches failure *velocity* across the whole model (any key): `MODEL_CIRCUIT_BREAKER_THRESHOLD`
hits within `MODEL_CIRCUIT_BREAKER_WINDOW_SECONDS` trips a short
`MODEL_CIRCUIT_BREAKER_COOLDOWN_SECONDS` model-wide cooldown, so the pool falls back down
`model_priority` within seconds and retries the throttled model again shortly after —
self-healing rather than the old hour-long dead-model cooldown. Deliberately does not
fire on `FailureReason.UNKNOWN` (unclassified 400s etc.) — those are as likely to be a
bad request payload as provider capacity, and the jobs worker depends on unclassified
failures propagating fast rather than being absorbed into a cooldown.

**Redis data model** (prefix `{REDIS_KEY_PREFIX}:`, keys are never stored raw — see
`app/pool/redis_keys.py`, hashed via `sha256(api_key)[:16]`):

| Redis key | Type | Purpose |
|---|---|---|
| `cooldown:key:{key_id}` | STRING, EX | Global key cooldown (e.g. `auth_dead`). |
| `cooldown:keymodel:{key_id}:{model}` | STRING, EX | Per-model cooldown (quota/rate-limit/high-demand). |
| `cooldown:model:{model}` | STRING, EX | Model blacklist — all keys exhausted / 404 (long, clamped ≤1h), or circuit breaker tripped (short, `MODEL_CIRCUIT_BREAKER_COOLDOWN_SECONDS`). |
| `cooldown:model_events:{model}` | ZSET, EX | Rolling RATE_LIMIT/HIGH_DEMAND failure timestamps across all keys for a model — feeds the circuit breaker; separate from the trip switch above. |
| `failure_meta:{key_id}[:{model}]` | HASH, EX | `{reason, streak, cooldown_seconds, updated_at}` — drives "dead + why". |
| `leased:{key_id}` | STRING, `SET NX PX` | Atomic cross-process lease with a TTL safety net. |
| `inflight:tokens` | ZSET | Global in-flight cap, Lua-scripted acquire/release. |
| `usage:key:{key_id}` | INTEGER | Least-used tie-break for key selection. |
| `usage:rpm:{key_id}:{model}` | ZSET | Atomic prune+check+reserve via `reserve_rpm.lua`. |
| `tracker:rpm:{model}:{suffix}` / `tracker:rpd:...` / `tracker:tokens_day:...` | ZSET / STRING | `CallTracker`'s quota-table enforcement (separate from the pool's own RPM cap). |
| `jobs:queue` / `jobs:processing` | LIST | Batch jobs work queue; claim = atomic `LMOVE`. |
| `jobs:lease:{batch_id}:{item_id}` | STRING, EX | Worker liveness for an in-flight item; reaper requeues entries without one. |
| `jobs:batch:{batch_id}` | HASH, EX | Batch status + `HINCRBY` counters (queued/running/succeeded/failed/…). |
| `jobs:batch_items:{batch_id}` | LIST, EX | Item ids in submit order. |
| `jobs:item:{batch_id}:{item_id}` | HASH, EX | Item request, status, attempts, result/error fields. |

**Two separate rate-limiting layers**, matching the source repo's original design: the
pool's own simple per-key RPM cap (`DEFAULT_RPM`), and `CallTracker`'s per-model
rpm/tpm/rpd quota table (`config/models.yaml`) — both are checked before a key is
considered available.

**Durable logging**: append-only JSONL, one file per UTC day
(`tmp/ai/logs/calls-YYYY-MM-DD.jsonl`), O(1) per write. This replaces the original
`APICallTracker._save_log()`, which rewrote its entire JSON file on every single call.
Includes explicit `input_tokens`/`output_tokens` fields — the original only ever
persisted `total_token_count` (input/output were logged to console but never stored).
Run `scripts/prune_logs.py` out-of-band (e.g. cron) for retention.

## Adding a new provider

Implement the `Provider` ABC (`app/providers/base.py`): `model_priority()`,
`model_aliases()`, `quota_table()`, `classify_error(str) -> FailureClassification`,
`async generate(ctx) -> ProviderResult`. Register the class in
`app/providers/registry.py`'s `_BUILDERS` dict, add a section to `config/models.yaml`,
and wire its API key env var in `app/main.py`'s `provider_key_sources`. Nothing else
needs to change — the pool, tracker, rate limiter, and all `/v1/*` endpoints are
provider-agnostic.

## Testing

```bash
pip install -e ".[dev]"
pytest -v
```

All tests run against `fakeredis` (no real Redis needed) and a mocked `google.genai`
client — no network calls or real API keys required.

## Not yet done / follow-up work

- The 7 caller files in `socials-instagram` that currently import
  `services/support/ai/*` directly still need to be migrated to call this service over
  HTTP (likely via a thin client library). Not part of this repo.
- No hot-reload of `config/models.yaml` — restart the service after editing it.
- Only the `Provider` interface + Gemini implementation exist; a second provider
  (Anthropic, etc.) is future work.

## Recent changes

- **Model-wide circuit breaker** (2026-07-13): production job (27-key pool) sat on one
  rate-limited preview model for 15+ minutes instead of falling back — per-key cooldowns
  only ever cooled the one key that failed, so a large pool kept finding another
  "available" key on the same throttled model. `AsyncAPIKeyPool._maybe_trip_model_breaker()`
  now trips a short model-wide cooldown off failure velocity (any key) instead of
  requiring every key to individually die. New `MODEL_CIRCUIT_BREAKER_*` settings. 69
  tests total.
- Initial extraction from `services/support/ai/` in `socials-instagram`: FastAPI +
  Redis-backed pool/tracker/rate-limiter, Gemini provider, `/v1/generate[/media]`,
  pool/keys/usage/health endpoints, Docker Compose dev setup, test suite.
