async def _seed_all_keys_on_cooldown(seconds: float):
    from app.main import app as fastapi_app

    pool = fastapi_app.state.pools["gemini"]
    for api_key in pool.api_keys:
        await pool.mark_cooldown(api_key, seconds=seconds, reason="rate_limit")
    return pool


def test_generate_returns_429_with_retry_after_when_all_keys_backed_off(api_client):
    import asyncio

    asyncio.run(_seed_all_keys_on_cooldown(45.0))

    resp = api_client.post("/v1/generate", json={"prompt": "hi"})

    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    body = resp.json()
    assert body["error"] == "rate_limited"
    assert body["retry_after_seconds"] > 0


def test_generate_returns_503_when_all_keys_dead(api_client):
    import asyncio
    from app.main import app as fastapi_app
    from app.models.enums import FailureReason
    from app.providers.base import FailureClassification

    async def _kill_all_keys():
        pool = fastapi_app.state.pools["gemini"]
        for api_key in pool.api_keys:
            await pool.report_failure(
                api_key, pool.model_priority[0], FailureClassification(reason=FailureReason.AUTH_DEAD, scope="key")
            )

    asyncio.run(_kill_all_keys())

    resp = api_client.post("/v1/generate", json={"prompt": "hi"})

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "all_keys_dead"
    assert len(body["key_statuses"]) == 2
    assert all(k["status"] == "dead_auth" for k in body["key_statuses"])
