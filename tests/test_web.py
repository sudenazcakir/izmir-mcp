import json
from types import SimpleNamespace as NS

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from izmir_mcp.config import Settings
from izmir_mcp.web import (
    MAX_HISTORY_CHARS,
    MCP_BETA,
    RateLimiter,
    clean_history,
    client_ip,
    create_app,
    extract_places,
    parse_location,
)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def fake_client(*responses):
    msgs = FakeMessages(responses)
    return NS(beta=NS(messages=msgs)), msgs


def settings(**kw) -> Settings:
    base = dict(anthropic_api_key="test", public_base_url="https://example.onrender.com", mcp_api_key="s3cret")
    base.update(kw)
    return Settings(_env_file=None, **base)


def reply(*blocks, stop="end_turn"):
    return NS(content=list(blocks), stop_reason=stop)


def text(t):
    return NS(type="text", text=t)


def tool_use(name, id="t1"):
    return NS(type="mcp_tool_use", id=id, name=name, server_name="izmir", input={})


def tool_result(payload, id="t1", is_error=False):
    return NS(type="mcp_tool_result", tool_use_id=id, is_error=is_error, content=[NS(type="text", text=json.dumps(payload))])


PHARMACIES = {
    "district": "Buca",
    "on_duty": True,
    "pharmacies": [
        {"name": "Erim Eczanesi", "address": "127 Sok. No:40/B", "phone": "0232 426 02 10",
         "lat": 38.3881, "lon": 27.1752, "distance_km": 0.3},
        {"name": "Konumsuz Eczane", "address": None, "phone": None, "lat": None, "lon": None, "distance_km": None},
        {"name": "Uzak Eczane", "lat": 41.0, "lon": 29.0, "distance_km": 400},
    ],
}


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_page_and_health():
    client, _ = fake_client()
    with TestClient(create_app(settings(), client)) as tc:
        page = tc.get("/")
        assert page.status_code == 200 and "İzmir Asistanı" in page.text
        assert tc.get("/health").json() == {"ok": True, "chat": True}


def test_chat_sends_mcp_connector_and_returns_answer():
    client, msgs = fake_client(reply(tool_use("find_pharmacies"), text("Buca'da nöbetçi: **Ecza A**.")))
    with TestClient(create_app(settings(), client)) as tc:
        r = tc.post("/api/chat", json={"messages": [{"role": "user", "content": "Buca'da nöbetçi eczane?"}]})
    assert r.status_code == 200
    assert r.json() == {"answer": "Buca'da nöbetçi: **Ecza A**.", "tools": ["find_pharmacies"], "places": []}
    call = msgs.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["betas"] == [MCP_BETA]
    assert call["mcp_servers"] == [
        {"type": "url", "url": "https://example.onrender.com/mcp", "name": "izmir", "authorization_token": "s3cret"}
    ]
    assert call["tools"] == [{"type": "mcp_toolset", "mcp_server_name": "izmir"}]


def test_chat_continues_after_pause_turn():
    client, msgs = fake_client(reply(tool_use("get_stop_arrivals"), stop="pause_turn"), reply(text("4 dk sonra 302.")))
    with TestClient(create_app(settings(), client)) as tc:
        r = tc.post("/api/chat", json={"messages": [{"role": "user", "content": "Bornova Metro?"}]})
    assert r.json()["answer"] == "4 dk sonra 302."
    assert len(msgs.calls) == 2 and msgs.calls[1]["messages"][-1]["role"] == "assistant"


def test_chat_refusal_is_friendly():
    client, _ = fake_client(reply(stop="refusal"))
    with TestClient(create_app(settings(), client)) as tc:
        r = tc.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200 and "cevap veremiyorum" in r.json()["answer"]


def test_chat_not_configured():
    with TestClient(create_app(settings(anthropic_api_key=None))) as tc:
        assert tc.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]}).status_code == 503


def test_chat_rejects_bad_input():
    client, _ = fake_client()
    with TestClient(create_app(settings(), client)) as tc:
        assert tc.post("/api/chat", json={"messages": []}).status_code == 400
        assert tc.post("/api/chat", json={"messages": [{"role": "user", "content": "a" * 1001}]}).status_code == 400
        assert tc.post("/api/chat", content=b"not json").status_code == 400


def test_chat_rate_limited():
    client, _ = fake_client(*[reply(text("ok")) for _ in range(3)])
    with TestClient(create_app(settings(chat_rate_limit=2), client)) as tc:
        codes = [tc.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_mcp_requires_key():
    client, _ = fake_client()
    with TestClient(create_app(settings(), client)) as tc:
        assert tc.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401
        ok = tc.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


@pytest.mark.parametrize(
    "raw, ok",
    [
        ([{"role": "user", "content": "a"}], True),
        ([{"role": "assistant", "content": "selam"}, {"role": "user", "content": "a"}], True),
        ([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}], False),
        ([{"role": "system", "content": "a"}], False),
        ("nope", False),
    ],
)
def test_clean_history(raw, ok):
    assert (clean_history(raw) is not None) is ok


def test_rate_limiter_window():
    rl = RateLimiter(1, 100, window=0)
    assert rl.allow("a") and rl.allow("a")


def ask(tc, xff=None):
    headers = {"X-Forwarded-For": xff} if xff else {}
    return tc.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]}, headers=headers).status_code


