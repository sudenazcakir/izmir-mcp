import json
from types import SimpleNamespace as NS

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from izmir_mcp.config import Settings
from izmir_mcp.web import (
    MAX_HISTORY_CHARS,
    MCP_BETA,
    LiveSigner,
    RateLimiter,
    clean_history,
    client_ip,
    create_app,
    extract_places,
    extract_trip,
    extract_weather,
    live_payload,
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
        assert page.status_code == 200 and "CityMate" in page.text
        assert tc.get("/health").json() == {"ok": True, "chat": True}


def test_chat_sends_mcp_connector_and_returns_answer():
    client, msgs = fake_client(reply(tool_use("find_pharmacies"), text("Buca'da nöbetçi: **Ecza A**.")))
    with TestClient(create_app(settings(), client)) as tc:
        r = tc.post("/api/chat", json={"messages": [{"role": "user", "content": "Buca'da nöbetçi eczane?"}]})
    assert r.status_code == 200
    assert r.json() == {"answer": "Buca'da nöbetçi: **Ecza A**.", "tools": ["find_pharmacies"], "places": [],
                        "trip": None, "live": None, "weather": None}
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


def test_chat_refusal_follows_ui_language():
    client, _ = fake_client(reply(stop="refusal"))
    with TestClient(create_app(settings(), client)) as tc:
        r = tc.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}], "lang": "en"})
    assert r.status_code == 200 and "can't answer" in r.json()["answer"]


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


def test_english_ui_asks_for_english_answers():
    client, msgs = fake_client(*[reply(text("ok")) for _ in range(5)])
    with TestClient(create_app(settings(), client)) as tc:
        for lang in ("en", "tr", "<script>", ["en"]):
            tc.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}], "lang": lang})
        tc.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]})
    assert "İngilizce cevap ver" in msgs.calls[0]["system"]
    assert all("İngilizce" not in c["system"] for c in msgs.calls[1:])


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


TRIP = {
    "origin": {"query": "Bornova", "name": "Bornova", "kind": "metro", "lat": 38.4577, "lon": 27.2125},
    "destination": {"query": "Konak", "name": "Konak", "kind": "metro", "lat": 38.4189, "lon": 27.1287},
    "straight_km": 8.7,
    "nearby_stations": [{"system": "metro", "name": "Bornova", "distance_km": 0.0, "end": "origin"}],
    "maps_url": "https://www.google.com/maps/dir/?api=1&origin=38.4577,27.2125&destination=38.4189,27.1287&travelmode=transit",
    "meta": {"sources": [], "fetched_at": "2026-11-16T16:00:00Z"},
    "error": None,
}


def test_chat_returns_trip_card():
    client, _ = fake_client(reply(tool_use("get_transit_directions"), tool_result(TRIP), text("Metroyla aktarmasız.")))
    with TestClient(create_app(settings(), client)) as tc:
        r = tc.post("/api/chat", json={"messages": [{"role": "user", "content": "Bornova'dan Konak'a"}]})
    trip = r.json()["trip"]
    assert trip["origin"]["name"] == "Bornova" and trip["destination"]["name"] == "Konak"
    assert trip["maps_url"].endswith("travelmode=transit")
    assert trip["nearby"] == [{"system": "metro", "name": "Bornova", "end": "origin"}]


def test_extract_weather_offers_district_but_never_the_users_location():
    assert extract_weather(json.dumps({"place": "Karşıyaka", "day": "yarın", "periods": []})) == {"place": "Karşıyaka"}
    assert extract_weather(json.dumps({"place": "Konumun", "lat": 38.4, "lon": 27.1})) is None
    assert extract_weather(json.dumps({"place": "Bornova", "error": {"code": "source_unavailable"}})) is None
    assert extract_weather("nope") is None


def test_extract_trip_rejects_bad_results():
    assert extract_trip("nope") is None
    assert extract_trip(json.dumps({**TRIP, "error": {"code": "not_found"}})) is None
    assert extract_trip(json.dumps({**TRIP, "maps_url": "javascript:alert(1)"})) is None
    assert extract_trip(json.dumps({**TRIP, "origin": None})) is None


ARRIVALS = {
    "query": "Bornova Metro",
    "stops": [{"id": "30005", "name": "Bornova Metro", "lat": 38.4577, "lon": 27.2125},
              {"id": "evil<script>", "name": "x", "lat": 38.4, "lon": 27.2}],
    "arrivals": [
        {"stop_id": "30005", "line_number": "505", "line_name": "Çamkule - Bornova Metro", "bus_id": 1234,
         "stops_remaining": 2, "eta_minutes": 2, "eta_method": "blend", "bus_lat": 38.455, "bus_lon": 27.205,
         "wheelchair_accessible": True},
        {"stop_id": "30005", "line_number": "268", "line_name": "Doğanlar", "bus_id": 99,
         "stops_remaining": 4, "eta_minutes": 3, "eta_method": "stops", "bus_lat": None, "bus_lon": None},
    ],
    "meta": {"sources": [], "fetched_at": "2026-11-16T16:00:00Z"},
    "error": None,
}


