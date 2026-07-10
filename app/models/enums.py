from enum import Enum


class KeyStatus(str, Enum):
    AVAILABLE = "available"
    IN_USE = "in_use"
    SHORT_COOLDOWN = "short_cooldown"
    DEAD_AUTH = "dead_auth"
    DEAD_QUOTA = "dead_quota"
    HIGH_DEMAND = "high_demand"
    RATE_LIMITED = "rate_limited"
    TRACKER_LIMITED = "tracker_limited"


class FailureReason(str, Enum):
    AUTH_DEAD = "auth_dead"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMIT = "rate_limit"
    HIGH_DEMAND = "high_demand"
    NOT_FOUND = "not_found"
    SHORT_COOLDOWN = "short_cooldown"
    STALE_MEDIA = "stale_media"
    UNKNOWN = "unknown"
