def test_pool_status_all_available_initially(api_client):
    resp = api_client.get("/v1/pool/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_keys"] == 2
    assert body["available"] == 2
    assert body["permanently_blocked"] == 0


def test_keys_endpoint_lists_each_configured_key(api_client):
    resp = api_client.get("/v1/keys")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    for entry in body:
        assert entry["status"] == "available"


def test_health_and_ready(api_client):
    resp = api_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    resp_ready = api_client.get("/health/ready")
    assert resp_ready.status_code == 200
    body = resp_ready.json()
    assert body["redis_ok"] is True
    assert body["keys_configured"] == 2
