from types import SimpleNamespace as NS

import pytest
from starlette.testclient import TestClient

from izmir_mcp.config import Settings
from izmir_mcp.web import MCP_BETA, RateLimiter, clean_history, create_app


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


def tool_use(name):
    return NS(type="mcp_tool_use", name=name, server_name="izmir", input={})


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
    assert r.json() == {"answer": "Buca'da nöbetçi: **Ecza A**.", "tools": ["find_pharmacies"]}
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
    rl = RateLimiter(1, window=0)
    assert rl.allow("a") and rl.allow("a")
