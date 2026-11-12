import json
from pathlib import Path

import httpx
import pytest

from izmir_mcp import http
from izmir_mcp.cache import cache
from izmir_mcp.config import get_settings

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text("utf-8"))


class FakeIzmir:
    """İzmir API'sini taklit eden httpx transport'u. routes: yol -> (durum, gövde) ya da Exception."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        for prefix, result in self.routes.items():
            if path == prefix or path.startswith(prefix + "/") or (prefix.endswith("*") and path.startswith(prefix[:-1])):
                if isinstance(result, Exception):
                    raise result
                status, body = result
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"Message": "not found"})


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "http_retries", 0)
    monkeypatch.setattr(get_settings(), "database_url", None)  # .env içindeki gerçek veritabanına dokunma
    cache.clear()
    f = FakeIzmir()
    http.set_client(httpx.AsyncClient(transport=httpx.MockTransport(f)))
    yield f
    http.set_client(None)
    cache.clear()
    get_settings.cache_clear()
