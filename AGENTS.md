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

curl localhost:8080/v1/pool/status
curl localhost:8080/v1/keys
curl localhost:8080/v1/usage/summary
curl localhost:8080/health/ready
```

## Project Structure
```
gemini/
├── app/
│   ├── main.py                # FastAPI app, lifespan (redis/provider/pool wiring)
│   ├── config.py               # env-var settings, hard 1h cooldown clamp
│   ├── api/v1/                 # generate, pool, keys, usage, health routers
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
- **429 must carry retry guidance**: `retry_after_seconds` in the body and a
  `Retry-After` HTTP header, every time.
- **Keep `acquire_key()` bounded** (`ACQUIRE_KEY_MAX_WAIT_SECONDS`) — this is an HTTP
  service, not a batch job; don't hold connections open for long backoffs.
- **New provider checklist**: implement `Provider` ABC (`app/providers/base.py`),
  register in `app/providers/registry.py`'s `_BUILDERS`, add a section to
  `config/models.yaml`, wire its key env var in `app/main.py`. Nothing else changes.
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
