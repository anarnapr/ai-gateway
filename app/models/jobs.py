from __future__ import annotations

from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, model_validator

from app.models.requests import InlinePart


class ItemStatus(str, Enum):
    AWAITING_MEDIA = "awaiting_media"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class BatchStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"


class JobItemSpec(BaseModel):
    """One unit of work in a batch. Mirrors GenerateRequest fields; batch-level
    provider/model act as defaults an item may override."""

    item_id: Optional[str] = None  # client-chosen id (e.g. post pk); server uuid if absent
    prompt: Optional[str] = None
    parts: Optional[list[Union[str, InlinePart]]] = None
    model: Optional[str] = None
    timeout_seconds: Optional[float] = None
    metadata: Optional[dict] = None  # echoed back verbatim in results
    has_media: bool = False  # True -> item waits in awaiting_media until media uploaded
    media_urls: Optional[list[str]] = None  # CDN urls -> worker downloads before generating

    @model_validator(mode="after")
    def _require_prompt_or_parts(self) -> "JobItemSpec":
        if not self.prompt and not self.parts:
            raise ValueError("Each item requires 'prompt' or 'parts'.")
        return self

    @model_validator(mode="after")
    def _has_media_and_media_urls_are_exclusive(self) -> "JobItemSpec":
        if self.has_media and self.media_urls:
            raise ValueError("An item can't set both 'has_media' (multipart upload) and 'media_urls'.")
        if self.media_urls is not None and not self.media_urls:
            raise ValueError("'media_urls' must not be an empty list.")
        return self


class JobSubmitRequest(BaseModel):
    provider: str = "gemini"
    model: Optional[str] = None  # batch default; item-level model overrides
    items: list[JobItemSpec]

    @model_validator(mode="after")
    def _validate_items(self) -> "JobSubmitRequest":
        if not self.items:
            raise ValueError("'items' must not be empty.")
        ids = [i.item_id for i in self.items if i.item_id]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate item_id values in batch.")
        return self


class JobItemBrief(BaseModel):
    item_id: str
    status: ItemStatus


class JobSubmitResponse(BaseModel):
    batch_id: str
    total: int
    items: list[JobItemBrief]


class JobItemResult(BaseModel):
    item_id: str
    status: ItemStatus
    text: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None  # generate_failed | pool_exhausted | all_keys_dead
    attempts: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    api_key_suffix: Optional[str] = None
    latency_ms: Optional[float] = None
    metadata: Optional[dict] = None


class BatchStatusResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    total: int
    counts: dict[str, int]
    created_at: float
    finished_at: Optional[float] = None
    items: list[JobItemResult]


class BatchSummary(BaseModel):
    batch_id: str
    status: BatchStatus
    total: int
    counts: dict[str, int]
    created_at: float
    finished_at: Optional[float] = None
