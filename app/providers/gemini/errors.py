from __future__ import annotations

import re

from app.models.enums import FailureReason
from app.providers.base import FailureClassification

_AUTH_MARKERS = (
    "permission_denied",
    "unrestricted key",
    "unrestricted keys",
    "api key not valid",
    "api_key_invalid",
)
_QUOTA_MARKERS = ("daily", "project quota", "daily quota")
_NOT_FOUND_RE = re.compile(r"\b404\b|not_found|not found")
_RATE_LIMIT_RE = re.compile(
    r"\b429\b|rate limit|quota|resource has been exhausted|too many requests|exhausted"
)
_HIGH_DEMAND_RE = re.compile(r"\b503\b|unavailable|overloaded|high demand|temporary")


def classify_gemini_error(error: str) -> FailureClassification:
    """Ports the error-string classification that lived in both
    api_key_pool.report_failure() and gemini_util.py's except block in the source repo,
    merged into the single seam a Provider is responsible for.
    """
    message = str(error).lower()

    if any(marker in message for marker in _AUTH_MARKERS):
        return FailureClassification(reason=FailureReason.AUTH_DEAD, scope="key", retryable=True)

    if any(marker in message for marker in _QUOTA_MARKERS):
        return FailureClassification(reason=FailureReason.QUOTA_EXHAUSTED, scope="key_model", retryable=True)

    if _NOT_FOUND_RE.search(message):
        return FailureClassification(reason=FailureReason.NOT_FOUND, scope="model", retryable=True)

    if _RATE_LIMIT_RE.search(message):
        return FailureClassification(reason=FailureReason.RATE_LIMIT, scope="key_model", retryable=True)

    if _HIGH_DEMAND_RE.search(message):
        return FailureClassification(reason=FailureReason.HIGH_DEMAND, scope="key_model", retryable=True)

    return FailureClassification(reason=FailureReason.UNKNOWN, scope="key_model", retryable=True)
