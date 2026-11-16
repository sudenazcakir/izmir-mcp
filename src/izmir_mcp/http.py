"""İzmir API'leri için ortak HTTP istemcisi: zaman aşımı, kısa tekrar deneme, tek tip hata."""

import asyncio
import logging
from typing import Any

import httpx

from .breaker import breakers
from .cache import store
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


def _circuit_open_error(source: str) -> SourceError:
    h = breakers.health(source)
    wait = h.retry_in_seconds()
    when = f"yaklaşık {wait} sn sonra" if wait else "birazdan"
    return SourceError(
        code=ErrorCode.CIRCUIT_OPEN, source=source, retryable=True,
        message=(f"{source} art arda {h.consecutive_failures} kez cevap vermediği için şu an kapalı kabul ediliyor; "
                 f"{when} yeniden denenecek."),
        hint="Kullanıcıya bu kaynaktan şu an veri alınamadığını açıkça söyle; veri uydurma.",
    )


async def get_json(url: str, *, source: str, params: dict[str, Any] | None = None,
                   retries: int | None = None) -> Any:
    """Kaynağa istek atar; devre kesici açıksa hiç denemeden CIRCUIT_OPEN hatası verir."""
    if not breakers.allow(source):
        raise SourceFailure(_circuit_open_error(source))
    try:
        data = await _get_json(url, source=source, params=params, retries=retries)
    except SourceFailure as f:
        if f.error.code == ErrorCode.NOT_FOUND:
            breakers.record_success(source)  # kaynak ayakta, sadece kayıt yok
        else:
            breakers.record_failure(source, f.error.message)
        raise
    except BaseException:
        breakers.release(source)  # iptal vb.: sayma ama yarı açık deneme kilitli kalmasın
        raise
    breakers.record_success(source)
    return data


async def _get_json(url: str, *, source: str, params: dict[str, Any] | None, retries: int | None) -> Any:
    retries = get_settings().http_retries if retries is None else retries
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


async def get_json_cached(url: str, *, source: str, ttl: float, params: dict[str, Any] | None = None,
                          stale_not_before: float | None = None) -> tuple[Any, bool, bool]:
    """(veri, önbellekten_mi, bayat_mi). Kaynak çökerse son iyi veriye düşer (bellek, sonra Redis).

    stale_not_before (epoch): son iyi veri bu andan önce alınmışsa kullanılmaz, hata döner.
    """
    key = f"{url}?{sorted((params or {}).items())}"
    hit = await store.get(key)
    if hit is not None:
        return hit, True, False
    try:
        data = await get_json(url, source=source, params=params)
    except SourceFailure:
        stale = await store.get_stale(key, not_before=stale_not_before)
        if stale is not None:
            return stale, True, True
        raise
    await store.set(key, data, ttl)
    return data, False, False
