"""Proxy tests against a mock upstream -- no network, no API key."""

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_metering import proxy


def upstream(status=200, usage=None, headers=None, error_code=None):
    def handler(request: httpx.Request) -> httpx.Response:
        h = {
            "anthropic-ratelimit-requests-limit": "4000",
            "anthropic-ratelimit-requests-remaining": "3990",
            "anthropic-ratelimit-input-tokens-limit": "2000000",
            "anthropic-ratelimit-input-tokens-remaining": "1200000",
            "anthropic-ratelimit-output-tokens-limit": "400000",
            "anthropic-ratelimit-output-tokens-remaining": "399000",
            "anthropic-workspace-id": "wrkspc_test",
        }
        h.update(headers or {})
        if status == 200:
            body = {"id": "msg_1", "content": [], "usage": usage or {}}
        else:
            err = {"type": "rate_limit_error", "message": "limit"}
            if error_code:
                err["details"] = {"error_code": error_code}
            body = {"type": "error", "error": err}
        return httpx.Response(status, json=body, headers=h)

    return handler


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "TELEMETRY_PATH", str(tmp_path / "t.jsonl"))
    proxy.app.state.metering = proxy.Metering(
        policy=proxy.PolicyConfig(name="shadow", admission=True, cache_gate=True),
        rpm=4000, itpm=2_000_000, otpm=400_000,
    )
    return proxy.app.state.metering


def make(client_meter, handler):
    tc = TestClient(proxy.app)
    tc.__enter__()
    client_meter.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://upstream"
    )
    return tc


BODY = {
    "model": "claude-opus-5",
    "system": "s" * 8000,
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 512,
}


def test_records_the_discriminating_fields(client):
    tc = make(client, upstream(usage={
        "input_tokens": 40, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 2000, "output_tokens": 120,
    }))
    r = tc.post("/v1/messages", json=BODY)
    assert r.status_code == 200
    m = tc.get("/metrics").json()
    assert m["completed"] == 1
    assert m["cache_read_tokens"] == 2000
    assert m["cache_hit_rate"] > 0.9
    assert m["min_headroom"]["input_tokens"] == pytest.approx(0.6)
    assert m["min_headroom"]["requests"] == pytest.approx(0.9975)
    tc.__exit__(None, None, None)


def test_distinguishes_spend_cap_from_rate_limit(client):
    tc = make(client, upstream(status=429, error_code="enforced_spend_limit_reached"))
    assert tc.post("/v1/messages", json=BODY).status_code == 429
    m = tc.get("/metrics").json()
    assert m["throttled_429"] == 1 and m["spend_cap_429"] == 1
    tc.__exit__(None, None, None)


def test_shadow_mode_never_sheds(client, monkeypatch):
    """Even when the policy says DROP, shadow mode forwards and only records."""
    monkeypatch.setattr(proxy, "ENFORCE", False)
    # Force a DROP decision: drive the bucket deeply negative and stamp it to
    # now, so continuous refill cannot recover it during the request.
    import time as _t
    b = client.state.buckets["requests"]
    b.level, b.updated_at = -1e9, _t.monotonic()
    tc = make(client, upstream(usage={"input_tokens": 10, "output_tokens": 10}))
    r = tc.post("/v1/messages", json=BODY)
    assert r.status_code == 200, "shadow mode must not shed traffic"
    m = tc.get("/metrics").json()
    assert m["would_have"]["drop"] + m["would_have"]["wait"] >= 1
    assert m["enforcing"] is False
    tc.__exit__(None, None, None)


def test_transport_error_is_reported_not_swallowed(client):
    def boom(request):
        raise httpx.ConnectTimeout("pool exhausted")

    tc = make(client, boom)
    r = tc.post("/v1/messages", json=BODY)
    assert r.status_code == 502
    assert "transport error" in r.json()["error"]["message"]
    tc.__exit__(None, None, None)
