from app.providers.base import ProviderResult
from app.providers.gemini.provider import GeminiProvider


def test_generate_success(api_client, monkeypatch):
    async def fake_generate(self, ctx):
        return ProviderResult(text="hello world", input_tokens=5, output_tokens=3, total_tokens=8)

    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    resp = api_client.post("/v1/generate", json={"prompt": "hi there"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "hello world"
    assert body["input_tokens"] == 5
    assert body["output_tokens"] == 3
    assert body["total_tokens"] == 8
    assert body["attempts"] == 1


def test_generate_requires_prompt_or_parts(api_client):
    resp = api_client.post("/v1/generate", json={})
    assert resp.status_code == 422


def test_generate_retries_on_transient_error_then_succeeds(api_client, monkeypatch):
    calls = {"n": 0}

    async def flaky_generate(self, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("503 model overloaded, high demand")
        return ProviderResult(text="ok on retry", input_tokens=1, output_tokens=1, total_tokens=2)

    monkeypatch.setattr(GeminiProvider, "generate", flaky_generate)

    resp = api_client.post("/v1/generate", json={"prompt": "hi"})
    assert resp.status_code == 200
    assert resp.json()["text"] == "ok on retry"
    assert resp.json()["attempts"] == 2


def test_generate_default_timeout_applied_and_rotates_on_hang(api_client, monkeypatch):
    """Caller sends no timeout_seconds -> a hung generate must still raise (via the
    server default timeout) so the loop rotates to another key instead of blocking.
    """
    seen = {"timeouts": [], "n": 0}

    async def hanging_then_ok(self, ctx):
        seen["n"] += 1
        seen["timeouts"].append(ctx.timeout_seconds)
        if seen["n"] == 1:
            raise TimeoutError("Gemini request timed out after 90.0s.")
        return ProviderResult(text="ok after timeout", input_tokens=1, output_tokens=1, total_tokens=2)

    monkeypatch.setattr(GeminiProvider, "generate", hanging_then_ok)

    resp = api_client.post("/v1/generate", json={"prompt": "hi"})  # no timeout_seconds
    assert resp.status_code == 200
    assert resp.json()["text"] == "ok after timeout"
    assert resp.json()["attempts"] == 2
    # Server default timeout was injected (not None), so the SDK call is bounded.
    assert all(t and t > 0 for t in seen["timeouts"])
