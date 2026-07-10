from __future__ import annotations

import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import redis.asyncio as redis

from app.pool.redis_keys import RedisKeys

RPD_TTL_SECONDS = 2 * 24 * 3600


class CallTracker:
    """Redis-backed port of APICallTracker: enforces the per-model rpm/tpm/rpd quota
    table and records call outcomes. Two concerns kept deliberately separate from the
    original (which conflated them in one full-file-rewrite-per-call function):
    quota bookkeeping lives here in fast Redis counters, durable audit logging is
    delegated to UsageLogger (JSONL, O(1) append).
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        redis_keys: RedisKeys,
        quota_table: dict[str, dict],
        model_aliases: dict[str, str],
        service: str = "gemini",
    ):
        self.redis = redis_client
        self.rk = redis_keys
        self.quota_table = quota_table
        self.model_aliases = model_aliases
        self.service = service

    def resolve_model(self, model: Optional[str]) -> Optional[str]:
        if not model:
            return model
        return self.model_aliases.get(model, model)

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d")

    async def _rpm_tpm_counts(self, model: str, suffix: str, now: float) -> tuple[int, int, float]:
        key = self.rk.tracker_rpm(model, suffix)
        await self.redis.zremrangebyscore(key, "-inf", now - 60)
        entries = await self.redis.zrangebyscore(key, now - 60, "+inf", withscores=True)
        rpm_count = len(entries)
        tpm_count = 0
        oldest_ts = None
        for member, score in entries:
            try:
                tokens = int(member.split(":")[-1])
            except (ValueError, IndexError):
                tokens = 0
            tpm_count += tokens
            if oldest_ts is None or score < oldest_ts:
                oldest_ts = score
        retry_after = max((oldest_ts + 60 - now), 0.0) if oldest_ts is not None else 0.0
        return rpm_count, tpm_count, retry_after

    async def _rpd_count(self, model: str, suffix: str) -> tuple[int, float]:
        raw = await self.redis.get(self.rk.tracker_rpd(model, suffix, self._today()))
        count = int(raw) if raw else 0
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        retry_after = max((tomorrow - now).total_seconds(), 0.0)
        return count, retry_after

    async def can_make_call(
        self, service: str, method: str, model: Optional[str], api_key_suffix: str = ""
    ) -> tuple[bool, str]:
        resolved = self.resolve_model(model)
        quotas = self.quota_table.get(resolved) if resolved else None
        if not quotas:
            return False, f"Unknown {service} model: {model}"

        now = time.time()
        rpm_count, tpm_count, _ = await self._rpm_tpm_counts(resolved, api_key_suffix, now)
        rpd_count, _ = await self._rpd_count(resolved, api_key_suffix)

        if quotas.get("rpm", -1) != -1 and rpm_count >= quotas["rpm"]:
            return False, f"Rate limit (RPM) exceeded for {service}/{method} (model: {model})."
        if quotas.get("tpm", -1) != -1 and tpm_count >= quotas["tpm"]:
            return False, f"Rate limit (TPM) exceeded for {service}/{method} (model: {model})."
        if quotas.get("rpd", -1) != -1 and rpd_count >= quotas["rpd"]:
            return False, f"Rate limit (RPD) exceeded for {service}/{method} (model: {model})."
        return True, "Call allowed."

    async def get_retry_after_seconds(
        self, service: str, method: str, model: Optional[str] = None, api_key_suffix: str = ""
    ) -> float:
        resolved = self.resolve_model(model)
        quotas = self.quota_table.get(resolved) if resolved else None
        if not quotas:
            return 0.0

        now = time.time()
        rpm_count, tpm_count, rpm_retry = await self._rpm_tpm_counts(resolved, api_key_suffix, now)
        rpd_count, rpd_retry = await self._rpd_count(resolved, api_key_suffix)

        wait = 0.0
        if quotas.get("rpm", -1) != -1 and rpm_count >= quotas["rpm"]:
            wait = max(wait, rpm_retry)
        if quotas.get("tpm", -1) != -1 and tpm_count >= quotas["tpm"]:
            wait = max(wait, rpm_retry)
        if quotas.get("rpd", -1) != -1 and rpd_count >= quotas["rpd"]:
            wait = max(wait, rpd_retry)
        return max(wait, 0.0)

    async def record_call(
        self,
        service: str,
        method: str,
        model: Optional[str] = None,
        api_key_suffix: Optional[str] = None,
        success: bool = True,
        response: Optional[Any] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
    ) -> None:
        resolved = self.resolve_model(model) if service == self.service else None
        suffix = api_key_suffix or "????"
        now = time.time()
        today = self._today()

        if resolved:
            token = str(uuid.uuid4())
            tokens_for_member = max(int(total_tokens or 0), 0)
            rpm_key = self.rk.tracker_rpm(resolved, suffix)
            await self.redis.zadd(rpm_key, {f"{token}:{tokens_for_member}": now})
            await self.redis.expire(rpm_key, 120)

            rpd_key = self.rk.tracker_rpd(resolved, suffix, today)
            await self.redis.incr(rpd_key)
            await self.redis.expire(rpd_key, RPD_TTL_SECONDS)

            if total_tokens:
                tok_key = self.rk.tracker_tokens_day(resolved, suffix, today)
                await self.redis.incrby(tok_key, max(int(total_tokens), 0))
                await self.redis.expire(tok_key, RPD_TTL_SECONDS)

            if not success:
                fail_key = self.rk.tracker_failures_day(resolved, suffix, today)
                await self.redis.incr(fail_key)
                await self.redis.expire(fail_key, RPD_TTL_SECONDS)

    async def get_quota_summary(self, known_suffixes: list[str]) -> dict[str, Any]:
        """Aggregate today's usage across all configured key suffixes, per model."""
        today = self._today()
        now = time.time()
        summary: dict[str, Any] = {}

        for model, limits in self.quota_table.items():
            day_total = 0
            minute_total = 0
            tokens_day_total = 0
            failures_day_total = 0

            for suffix in known_suffixes:
                rpd_raw = await self.redis.get(self.rk.tracker_rpd(model, suffix, today))
                day_total += int(rpd_raw) if rpd_raw else 0

                rpm_count, _, _ = await self._rpm_tpm_counts(model, suffix, now)
                minute_total += rpm_count

                tok_raw = await self.redis.get(self.rk.tracker_tokens_day(model, suffix, today))
                tokens_day_total += int(tok_raw) if tok_raw else 0

                fail_raw = await self.redis.get(self.rk.tracker_failures_day(model, suffix, today))
                failures_day_total += int(fail_raw) if fail_raw else 0

            if day_total == 0 and minute_total == 0 and tokens_day_total == 0:
                continue

            summary[model] = {
                "minute": minute_total,
                "day": day_total,
                "tokens_day": tokens_day_total,
                "failures_day": failures_day_total,
                "limits": limits,
                "percent_day": round((day_total / limits["rpd"] * 100), 1) if limits.get("rpd", 0) > 0 else 0,
            }

        return {self.service: summary}