def test_chat_returns_live_bus_card():
    client, _ = fake_client(reply(tool_use("get_stop_arrivals"), tool_result(ARRIVALS), text("505 2 dk.")))
    with TestClient(create_app(settings(), client)) as tc:
        r = tc.post("/api/chat", json={"messages": [{"role": "user", "content": "Bornova Metro otobüs"}]})
    live = r.json()["live"]
    assert live["stops"] == [{"id": "30005", "name": "Bornova Metro", "lat": 38.4577, "lon": 27.2125}]
    assert live["buses"][0] == {"line": "505", "name": "Çamkule - Bornova Metro", "bus_id": 1234, "stop_id": "30005",
                                "eta": 2, "stops_left": 2, "lat": 38.455, "lon": 27.205, "accessible": True}
    assert live["buses"][1]["lat"] is None


def test_live_payload_rejects_errors():
    assert live_payload({**ARRIVALS, "error": {"code": "timeout"}}) is None
    assert live_payload({**ARRIVALS, "stops": []}) is None


def live_app(monkeypatch, *, budget=None):
    from izmir_mcp import tools as tools_mod
    from izmir_mcp import web
    from izmir_mcp.models import StopArrivals

    calls = []

    async def fake_arrivals(query, stops, notes, limit, started=None):
        calls.append([s.id for s in stops])
        return StopArrivals.model_validate({**ARRIVALS, "stops": [s.model_dump() for s in stops]})

    monkeypatch.setattr(tools_mod, "arrivals_for_stops", fake_arrivals)
    if budget is not None:
        monkeypatch.setattr(web, "LIVE_UPSTREAM_BUDGET", budget)
    client, msgs = fake_client(*[reply(tool_use("get_stop_arrivals"), tool_result(ARRIVALS), text("ok"))
                                 for _ in range(3)])
    return create_app(settings(), client), calls, msgs


def test_live_needs_token_from_chat(monkeypatch):
    app, calls, msgs = live_app(monkeypatch)
    with TestClient(app) as tc:
        chat = tc.post("/api/chat", json={"messages": [{"role": "user", "content": "Bornova Metro"}]}).json()
        token = chat["live"]["token"]
        ok = tc.post("/api/live", json={"token": token})
        raw = tc.post("/api/live", json={"stops": [{"id": "30005"}]})  # eski biçim: imzasız durak listesi
        forged = tc.post("/api/live", json={"token": token[:-3] + "AAA"})
        other = LiveSigner(b"x" * 32).sign([{"id": "40010", "name": None, "lat": None, "lon": None}])
        foreign = tc.post("/api/live", json={"token": other})
    assert ok.status_code == 200 and ok.json()["buses"][0]["line"] == "505"
    assert raw.status_code == forged.status_code == foreign.status_code == 403
    assert calls == [["30005"]]  # yalnız imzalı istek ESHOT'a gitti
    assert len(msgs.calls) == 1  # canlı yenileme Claude'a gitmez


def test_live_token_expires():
    signer = LiveSigner(b"k" * 32)
    token = signer.sign([{"id": "30005", "name": "B", "lat": 38.45, "lon": 27.21}], now=1000)
    assert signer.verify(token, now=1000 + 60)[0]["id"] == "30005"
    assert signer.verify(token, now=1000 + 12 * 60) is None
    assert signer.verify("garbage") is None and signer.verify(None) is None


def test_live_upstream_budget_is_shared(monkeypatch):
    from izmir_mcp.sources import eshot

    eshot.clear_arrivals_cache()
    app, calls, _ = live_app(monkeypatch, budget=1)
    with TestClient(app) as tc:
        token = tc.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]}).json()["live"]["token"]
        first = tc.post("/api/live", json={"token": token})
        second = tc.post("/api/live", json={"token": token}, headers={"X-Forwarded-For": "203.0.113.9"})
    assert first.status_code == 200
    assert second.status_code == 503 and len(calls) == 1  # bütçe bitti, ESHOT'a gidilmedi


def test_leaflet_is_served_locally_with_csp():
    import hashlib

    with TestClient(create_app(settings(), fake_client()[0])) as tc:
        page = tc.get("/")
        js = tc.get("/static/vendor/leaflet/leaflet.js")
        css = tc.get("/static/vendor/leaflet/leaflet.css")
        missing = tc.get("/static/vendor/leaflet/../web.py")
    csp = page.headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline'" in csp and "cdnjs" not in page.text
    assert "/static/vendor/leaflet/" in page.text
    assert hashlib.sha256(js.content).hexdigest() == "db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a"
    assert hashlib.sha256(css.content).hexdigest() == "a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6"
    assert js.headers["x-content-type-options"] == "nosniff"
    assert missing.status_code == 404
