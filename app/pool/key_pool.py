from __future__ import annotations

import asyncio
import math
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import redis.asyncio as redis

from app.config import Settings
from app.models.enums import FailureReason, KeyStatus
from app.pool.backoff import compute_backoff_seconds
from app.pool.redis_keys import RedisKeys, key_id, key_suffix
from app.providers.base import FailureClassification
from app.tracking import stats

LONG_TERM_THRESHOLD_SECONDS = 3600.0
_LUA_DIR = Path(__file__).parent / "lua"


class PoolExhaustedError(Exception):
    """No candidate model has any available key within the wait budget."""


class AllKeysDeadError(Exception):
    """Every configured key is dead_auth/dead_quota, or no keys are configured."""


class AsyncAPIKeyPool:
    """Redis-backed multi-key pool. Replaces the original in-process class-level
    dicts + tmp/pool/*.json with shared Redis state so cooldowns, in-flight caps,
    and failure streaks are correct across multiple worker processes/instances.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        api_keys_string: str,
        model_priority: list[str],
        settings: Settings,
        rpm: Optional[int] = None,
    ):
        self.redis = redis_client
        self.settings = settings
        self.rk = RedisKeys(settings.redis_key_prefix)
        self.model_priority = model_priority
        self.rpm = rpm or settings.default_rpm
        self.api_keys: list[str] = [k.strip() for k in api_keys_string.split(",") if k.strip()]
        self._key_by_id = {key_id(k): k for k in self.api_keys}
        self._active_tokens: dict[str, tuple[str, str]] = {}  # kid -> (inflight_token, model)
        self._tokens_lock = asyncio.Lock()

        self._acquire_inflight_sha: Optional[str] = None
        self._reserve_rpm_sha: Optional[str] = None

    def size(self) -> int:
        return len(self.api_keys)

    async def _load_scripts(self) -> None:
        if self._acquire_inflight_sha is None:
            script = (_LUA_DIR / "acquire_inflight.lua").read_text()
            self._acquire_inflight_sha = await self.redis.script_load(script)
        if self._reserve_rpm_sha is None:
            script = (_LUA_DIR / "reserve_rpm.lua").read_text()
            self._reserve_rpm_sha = await self.redis.script_load(script)

    def _clamped(self, seconds: float) -> float:
        return min(seconds, self.settings.clamped_dead_cooldown_seconds)

    # ---------- cooldown reads ----------

    async def _read_cooldown(self, redis_key: str, now: float) -> float:
        raw = await self.redis.get(redis_key)
        if raw is None:
            return 0.0
        try:
            until = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return max(until - now, 0.0)

    async def _read_failure_meta(self, kid: str, model: str = "") -> dict[str, Any]:
        return await self.redis.hgetall(self.rk.failure_meta(kid, model))

    async def get_failure_meta(self, api_key: str, model: str = "") -> dict[str, Any]:
        return await self._read_failure_meta(key_id(api_key), model)

    async def get_effective_failure_meta(self, api_key: str, model: str) -> dict[str, Any]:
        """Per-model failure metadata (rate_limit/high_demand/quota_exhausted) if present,
        else the global one (auth_dead is recorded globally via mark_cooldown, not
        per-model) — so dead_auth entries surface their reason instead of an empty dict.
        """
        kid = key_id(api_key)
        meta = await self._read_failure_meta(kid, model)
        if meta:
            return meta
        return await self._read_failure_meta(kid)

    async def classify_key_status(
        self,
        api_key: str,
        model: str,
        now: float,
        tracker: Any = None,
        service: str = "gemini",
        method: str = "generate",
    ) -> tuple[str, float]:
        kid = key_id(api_key)

        if await self.redis.exists(self.rk.leased(kid)):
            return KeyStatus.IN_USE.value, 0.0

        global_remaining = await self._read_cooldown(self.rk.cooldown_key(kid), now)
        if global_remaining > 0:
            # Classify by stored reason, not remaining duration: duration decays every
            # tick after mark_cooldown() while the long-term threshold is a fixed 3600s,
            # so a duration-only comparison races the clock and can misclassify a fresh
            # auth-dead key as merely short-cooldown a few milliseconds after it's set.
            global_meta = await self._read_failure_meta(kid)
            if global_meta.get("reason") == FailureReason.AUTH_DEAD.value or global_remaining >= LONG_TERM_THRESHOLD_SECONDS:
                return KeyStatus.DEAD_AUTH.value, global_remaining
            return KeyStatus.SHORT_COOLDOWN.value, global_remaining

        keymodel_remaining = await self._read_cooldown(self.rk.cooldown_keymodel(kid, model), now)
        if keymodel_remaining > 0:
            meta = await self._read_failure_meta(kid, model)
            reason = meta.get("reason", "")
            if keymodel_remaining >= LONG_TERM_THRESHOLD_SECONDS or reason in (
                FailureReason.AUTH_DEAD.value,
                FailureReason.QUOTA_EXHAUSTED.value,
            ):
                if reason == FailureReason.AUTH_DEAD.value:
                    return KeyStatus.DEAD_AUTH.value, keymodel_remaining
                return KeyStatus.DEAD_QUOTA.value, keymodel_remaining
            if reason == FailureReason.HIGH_DEMAND.value:
                return KeyStatus.HIGH_DEMAND.value, keymodel_remaining
            return KeyStatus.RATE_LIMITED.value, keymodel_remaining

        if tracker is not None:
            can_call, reason = await tracker.can_make_call(service, method, model, key_suffix(api_key))
            if not can_call:
                wait = await tracker.get_retry_after_seconds(service, method, model, key_suffix(api_key))
                if wait >= LONG_TERM_THRESHOLD_SECONDS or "rpd" in reason.lower() or "daily" in reason.lower():
                    return KeyStatus.DEAD_QUOTA.value, wait
                return KeyStatus.TRACKER_LIMITED.value, wait

        return KeyStatus.AVAILABLE.value, 0.0

    # ---------- model candidate selection ----------

    async def _get_candidate_models(self, now: float) -> list[str]:
        candidates = []
        for model in self.model_priority:
            remaining = await self._read_cooldown(self.rk.cooldown_model(model), now)
            if remaining <= 0:
                candidates.append(model)
        return candidates

    async def get_available_model(self) -> Optional[str]:
        candidates = await self._get_candidate_models(time.time())
        return candidates[0] if candidates else None

    # ---------- status / observability ----------

    async def get_pool_status(
        self,
        model: Optional[str] = None,
        tracker: Any = None,
        service: str = "gemini",
        method: str = "generate",
    ) -> dict[str, Any]:
        now = time.time()
        model_to_use = model or await self.get_available_model() or (self.model_priority[0] if self.model_priority else "")

        buckets: dict[str, list[dict[str, Any]]] = {status.value: [] for status in KeyStatus}

        for api_key in self.api_keys:
            status, retry_in = await self.classify_key_status(api_key, model_to_use, now, tracker, service, method)
            entry: dict[str, Any] = {
                "suffix": key_suffix(api_key),
                "retry_in_seconds": round(max(retry_in, 0.0), 1),
            }
            meta = await self.get_effective_failure_meta(api_key, model_to_use)
            if meta.get("reason"):
                entry["last_reason"] = meta["reason"]
            if meta.get("streak"):
                entry["failure_streak"] = int(meta["streak"])
            buckets[status].append(entry)

        total = len(self.api_keys)
        short_blocked = (
            len(buckets[KeyStatus.RATE_LIMITED.value])
            + len(buckets[KeyStatus.HIGH_DEMAND.value])
            + len(buckets[KeyStatus.SHORT_COOLDOWN.value])
            + len(buckets[KeyStatus.TRACKER_LIMITED.value])
        )
        dead_count = len(buckets[KeyStatus.DEAD_AUTH.value]) + len(buckets[KeyStatus.DEAD_QUOTA.value])

        retry_times = [
            entry["retry_in_seconds"]
            for bucket_name in (
                KeyStatus.RATE_LIMITED.value,
                KeyStatus.HIGH_DEMAND.value,
                KeyStatus.SHORT_COOLDOWN.value,
                KeyStatus.TRACKER_LIMITED.value,
                KeyStatus.DEAD_AUTH.value,
                KeyStatus.DEAD_QUOTA.value,
            )
            for entry in buckets[bucket_name]
            if entry["retry_in_seconds"] > 0
        ]

        return {
            "model": model_to_use,
            "total_keys": total,
            "available": len(buckets[KeyStatus.AVAILABLE.value]),
            "in_use": len(buckets[KeyStatus.IN_USE.value]),
            "short_cooldown": short_blocked,
            "permanently_blocked": dead_count,
            "in_flight_limit": self.settings.max_in_flight,
            "next_retry_seconds": round(min(retry_times), 1) if retry_times else None,
            "keys": buckets,
        }

    # ---------- acquisition ----------

    async def acquire_key(
        self,
        tracker: Any = None,
        service: str = "gemini",
        method: str = "generate",
        max_wait_seconds: float = 120.0,
    ) -> tuple[Optional[str], Optional[str]]:
        await self._load_scripts()
        if not self.api_keys:
            return None, None

        deadline = time.time() + max_wait_seconds

        while time.time() < deadline:
            now = time.time()
            candidate_models = await self._get_candidate_models(now)
            if not candidate_models:
                return None, None

            selected_key = None
            selected_model = None
            best_wait = None

            for model_to_use in candidate_models:
                leased_flags = await asyncio.gather(
                    *(self.redis.exists(self.rk.leased(key_id(k))) for k in self.api_keys)
                )
                unleased_keys = [k for k, leased in zip(self.api_keys, leased_flags) if not leased]

                if not unleased_keys:
                    best_wait = 1.0 if best_wait is None else min(best_wait, 1.0)
                    continue

                available_keys = []
                model_retry_times = []
                long_term_count = 0

                for key in unleased_keys:
                    status, retry_in = await self.classify_key_status(key, model_to_use, now, tracker, service, method)
                    if status == KeyStatus.AVAILABLE.value:
                        rpm_count = await self.redis.zcount(
                            self.rk.usage_rpm(key_id(key), model_to_use), now - 60, "+inf"
                        )
                        if rpm_count < self.rpm:
                            available_keys.append(key)
                        else:
                            model_retry_times.append(60.0)
                    elif status in (KeyStatus.DEAD_AUTH.value, KeyStatus.DEAD_QUOTA.value):
                        long_term_count += 1
                    elif retry_in > 0:
                        model_retry_times.append(retry_in)

                if available_keys:
                    usage_counts = await asyncio.gather(
                        *(self.redis.get(self.rk.usage_key(key_id(k))) for k in available_keys)
                    )
                    counts = [int(c) if c else 0 for c in usage_counts]
                    selected_key = min(zip(available_keys, counts), key=lambda pair: pair[1])[0]
                    selected_model = model_to_use
                    break

                if long_term_count >= len(unleased_keys):
                    already_blacklisted = await self.redis.exists(self.rk.cooldown_model(model_to_use))
                    if not already_blacklisted:
                        ttl = self._clamped(self.settings.dead_cooldown_seconds)
                        await self.redis.set(
                            self.rk.cooldown_model(model_to_use), now + ttl, ex=math.ceil(ttl)
                        )
                    continue

                if model_retry_times:
                    model_wait = min(model_retry_times)
                    if best_wait is None or model_wait < best_wait:
                        best_wait = model_wait

            if selected_key:
                kid = key_id(selected_key)
                token = str(uuid.uuid4())

                acquired = await self._eval_acquire_inflight(now, token)

                if not acquired:
                    await asyncio.sleep(0.05)
                    continue

                leased = await self.redis.set(self.rk.leased(kid), "1", nx=True, px=self.settings.lease_ttl_ms)
                if not leased:
                    await self._release_inflight(token)
                    continue

                reserved = await self._eval_reserve_rpm(kid, selected_model, now, token)
                if not reserved:
                    await self.redis.delete(self.rk.leased(kid))
                    await self._release_inflight(token)
                    continue

                await self.redis.incr(self.rk.usage_key(kid))

                async with self._tokens_lock:
                    self._active_tokens[kid] = (token, selected_model)

                return selected_key, selected_model

            wait_time = best_wait if best_wait is not None else 5.0
            await asyncio.sleep(min(wait_time, 5.0))

        return None, None

    async def _eval_acquire_inflight(self, now: float, token: str) -> bool:
        result = await self.redis.evalsha(
            self._acquire_inflight_sha,
            1,
            self.rk.inflight_tokens(),
            now,
            self.settings.max_in_flight,
            self.settings.inflight_slot_ttl_seconds,
            token,
        )
        return bool(int(result))

    async def _eval_reserve_rpm(self, kid: str, model: str, now: float, token: str) -> bool:
        result = await self.redis.evalsha(
            self._reserve_rpm_sha,
            1,
            self.rk.usage_rpm(kid, model),
            now,
            self.rpm,
            token,
        )
        acquired = int(result[0]) if isinstance(result, (list, tuple)) else int(result)
        return bool(acquired)

    async def _release_inflight(self, token: str) -> None:
        await self.redis.zrem(self.rk.inflight_tokens(), token)

    async def current_in_flight(self) -> int:
        """Live count of in-flight generate calls across the whole pool (same ZSET
        acquire_inflight.lua reads/writes) — prunes stale slots first so a crashed
        request doesn't count against capacity forever. Used by GET /v1/capacity."""
        now = time.time()
        await self.redis.zremrangebyscore(
            self.rk.inflight_tokens(), "-inf", now - self.settings.inflight_slot_ttl_seconds
        )
        return await self.redis.zcard(self.rk.inflight_tokens())

    async def release_key(self, api_key: str) -> None:
        kid = key_id(api_key)
        async with self._tokens_lock:
            entry = self._active_tokens.pop(kid, None)
        await self.redis.delete(self.rk.leased(kid))
        if entry:
            token, _model = entry
            await self._release_inflight(token)

    # ---------- failure / success reporting ----------

    async def mark_cooldown(self, api_key: str, seconds: float, reason: str) -> None:
        kid = key_id(api_key)
        ttl = self._clamped(seconds)
        now = time.time()
        await self.redis.set(self.rk.cooldown_key(kid), now + ttl, ex=max(1, math.ceil(ttl)))
        await self.redis.hset(
            self.rk.failure_meta(kid),
            mapping={"reason": reason, "streak": 1, "cooldown_seconds": round(ttl, 1), "updated_at": now},
        )
        await self.redis.expire(self.rk.failure_meta(kid), max(1, math.ceil(ttl)))

    async def record_success(self, api_key: str, model: str) -> None:
        kid = key_id(api_key)
        await self.redis.delete(self.rk.failure_meta(kid, model))
        await self.redis.delete(self.rk.cooldown_keymodel(kid, model))

    async def _get_failure_streak(self, kid: str, model: str, failure_type: str) -> int:
        meta = await self._read_failure_meta(kid, model)
        if meta.get("reason") == failure_type:
            try:
                return int(meta.get("streak", 0)) + 1
            except (TypeError, ValueError):
                return 1
        return 1

    async def report_failure(
        self,
        api_key: str,
        model: str,
        classification: FailureClassification,
        tracker: Any = None,
        service: str = "gemini",
    ) -> None:
        kid = key_id(api_key)
        now = time.time()

        await stats.record_failure_reason(self.redis, self.rk, service, classification.reason.value)

        if classification.reason == FailureReason.STALE_MEDIA:
            # Cross-key/expired File API ref, not a key health problem — never cool.
            return

        if classification.reason == FailureReason.AUTH_DEAD:
            await self.mark_cooldown(api_key, self.settings.dead_cooldown_seconds, FailureReason.AUTH_DEAD.value)
            return

        if classification.reason == FailureReason.QUOTA_EXHAUSTED:
            ttl = self._clamped(self.settings.dead_cooldown_seconds)
            await self.redis.set(self.rk.cooldown_keymodel(kid, model), now + ttl, ex=max(1, math.ceil(ttl)))
            streak = await self._get_failure_streak(kid, model, FailureReason.QUOTA_EXHAUSTED.value)
            await self._set_failure_meta(kid, model, FailureReason.QUOTA_EXHAUSTED.value, ttl, streak)

            all_exhausted = True
            for key in self.api_keys:
                remaining = await self._read_cooldown(self.rk.cooldown_keymodel(key_id(key), model), now)
                if remaining <= 0:
                    all_exhausted = False
                    break
            if all_exhausted:
                model_ttl = self._clamped(self.settings.dead_cooldown_seconds)
                await self.redis.set(self.rk.cooldown_model(model), now + model_ttl, ex=max(1, math.ceil(model_ttl)))
            return

        if classification.reason == FailureReason.NOT_FOUND:
            ttl = self._clamped(self.settings.dead_cooldown_seconds)
            await self.redis.set(self.rk.cooldown_model(model), now + ttl, ex=max(1, math.ceil(ttl)))
            return

        if classification.reason in (FailureReason.RATE_LIMIT, FailureReason.HIGH_DEMAND):
            streak = await self._get_failure_streak(kid, model, classification.reason.value)
            backoff = (
                classification.cooldown_seconds
                if classification.cooldown_seconds is not None
                else compute_backoff_seconds(classification.reason.value, streak)
            )
            backoff = self._clamped(backoff)
            await self.redis.set(
                self.rk.cooldown_keymodel(kid, model), now + backoff, ex=max(1, math.ceil(backoff))
            )
            await self._set_failure_meta(kid, model, classification.reason.value, backoff, streak)
            await self._maybe_trip_model_breaker(model, now)
            return

        # FailureReason.UNKNOWN deliberately gets no cooldown here: the jobs worker
        # (app/jobs/worker.py) treats a bare exception from run_generate as an
        # item-level failure to retry a bounded number of times and then report as
        # "generate_failed" — cooling the key/model here would instead route retries
        # through the pool's capacity-exhaustion path (PoolExhaustedHTTPError), which
        # has its own separate, much larger retry budget. For a genuinely
        # request-shaped failure (bad payload, not a key/model health problem) that
        # just delays reporting the real error without ever fixing it.

    async def _maybe_trip_model_breaker(self, model: str, now: float) -> None:
        """Model-wide circuit breaker: fires on failure *velocity* (N RATE_LIMIT/
        HIGH_DEMAND hits across ANY key within a short window), not on "every key
        individually cooled" like the QUOTA_EXHAUSTED path below. With a large key
        pool, the latter basically never happens for a provider-side throttle — each
        429 only cools the one key that hit it, so acquire_key() keeps finding a
        different "available" key on the same saturated model instead of falling back
        down model_priority. This trips fast and cools briefly (self-healing), so a
        model that's genuinely fine again in 20s isn't penalized for an hour.
        """
        window = self.settings.model_circuit_breaker_window_seconds
        zkey = self.rk.model_failure_events(model)
        await self.redis.zadd(zkey, {str(uuid.uuid4()): now})
        await self.redis.zremrangebyscore(zkey, 0, now - window)
        await self.redis.expire(zkey, max(1, math.ceil(window)))
        count = await self.redis.zcard(zkey)
        if count < self.settings.model_circuit_breaker_threshold:
            return
        if await self.redis.exists(self.rk.cooldown_model(model)):
            return
        ttl = self.settings.model_circuit_breaker_cooldown_seconds
        await self.redis.set(self.rk.cooldown_model(model), now + ttl, ex=max(1, math.ceil(ttl)))

    async def _set_failure_meta(self, kid: str, model: str, reason: str, cooldown_seconds: float, streak: int) -> None:
        now = time.time()
        await self.redis.hset(
            self.rk.failure_meta(kid, model),
            mapping={
                "reason": reason,
                "streak": streak,
                "cooldown_seconds": round(cooldown_seconds, 1),
                "updated_at": now,
            },
        )
        await self.redis.expire(self.rk.failure_meta(kid, model), max(1, math.ceil(cooldown_seconds)))