def test_spoofed_forwarded_for_does_not_bypass_limit():
    client, _ = fake_client(*[reply(text("ok")) for _ in range(3)])
    with TestClient(create_app(settings(chat_rate_limit=2), client)) as tc:
        # Render gerçek IP'yi sona ekler; ilk değer her istekte farklı ve sahte.
        codes = [ask(tc, f"10.0.0.{i}, 203.0.113.7") for i in range(3)]
    assert codes == [200, 200, 429]


def test_client_ip_uses_last_forwarded_value():
    req = Request({"type": "http", "headers": [(b"x-forwarded-for", b"1.1.1.1, 2.2.2.2")], "client": ("9.9.9.9", 1)})
    assert client_ip(req) == "2.2.2.2"
    assert client_ip(Request({"type": "http", "headers": [], "client": ("9.9.9.9", 1)})) == "9.9.9.9"


def test_global_limit_applies_across_ips():
    client, _ = fake_client(*[reply(text("ok")) for _ in range(3)])
    with TestClient(create_app(settings(chat_rate_limit=10, chat_global_rate_limit=2), client)) as tc:
        codes = [ask(tc, f"198.51.100.{i}") for i in range(3)]
    assert codes == [200, 200, 429]


def test_rate_limiter_keeps_bounded_keys():
    rl = RateLimiter(5, 10_000, max_keys=3)
    for i in range(10):
        assert rl.allow(f"ip{i}")
    assert len(rl.hits) <= 3


def test_long_history_is_trimmed_to_budget():
    client, msgs = fake_client(reply(text("ok")))
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "a" * 4000} for i in range(11)]
    history.append({"role": "user", "content": "Son soru?"})
    with TestClient(create_app(settings(), client)) as tc:
        assert tc.post("/api/chat", json={"messages": history}).status_code == 200
    sent = msgs.calls[0]["messages"]
    assert sum(len(m["content"]) for m in sent) <= MAX_HISTORY_CHARS
    assert sent[0]["role"] == "user"
    assert sent[-1] == {"role": "user", "content": "Son soru?"}


def test_trimmed_history_starts_with_user():
    raw = [
        {"role": "user", "content": "u" * 1000},
        {"role": "assistant", "content": "a" * 2000},
        {"role": "user", "content": "u" * 1000},
        {"role": "assistant", "content": "a" * 2000},
        {"role": "user", "content": "u" * 1000},
        {"role": "assistant", "content": "a" * 2000},
        {"role": "user", "content": "son"},
    ]
    out = clean_history(raw)
    assert out[0]["role"] == "user" and out[-1]["content"] == "son"
    assert sum(len(m["content"]) for m in out) <= MAX_HISTORY_CHARS


def test_mcp_fails_closed_when_public_without_key():
    with TestClient(create_app(settings(mcp_api_key=None))) as tc:
        assert tc.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 503


def test_mcp_open_locally_without_key():
    local = settings(mcp_api_key=None, public_base_url=None, anthropic_api_key=None)
    with TestClient(create_app(local)) as tc:
        assert tc.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 200


def test_chat_returns_pharmacy_places_for_map():
    client, _ = fake_client(reply(tool_use("find_pharmacies"), tool_result(PHARMACIES), text("Erim Eczanesi en yakın.")))
    with TestClient(create_app(settings(), client)) as tc:
        r = tc.post("/api/chat", json={"messages": [{"role": "user", "content": "Buca nöbetçi eczane"}]})
    assert r.json()["places"] == [{
        "name": "Erim Eczanesi", "address": "127 Sok. No:40/B", "phone": "0232 426 02 10",
        "lat": 38.3881, "lon": 27.1752, "distance_km": 0.3,
    }]


def test_places_ignore_other_tools_and_errors():
    client, _ = fake_client(reply(
        tool_use("get_air_quality", id="a"), tool_result(PHARMACIES, id="a"),
        tool_use("find_pharmacies", id="b"), tool_result(PHARMACIES, id="b", is_error=True),
        text("ok"),
    ))
    with TestClient(create_app(settings(), client)) as tc:
        r = tc.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]})
    assert r.json()["places"] == []


def test_location_goes_to_system_prompt():
    client, msgs = fake_client(reply(text("ok")), reply(text("ok")))
    with TestClient(create_app(settings(), client)) as tc:
        tc.post("/api/chat", json={"messages": [{"role": "user", "content": "en yakın eczane"}],
                                   "location": {"lat": 38.3881, "lon": 27.1752}})
        tc.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}], "location": {"lat": 51.5, "lon": 0}})
    assert "38.38810" in msgs.calls[0]["system"] and "27.17520" in msgs.calls[0]["system"]
    assert "konumunu paylaştı" not in msgs.calls[1]["system"]


@pytest.mark.parametrize(
    "raw, ok",
    [
        ({"lat": 38.42, "lon": 27.14}, True),
        ({"lat": 41.0, "lon": 29.0}, False),
        ({"lat": "38", "lon": 27}, False),
        ({"lat": True, "lon": 27}, False),
        ({"lat": float("nan"), "lon": 27}, False),
        (None, False),
    ],
)
def test_parse_location(raw, ok):
    assert (parse_location(raw) is not None) is ok


def test_extract_places_handles_garbage():
    assert extract_places("not json") == []
    assert extract_places('{"pharmacies": "x"}') == []
    assert extract_places("[1, 2]") == []


def test_unexpected_error_returns_json():
    class Boom:
        async def create(self, **kw):
            raise RuntimeError("x")

    with TestClient(create_app(settings(), NS(beta=NS(messages=Boom())))) as tc:
        r = tc.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 500 and "error" in r.json()
