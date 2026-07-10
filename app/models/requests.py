from __future__ import annotations

from typing import Optional, Union

from pydantic import BaseModel, Field, model_validator


class InlinePart(BaseModel):
    mime_type: str
    data: str  # base64-encoded


class GenerateRequest(BaseModel):
    provider: str = "gemini"
    prompt: Optional[str] = None
    parts: Optional[list[Union[str, InlinePart]]] = None
    model: Optional[str] = None
    max_retries: int = 10
    timeout_seconds: Optional[float] = None
    verbose: bool = False
    metadata: Optional[dict] = None

    @model_validator(mode="after")
    def _require_prompt_or_parts(self) -> "GenerateRequest":
        if not self.prompt and not self.parts:
            raise ValueError("Either 'prompt' or 'parts' must be provided.")
        return self

    def parts_as_dicts(self) -> Optional[list[Union[str, dict]]]:
        if self.parts is None:
            return None
        result: list[Union[str, dict]] = []
        for p in self.parts:
            if isinstance(p, InlinePart):
                result.append({"inline_data": {"mime_type": p.mime_type, "data": p.data}})
            else:
                result.append(p)
        return result
