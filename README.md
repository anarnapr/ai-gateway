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
| `REDIS_MAX_CONNECTIONS` | `200` | Client-side connection pool cap for the shared Redis client. redis-py defaults this to 100 when unset, which this service's fan-out (`acquire_key()` gathers a `leased:*` check per configured key, per candidate model, times `JOBS_WORKER_CONCURRENCY` parallel workers) can exceed under load, raising `MaxConnectionsError`. Raise further for very large key pools or worker concurrency. |
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
| `UPLOADS_DIR` | `tmp/ai/uploads` | Scratch space for `/v1/generate/media` and `/v1/generate/media/url` uploads/downloads; cleaned up per-request. |
| `LOG_FULL_PAYLOADS` | `false` | When true, also persist full request/response payloads per request under `tmp/ai/logs/requests/`. Off by default (prompts/media may be large or sensitive). |
| `MEDIA_URL_MAX_BYTES` | `52428800` (50MB) | Max size the gateway will download for each `media_urls` entry, enforced while streaming. |
| `MEDIA_URL_DOWNLOAD_TIMEOUT_SECONDS` | `30.0` | Timeout for each server-side `media_urls` fetch. |
| `MEDIA_URL_MAX_COUNT` | `10` | Max number of urls accepted in one `/v1/generate/media/url` request; downloads run concurrently. |
| `RESULT_CACHE_TTL_SECONDS` | `3600` | How long a successful `GenerateResponse` is kept in Redis under `result:{request_id}` for re-fetch. Set to `0` to disable. |

## API reference

### `POST /v1/generate`

```bash
curl -s -X POST localhost:8080/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Say hello in 3 words"}' | jq .
```

Body: `{provider?, prompt?, parts?, model?, max_retries?, timeout_seconds?, verbose?, metadata?}`
(one of `prompt`/`parts` required — `422` otherwise).

`model` behavior: omit it and the gateway falls back down the full `model_priority` list
in `config/models.yaml` as usual (aliases resolved, e.g. `gemini-3.1` →
`gemini-3.1-flash-preview`). **Send it, and the gateway pins the request to exactly that
model — no cross-model fallback.** If every key is cooled down for that one model, the
request fails (`429`/`503`) rather than silently substituting a different model. An
unrecognized model name (not in `model_priority`, e.g. a typo or a model id that doesn't
exist for this provider) returns `422 {"error": "unknown_model", ...}` immediately,
before any pool/key work. Same behavior applies to batch job items' `model` field.

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

### `POST /v1/generate/media/url`

Same as `/v1/generate/media`, but for media already hosted somewhere reachable (a CDN,
S3, etc.) — send the url(s) instead of the bytes and the gateway downloads them
server-side, in parallel. Avoids the client pulling media down just to push it right
back up to us, which matters for large video on constrained networks.

Body: `{provider?, prompt?, parts?, model?, max_retries?, timeout_seconds?, verbose?,
metadata?, media_urls}` (`media_urls`: non-empty list of urls, required, `422`
otherwise; capped at `MEDIA_URL_MAX_COUNT` entries).

```bash
# single url
curl -s -X POST localhost:8080/v1/generate/media/url \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"describe this image","media_urls":["https://cdn.example.com/photo.jpg"]}' | jq .

# multiple urls — all attached to the same generate call
curl -s -X POST localhost:8080/v1/generate/media/url \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"compare these","media_urls":["https://cdn.example.com/a.jpg","https://cdn.example.com/b.jpg"]}' | jq .
```

Each download is bounded by `MEDIA_URL_MAX_BYTES` (default 50MB, enforced while
streaming even if the server lies about `Content-Length`) and
`MEDIA_URL_DOWNLOAD_TIMEOUT_SECONDS` (default 30s); the list itself is capped by
`MEDIA_URL_MAX_COUNT` (default 10). A bad/unreachable url, wrong scheme, timeout, or
oversized body on *any* entry fails the whole request with
`422 {"error": "media_fetch_failed", "detail": "..."}` — not a pool/quota error, since
the problem is a caller-supplied url, not key/model state. No private-IP/SSRF
allowlist in v1 (see "Not yet done" below) — only add this endpoint to a deployment
where callers are already trusted to the same degree as the rest of the gateway.

### `GET /v1/generate/result/{request_id}`

Re-fetch a completed `GenerateResponse` without re-running generation. Every successful
call to `/v1/generate`, `/v1/generate/media`, or `/v1/generate/media/url` stores the
full response JSON in Redis for `RESULT_CACHE_TTL_SECONDS` (default 1 hour) keyed by
the `request_id` returned in the response body.

