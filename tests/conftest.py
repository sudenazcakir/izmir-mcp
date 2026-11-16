import json
from pathlib import Path

import httpx
import pytest

from izmir_mcp import http
from izmir_mcp.breaker import breakers
from izmir_mcp.cache import cache, store
from izmir_mcp.config import get_settings
from izmir_mcp.sources import eshot

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text("utf-8"))


class FakeIzmir:
    """İzmir API'sini taklit eden httpx transport'u. routes: yol -> (durum, gövde) ya da Exception."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.calls: list[str] = []
        self.methods: list[tuple[str, str]] = []  # (HTTP yöntemi, yol)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        self.methods.append((request.method, path))
        for prefix, result in self.routes.items():
            if path == prefix or path.startswith(prefix + "/") or (prefix.endswith("*") and path.startswith(prefix[:-1])):
                if isinstance(result, Exception):
                    raise result
                status, body = result
                if isinstance(body, str):  # CSV gibi düz metin dosyaları
                    return httpx.Response(status, content=body.encode("utf-8"), headers={"content-type": "text/csv"})
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"Message": "not found"})


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "http_retries", 0)
    monkeypatch.setattr(get_settings(), "database_url", None)  # .env içindeki gerçek veritabanına dokunma
    monkeypatch.setattr(get_settings(), "redis_url", None)  # .env içindeki gerçek Redis'e de dokunma
    monkeypatch.setattr(get_settings(), "demo_down_sources", "")  # .env'deki demo anahtarı testleri bozmasın
    store.set_redis(None)
    cache.clear()
    eshot.clear_arrivals_cache()
    breakers.reset()
    f = FakeIzmir()
    http.set_client(httpx.AsyncClient(transport=httpx.MockTransport(f)))
    yield f
    http.set_client(None)
    store.set_redis(None)
    cache.clear()
    eshot.clear_arrivals_cache()
    breakers.reset()
    get_settings.cache_clear()
