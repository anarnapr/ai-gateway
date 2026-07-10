from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models.enums import FailureReason
from app.providers.base import GenerateContext
from app.providers.gemini.errors import classify_gemini_error
from app.providers.gemini.provider import GeminiProvider


@pytest.mark.parametrize(
    "message,expected_reason",
    [
        ("400 API key not valid. Please pass a valid API key.", FailureReason.AUTH_DEAD),
        ("PERMISSION_DENIED: caller does not have permission", FailureReason.AUTH_DEAD),
        ("daily quota exceeded for this project", FailureReason.QUOTA_EXHAUSTED),
        ("404 models/foo is not found for API version v1beta", FailureReason.NOT_FOUND),
        ("429 Resource has been exhausted (e.g. check quota).", FailureReason.RATE_LIMIT),
        ("Too Many Requests", FailureReason.RATE_LIMIT),
        ("503 The model is overloaded. Please try again later.", FailureReason.HIGH_DEMAND),
        ("some totally unrelated error", FailureReason.UNKNOWN),
    ],
)
def test_classify_gemini_error(message, expected_reason):
    result = classify_gemini_error(message)
    assert result.reason == expected_reason


@pytest.fixture
def provider() -> GeminiProvider:
    return GeminiProvider(
        model_priority=["gemini-2.5-flash"],
        model_aliases={"gemini-3.1": "gemini-3.1-flash-preview"},
        quota_table={"gemini-2.5-flash": {"rpm": 15, "tpm": 1000000, "rpd": 500}},
    )


def test_resolve_model_uses_alias(provider):
    assert provider.resolve_model("gemini-3.1") == "gemini-3.1-flash-preview"
    assert provider.resolve_model("gemini-2.5-flash") == "gemini-2.5-flash"
    assert provider.resolve_model(None) == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_generate_success_parses_usage_metadata(provider, monkeypatch):
    mock_response = SimpleNamespace(
        text="Hello there  ",
        usage_metadata=SimpleNamespace(prompt_token_count=12, candidates_token_count=8, total_token_count=20),
    )
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response
    monkeypatch.setattr("app.providers.gemini.provider.genai.Client", lambda **kwargs: mock_client)

    ctx = GenerateContext(prompt_text="hi", model="gemini-2.5-flash", api_key="key-aaaa1111")
    result = await provider.generate(ctx)

    assert result.text == "Hello there"
    assert result.input_tokens == 12
    assert result.output_tokens == 8
    assert result.total_tokens == 20


@pytest.mark.asyncio
async def test_generate_propagates_sdk_error(provider, monkeypatch):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("429 Resource has been exhausted")
    monkeypatch.setattr("app.providers.gemini.provider.genai.Client", lambda **kwargs: mock_client)

    ctx = GenerateContext(prompt_text="hi", model="gemini-2.5-flash", api_key="key-aaaa1111")
    with pytest.raises(RuntimeError):
        await provider.generate(ctx)
