"""İzmir API'leri için ortak HTTP istemcisi: zaman aşımı, kısa tekrar deneme, tek tip hata."""

import asyncio
import logging
from typing import Any

import httpx

from .breaker import breakers
from .cache import store
from .config import get_settings
from .models import ErrorCode, SourceError
from .text import norm

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
                   retries: int | None = None, as_text: bool = False, head: bool = False) -> Any:
    """Kaynağa istek atar; devre kesici açıksa hiç denemeden CIRCUIT_OPEN hatası verir.

    as_text=True: JSON yerine metin döndürür (CSV dosyaları için; UTF-8, olmazsa Windows-1254).
    head=True: içeriği indirmeyen hafif sağlık kontrolü (HEAD isteği), None döner. Sunucu 4xx'ten küçük bir
    kod ya da 405 (HEAD desteklenmiyor ama sunucu ayakta) dönerse başarılı sayılır; 404 ve 5xx hata sayılır.
    """
    if not breakers.allow(source):
        raise SourceFailure(_circuit_open_error(source))
    try:
        data = await _get_json(url, source=source, params=params, retries=retries, as_text=as_text, head=head)
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


def demo_down(source: str) -> bool:
    """DEMO_DOWN_SOURCES içinde mi? Büyük/küçük harf ve Türkçe karakter farkı önemsiz, ad tam eşleşmeli."""
    raw = get_settings().demo_down_sources
    return bool(raw) and norm(source) in {norm(x) for x in raw.split(",") if x.strip()}


def _decode_text(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1254")  # belediye dosyalarında eski Türkçe kodlama da görülüyor


async def _get_json(url: str, *, source: str, params: dict[str, Any] | None, retries: int | None,
                    as_text: bool = False, head: bool = False) -> Any:
    if demo_down(source):
        log.warning("DEMO_DOWN_SOURCES: %s çökmüş gibi davranılıyor (istek atılmadı)", source)
        raise SourceFailure(SourceError(
            code=ErrorCode.SOURCE_UNAVAILABLE, source=source, retryable=True,
            message=f"{source} şu anda cevap vermiyor. Biraz sonra tekrar denenebilir.",
        ))
    retries = get_settings().http_retries if retries is None else retries
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = await (client().head(url, params=params) if head else client().get(url, params=params))
            if r.status_code >= 500:
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            if head:
                if r.status_code < 400 or r.status_code == 405:
                    return None
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            if r.status_code == 404:
                raise SourceFailure(SourceError(code=ErrorCode.NOT_FOUND, source=source,
                                                message=f"{source} bu kayıt için veri döndürmedi (404)."))
            if r.status_code >= 400:
                # 4xx tekrar denemekle düzelmez: Render loglarında nedenini görebilmek için gövdenin başını yaz.
                log.warning("source %s HTTP %s: %s", source, r.status_code, r.text[:300].replace("\n", " "))
                raise SourceFailure(SourceError(
                    code=ErrorCode.SOURCE_UNAVAILABLE if r.status_code == 429 else ErrorCode.BAD_RESPONSE,
                    source=source, retryable=r.status_code == 429,
                    message=(f"{source} şu an çok fazla istek aldığı için cevap vermiyor (429). "
                             "Biraz sonra tekrar denenebilir."
                             if r.status_code == 429 else f"{source} isteği reddetti (HTTP {r.status_code})."),
                ))
            return _decode_text(r.content) if as_text else r.json()
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
                          as_text: bool = False, stale_not_before: float | None = None
                          ) -> tuple[Any, bool, bool]:
    """(veri, önbellekten_mi, bayat_mi). Kaynak çökerse son iyi veriye düşer (bellek, sonra Redis).

    stale_not_before (epoch): son iyi veri bu andan önce alınmışsa kullanılmaz, hata döner.
    """
    key = f"{url}?{sorted((params or {}).items())}"
    hit = await store.get(key)
    if hit is not None:
        return hit, True, False
    try:
        data = await get_json(url, source=source, params=params, as_text=as_text)
    except SourceFailure:
        stale = await store.get_stale(key, not_before=stale_not_before)
        if stale is not None:
            return stale, True, True
        raise
    await store.set(key, data, ttl)
    return data, False, False