```bash
# after a successful generate, save the request_id:
RESPONSE=$(curl -s -X POST localhost:8080/v1/generate -H 'Content-Type: application/json' -d '{"prompt": "hi"}')
REQUEST_ID=$(echo $RESPONSE | jq -r .request_id)

# re-fetch the same result later (within RESULT_CACHE_TTL_SECONDS):
curl -s localhost:8080/v1/generate/result/$REQUEST_ID | jq .
```

Returns `200` with the identical `GenerateResponse` payload, or `404` if the entry has
expired or the request never succeeded. The cache write is best-effort — a Redis
failure is logged and swallowed (the original response is still returned normally).

### Batch jobs API (async, parallel)

For fan-out workloads (e.g. "describe these 36 reels"), don't hold 36 HTTP connections —
submit a batch, let the gateway's internal worker pool (`JOBS_WORKER_CONCURRENCY`, default
20 asyncio workers) process items in parallel across the whole key pool, and poll:

1. **`POST /v1/jobs`** — JSON `{provider?, model?, items: [{item_id?, prompt|parts,
   model?, timeout_seconds?, metadata?, has_media?, media_urls?}]}` →
   `201 {batch_id, total, items}`. Text-only items are queued immediately;
   `has_media: true` items wait in `awaiting_media` for a follow-up upload (step 2);
   `media_urls: [...]` items are ALSO queued immediately — no follow-up call, the
   worker downloads them itself right before generating (see below). `has_media` and
   `media_urls` are mutually exclusive on one item (`422` if both set). Each item's
   `media_urls` list is capped at `MEDIA_URL_MAX_COUNT`. Queue full → `429` with
   `retry_after_seconds` + `Retry-After`.
2. **`POST /v1/jobs/{batch_id}/items/{item_id}/media`** — multipart `file`, one call per
   `has_media` item (skip this entirely for `media_urls` items). Flips the item to
   `queued`; processing starts immediately (no need to finish all uploads first). `409`
   if the item isn't awaiting media.
3. **`GET /v1/jobs/{batch_id}`** — `{status, counts, items: [...]}` in submit order.
   Poll until `status == "completed"`. Succeeded items carry `text`/token counts;
   failed items carry `error` + `error_code` (`generate_failed` | `pool_exhausted` |
   `all_keys_dead`) — items are never silently dropped. Results expire after 24h
   (`JOBS_RESULT_TTL_SECONDS`).
4. `GET /v1/jobs/{batch_id}/items/{item_id}` — single-item view (debugging).
5. `GET /v1/jobs` — list every batch still tracked (newest first), one summary row
   each: `{batch_id, status, total, counts, created_at, finished_at}` — no per-item
   detail. Use this to see everything the gateway is currently handling without
   knowing a `batch_id` up front.

