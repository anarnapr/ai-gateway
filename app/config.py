from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


HARD_MAX_COOLDOWN_SECONDS = 3600.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_keys: str = ""

    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = "aiservice"

    max_in_flight: int = 4
    default_rpm: int = 15

    rate_limit_min_interval_seconds: float = 5.0
    rate_limit_rpm: int = 12

    dead_cooldown_seconds: float = 3600.0
    long_term_threshold_seconds: float = 3600.0

    models_config_path: str = "config/models.yaml"

    log_dir: str = "tmp/ai/logs"
    uploads_dir: str = "tmp/ai/uploads"
    log_full_payloads: bool = False

    lease_ttl_ms: int = 120_000
    inflight_slot_ttl_seconds: float = 300.0

    # Bounded internal wait inside acquire_key() before giving up and letting the HTTP
    # handler respond 429 with an accurate Retry-After. Deliberately short: unlike the
    # original CLI/batch context (which blocked with an unbounded while-True loop), an
    # HTTP request should fail fast with retry guidance rather than hold the connection
    # open for a potentially 30+ minute backoff.
    acquire_key_max_wait_seconds: float = 10.0

    @property
    def clamped_dead_cooldown_seconds(self) -> float:
        return min(self.dead_cooldown_seconds, HARD_MAX_COOLDOWN_SECONDS)


@lru_cache
def get_settings() -> Settings:
    return Settings()
