"""İzmir API'leri için ortak HTTP istemcisi: zaman aşımı, kısa tekrar deneme, tek tip hata."""

import asyncio
import logging
from typing import Any

import httpx

from .cache import cache
from .config import get_settings
from .models import ErrorCode, SourceError

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None
_override: httpx.AsyncClient | None = None


class SourceFailure(Exception):
    def __init__(self, error: SourceError):
        super().__init__(error.message)
        self.error = error


def client() -> httpx.AsyncClient:
    """Olay döngüsü başına bir istemci (testler ve ingest farklı döngülerde çalışabiliyor)."""
    global _client, _client_loop
    if _override is not None:
        return _override
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop:
        _client_loop = loop
        s = get_settings()
        _client = httpx.AsyncClient(
            timeout=s.http_timeout_seconds,
            headers={"User-Agent": "izmir-mcp/0.1 (+https://github.com/sudenazcakir/izmir-mcp)"},
            follow_redirects=True,
        )
    return _client


def set_client(c: httpx.AsyncClient | None) -> None:
    """Testlerde sahte transport takmak için."""
    global _override
    _override = c


async def get_json(url: str, *, source: str, params: dict[str, Any] | None = None) -> Any:
    retries = get_settings().http_retries
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = await client().get(url, params=params)
            if r.status_code >= 500:
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            if r.status_code == 404:
                raise SourceFailure(SourceError(code=ErrorCode.NOT_FOUND, source=source,
                                                message=f"{source} bu kayıt için veri döndürmedi (404)."))
            r.raise_for_status()
            return r.json()
        except SourceFailure:
            raise
        except httpx.TimeoutException as e:
            last_exc = e
            code = ErrorCode.TIMEOUT
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last_exc = e
            code = ErrorCode.SOURCE_UNAVAILABLE
        except ValueError as e:  # JSON değil
            raise SourceFailure(SourceError(code=ErrorCode.BAD_RESPONSE, source=source,
                                            message=f"{source} beklenmeyen bir cevap döndürdü.")) from e
        if attempt < retries:
            await asyncio.sleep(0.4 * (attempt + 1))
    log.warning("source %s failed: %r", source, last_exc)
    raise SourceFailure(SourceError(
        code=code, source=source, retryable=True,
        message=f"{source} şu anda cevap vermiyor. Biraz sonra tekrar denenebilir.",
    )) from last_exc


async def get_json_cached(url: str, *, source: str, ttl: float, params: dict[str, Any] | None = None
                          ) -> tuple[Any, bool, bool]:
    """(veri, önbellekten_mi, bayat_mi). Kaynak çökerse son iyi veriye düşer."""
    key = f"{url}?{sorted((params or {}).items())}"
    hit = cache.get(key)
    if hit is not None:
        return hit, True, False
    try:
        data = await get_json(url, source=source, params=params)
    except SourceFailure:
        stale = cache.get_stale(key)
        if stale is not None:
            return stale, True, True
        raise
    cache.set(key, data, ttl)
    return data, False, False