Item lifecycle: `awaiting_media → queued → running → succeeded | failed` (`has_media`
items) or `queued → running → succeeded | failed` (text-only and `media_urls` items —
no upload step to wait on). Each item runs through the same generate pipeline as the
sync endpoint (key rotation, same-key File-API media pinning, timeouts, tracking) with a
wider per-attempt deadline (`JOBS_ITEM_DEADLINE_SECONDS`, default 300s — no HTTP client
is waiting). Failed attempts retry server-side: real failures up to
`JOBS_ITEM_MAX_ATTEMPTS` (3), pool-capacity waits on a separate
`JOBS_CAPACITY_MAX_RETRIES` (10) budget honoring `retry_after_seconds`. `media_urls`
downloads are the one exception to that retry policy — a bad/unreachable url fails the
item immediately (`error_code: media_fetch_failed`, no retry), same non-retry stance as
the sync `/v1/generate/media/url` endpoint, since a bad url is a caller-input problem,
not a transient generate failure.

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
- **`422`** — validation error (bad body shape, or `error: "unknown_model"` when a
  pinned `model` isn't in `model_priority`).
- **`500`** — unexpected error, logged to `tmp/ai/logs/errors-*.log`.

### Pool / key / usage inspection

- `GET /v1/pool/status?model=` — bucketed key counts for one model (available, in_use,
  short_cooldown, dead_auth, dead_quota, rate_limited, high_demand, tracker_limited).
- `GET /v1/pool/status/all` — the above for every model in priority order.
- `GET /v1/keys` — one row per configured key: `status`, `reason`, `retry_in_seconds` —
  this is "which API key is dead, and why."
- `GET /v1/usage/summary` — today's rpm/rpd/token usage per model, from the quota table.
- `GET /v1/capacity` — single-call "should I submit more work right now" signal: key-pool
  headroom, global in-flight usage vs `MAX_IN_FLIGHT`, jobs-queue depth vs
  `JOBS_MAX_QUEUE_LENGTH`, and a rolled-up `accepting_more_work: bool` + `reasons: []`
  (`no_keys_available` / `in_flight_at_limit` / `jobs_queue_full`).
- `GET /v1/stats?days=` (default 1, max 90) — call/failure/latency/job stats for offline
  analysis: total/success/failed calls, failure-reason breakdown (`rate_limit`,
  `auth_dead`, ...), HTTP response codes actually returned to callers (429/503/422),
  per-model average latency, and job item outcomes + failure codes. Day-scoped Redis
  counters, 90-day retention.
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
| `jobs:all_batches` | ZSET, scored by `created_at` | Every batch_id ever created; feeds `GET /v1/jobs` (list-all). Members for expired batches are lazily `ZREM`'d on read, not TTL'd directly. |
| `stats:calls:{service}:{yyyymmdd}` | HASH, 90d EX | `total`/`success`/`failed` — bumped by `CallTracker.record_call()`. Feeds `GET /v1/stats`. |
| `stats:failure_reasons:{service}:{yyyymmdd}` | HASH, 90d EX | `FailureReason.value` → count, bumped unconditionally in `AsyncAPIKeyPool.report_failure()`. |
| `stats:http_responses:{yyyymmdd}` | HASH, 90d EX | `GatewayError.error` → count (`rate_limited`/`queue_full`/`media_fetch_failed`/`all_keys_dead`/...), bumped in `app/errors.py`'s exception handlers. |
| `stats:latency:{service}:{model}:{yyyymmdd}` | HASH, 90d EX | `sum_ms`/`count` for average generate latency per model. |
| `stats:jobs_items:{yyyymmdd}` / `stats:jobs_failure_codes:{yyyymmdd}` | HASH, 90d EX | Job item total/succeeded/failed + failure breakdown by `error_code`, bumped in `JobStore.finish_item()`. |
| `result:{request_id}` | STRING, `RESULT_CACHE_TTL_SECONDS` EX | Full `GenerateResponse` JSON for re-fetch after a successful generate. Best-effort write; never blocks or fails the original request. |

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
- `/v1/generate/media/url` and batch jobs' `media_urls` have no private-IP/SSRF
  allowlist — they trust callers to the same degree as the rest of the gateway. Add
  DNS-resolve + reject-private-ranges (`app/media_fetch.py`) before exposing either to
  untrusted clients.
- A crashed worker + reaper requeue on a `media_urls` item redownloads all of that
  item's urls from scratch (the downloaded paths only live in the worker's local
  `_process_item` scope, not persisted to Redis) — acceptable since the source urls are
  still valid, just extra network cost. Would need a `media_paths` field on the item
  hash to avoid it, not done since crash-mid-item is rare.

## Recent changes

- **Model roster update** (2026-07-19): `config/models.yaml` `model_priority` gained
  `gemini-3.1-pro-preview` (inserted after `gemini-2.5-flash`) and dropped
  `gemini-2.0-flash`, `gemini-2.0-flash-lite`, `gemini-1.5-flash`, `gemini-1.5-pro`.
  Fixed `gemini-flash-latest` alias, which pointed at the now-removed
  `gemini-2.0-flash` — repointed to `gemini-2.5-flash`. Added `gemini-3.1-pro` shorthand
  alias → `gemini-3.1-pro-preview`, matching the existing `gemini-3.1`/`gemini-3.1-flash`
  alias pattern. New `quota_table` row for `gemini-3.1-pro-preview` is a **placeholder**
  (`rpm: 2, tpm: 32000, rpd: 50`, mirroring the removed `gemini-1.5-pro`'s tighter
  pro-tier shape) — not verified against Google's real quota page for this model, correct
  before relying on it for rate-limit enforcement. No hot-reload — restart the service to
  pick this up.
- **`model` now actually pins the request** (2026-07-19): previously, a client-supplied
  `model` was resolved (alias lookup) but never passed into `AsyncAPIKeyPool.acquire_key()`
  — the pool always iterated the full `model_priority` list internally and returned
  whatever model it found a key for, silently overriding the caller's choice. Fixed by
  adding a `model` param to `acquire_key()`/`_get_candidate_models()`: when the caller
  pins a model, candidate selection is restricted to that one model with no cross-model
  fallback (all keys cooled on that model → request fails instead of substituting a
  different one); omitting `model` is unchanged (full priority-list fallback). Also added
  `UnknownModelHTTPError` (`422 unknown_model`) so a typo'd/nonexistent model fails fast
  instead of reaching the SDK. Same fix applies to batch job items (`JobItemSpec.model`),
  since jobs reuse `run_generate`. Also bumped `redis.from_url()`'s default client-side
  `max_connections` (redis-py defaults to 100) to a configurable `REDIS_MAX_CONNECTIONS`
  (default 200) — this service's per-`acquire_key()` fan-out (a `leased:*` gather across
  every key, per candidate model, times `JOBS_WORKER_CONCURRENCY` parallel workers) was
  exceeding 100 in-flight connections under load and raising `MaxConnectionsError`.
