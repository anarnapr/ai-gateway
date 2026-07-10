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
