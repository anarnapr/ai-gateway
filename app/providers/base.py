from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union

from app.models.enums import FailureReason


@dataclass
class FailureClassification:
    """Structured result of a provider classifying a raw SDK/HTTP error string.
    Only providers do error-string matching — the pool applies state transitions
    generically from this struct, so a new provider only needs to implement
    classify_error() to plug into the same cooldown/backoff machinery.
    """

    reason: FailureReason
    scope: Literal["key", "key_model", "model"]
    cooldown_seconds: Optional[float] = None  # None => pool computes via exponential backoff
    retryable: bool = True


@dataclass
class GenerateContext:
    prompt_text: Optional[str] = None
    prompt_parts: Optional[list[Union[str, dict]]] = None
    media_path: Optional[str] = None
    model: str = ""
    api_key: str = ""
    timeout_seconds: Optional[float] = None
    verbose: bool = False
    request_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResult:
    text: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


@dataclass
class UploadedMediaRef:
    name: str
    handle: Any = None


class Provider(ABC):
    name: str

    @abstractmethod
    def model_priority(self) -> list[str]: ...

    @abstractmethod
    def model_aliases(self) -> dict[str, str]: ...

    @abstractmethod
    def quota_table(self) -> dict[str, dict]: ...

    @abstractmethod
    def classify_error(self, error: str) -> FailureClassification: ...

    @abstractmethod
    async def generate(self, ctx: GenerateContext) -> ProviderResult: ...

    async def upload_media(self, media_path: str, api_key: str) -> Optional[UploadedMediaRef]:
        return None

    async def requires_file_upload(self, media_path: str) -> bool:
        return False

    def resolve_model(self, model: Optional[str]) -> str:
        if not model:
            return self.model_priority()[0]
        return self.model_aliases().get(model, model)