- **Result cache / re-fetch endpoint** (2026-07-17): every successful `POST /v1/generate`
  (all three variants) now stores the full `GenerateResponse` JSON in Redis for
  `RESULT_CACHE_TTL_SECONDS` (default 1h) under `result:{request_id}`. A new
  `GET /v1/generate/result/{request_id}` endpoint lets clients re-fetch the result if
  the original HTTP response was lost in transit (network drop, client crash, etc.)
  without re-running generation. The cache write is best-effort — a Redis failure is
  logged and swallowed so it never turns a successful generation into a 500. Set
  `RESULT_CACHE_TTL_SECONDS=0` to disable. 89 tests total.
- **`GET /v1/stats` + `GET /v1/capacity`** (2026-07-16): two new observability
  endpoints. `/v1/capacity` gives a caller one call to decide whether to submit more
  work — key-pool headroom, global in-flight usage (new
  `AsyncAPIKeyPool.current_in_flight()`, reads the `inflight:tokens` ZSET), jobs-queue
  depth, rolled into `accepting_more_work` + `reasons`. `/v1/stats` answers "how many
  calls, how many failed, how many 429s, which model is slow" over a trailing N-day
  window (new `app/tracking/stats.py`, day-scoped Redis hashes, 90-day TTL) — each
  counter is written from exactly one existing choke point (`CallTracker.record_call`,
  `AsyncAPIKeyPool.report_failure`, the exception handlers in `app/errors.py`,
  `JobStore.finish_item`), not duplicated across call sites.
- **Fixed unbounded `upload_media()` hang** (2026-07-16): a production batch job stalled
  13/27 items simultaneously — including plain-text items with no media at all — for
  15+ minutes with zero Gemini calls in the logs. Root cause: `GeminiProvider
  .upload_media()` wrapped its blocking SDK call in `asyncio.to_thread` but, unlike
  `generate()`, never bounded it with `asyncio.wait_for` — a stalled upload could hang
  forever. Since `asyncio.to_thread` shares one process-wide default executor thread
  pool, enough hung uploads eventually starved *every* other job, including unrelated
  text-only ones waiting for a free thread for their own `to_thread` call. Fixed by
  wrapping the whole upload (transfer + existing 600s ACTIVE-state poll) in
  `asyncio.wait_for(timeout=780s)`, matching `generate()`'s existing pattern — a hung
  upload now raises `TimeoutError` and rotates to the next key/attempt instead of
  parking a worker (and eventually the whole pool) forever.
- **`media_urls` support in batch jobs** (2026-07-15): `JobItemSpec` gained a
  `media_urls: list[str]` field, mutually exclusive with `has_media`. Unlike
  `has_media` items (which sit in `awaiting_media` until a separate multipart upload
  call), `media_urls` items are queued immediately at submit — `JobWorkerPool`
  downloads the urls itself (concurrently, same `app/media_fetch.py` used by the sync
  endpoint) right before calling generate, into `UPLOADS_DIR/jobs/{batch_id}/{item_id}/`
  (cleaned up on terminal success/failure like existing media items). This is the actual
  fix for large-media-over-the-network in the case that matters most — fan-out batches
  — since it removes the per-item upload round-trip entirely. A failed download fails
  that item immediately (`error_code: media_fetch_failed`) rather than burning the
  normal generate-failure retry budget, since a bad url isn't a transient error.
- **`POST /v1/generate/media/url`** (2026-07-15): clients can now send JSON
  `media_urls` (e.g. CDN links) instead of uploading raw files — the gateway
  downloads them server-side, concurrently (streamed, size/timeout-bounded per-url via
  `MEDIA_URL_MAX_BYTES`/`MEDIA_URL_DOWNLOAD_TIMEOUT_SECONDS`, count-capped via
  `MEDIA_URL_MAX_COUNT`) into the same per-request upload dir `/v1/generate/media`
  already uses, then runs the same generate pipeline. New endpoint rather than a mode
  switch on the existing multipart endpoint, to avoid entangling two very different
  request shapes (`multipart/form-data` vs JSON body) in one handler. Required
  generalizing `GenerateContext.media_path` (singular) into `media_paths: list[str]`
  plus per-path upload-ref tracking in `run_generate`/`GeminiProvider.generate`, so a
  single call can mix File-API-uploaded and inline media — existing single-file callers
  (`/v1/generate/media`, jobs worker) are unaffected, they just populate a one-item
  list under the hood. One failed url fails the whole request
  (`422 media_fetch_failed`), not a pool/quota error. This generalization is what made
  the batch-jobs `media_urls` support above straightforward to add on top. 86 tests
  total.
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
