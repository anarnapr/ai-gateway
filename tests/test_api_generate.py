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


def test_generate_with_pinned_model_uses_that_model(api_client, monkeypatch):
    """Caller pins a model that isn't first in model_priority — the gateway must use
    exactly that model, not silently fall back to a different one."""
    seen_models = []

    async def fake_generate(self, ctx):
        seen_models.append(ctx.model)
        return ProviderResult(text="pinned", input_tokens=1, output_tokens=1, total_tokens=2)

    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    resp = api_client.post("/v1/generate", json={"prompt": "hi", "model": "gemini-2.0-flash"})
    assert resp.status_code == 200
    assert resp.json()["model"] == "gemini-2.0-flash"
    assert seen_models == ["gemini-2.0-flash"]


def test_generate_with_pinned_model_alias_resolves(api_client, monkeypatch):
    async def fake_generate(self, ctx):
        return ProviderResult(text="alias", input_tokens=1, output_tokens=1, total_tokens=2)

    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    resp = api_client.post("/v1/generate", json={"prompt": "hi", "model": "gemini-3.1"})
    assert resp.status_code == 200
    assert resp.json()["model"] == "gemini-3.1-flash-preview"


def test_generate_with_unknown_model_returns_422(api_client):
    resp = api_client.post("/v1/generate", json={"prompt": "hi", "model": "gemini-3.1-pro"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "unknown_model"


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


# ---------------------------------------------------------------------------
# Result cache (GET /v1/generate/result/{request_id})
# ---------------------------------------------------------------------------

def test_result_cache_populated_after_successful_generate(api_client, monkeypatch):
    """After a successful POST /v1/generate the response must be re-fetchable."""
    async def fake_generate(self, ctx):
        return ProviderResult(text="cached result", input_tokens=2, output_tokens=4, total_tokens=6)

    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    post_resp = api_client.post("/v1/generate", json={"prompt": "store me"})
    assert post_resp.status_code == 200
    request_id = post_resp.json()["request_id"]

    # Re-fetch via dedicated endpoint — must return identical payload.
    get_resp = api_client.get(f"/v1/generate/result/{request_id}")
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["request_id"] == request_id
    assert body["text"] == "cached result"
    assert body["input_tokens"] == 2
    assert body["output_tokens"] == 4
    assert body["total_tokens"] == 6


def test_result_cache_404_for_unknown_request_id(api_client):
    """A request_id that was never produced (or has expired) returns 404."""
    resp = api_client.get("/v1/generate/result/doesnotexist000")
    assert resp.status_code == 404
    assert "doesnotexist000" in resp.json()["detail"]


def test_result_cache_disabled_when_ttl_zero(api_client, monkeypatch):
    """When result_cache_ttl_seconds=0 no cache entry is written."""
    from app.config import get_settings

    # Patch the setting on the live settings object.
    settings = get_settings()
    monkeypatch.setattr(settings, "result_cache_ttl_seconds", 0)

    async def fake_generate(self, ctx):
        return ProviderResult(text="no cache", input_tokens=1, output_tokens=1, total_tokens=2)

    monkeypatch.setattr(GeminiProvider, "generate", fake_generate)

    post_resp = api_client.post("/v1/generate", json={"prompt": "no cache please"})
    assert post_resp.status_code == 200
    request_id = post_resp.json()["request_id"]

    # With TTL=0 the result must NOT have been cached.
    get_resp = api_client.get(f"/v1/generate/result/{request_id}")
    assert get_resp.status_code == 404

