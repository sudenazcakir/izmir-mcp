"""İki katmanlı önbellek: süreç içi TTL önbellek + (REDIS_URL varsa) Upstash Redis.

- `store` (LayeredCache) http.get_json_cached'in kullandığı async arayüz: get / get_stale / set / clear.
- Okuma önce bellekten, sonra Redis'ten yapılır; Redis'ten gelen taze değer belleğe de yazılır.
- Yazma her zaman belleğe, Redis varsa ona da gider. Redis'te değer TTL'den sonra STALE_RETENTION boyunca
  daha durur: kaynak çökünce (sunucu yeniden başlamış olsa bile) "son iyi değer" oradan gelir.
- REDIS_URL yoksa ya da Redis hata verirse sessizce yalnız belleğe düşülür. Redis hataları devre kesiciden
  geçer ("Upstash Redis (önbellek)" kaynağı): art arda hata verirse bir süre hiç denenmez, böylece her
  istekte bağlantı zaman aşımı beklenmez; durumu get_source_status'ta görünür.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .breaker import breakers
from .config import get_settings

log = logging.getLogger(__name__)

SOURCE_REDIS = "Upstash Redis (önbellek)"
KEY_PREFIX = "izmir-mcp:v1:"
STALE_RETENTION_SECONDS = 2 * 24 * 3600
REDIS_TIMEOUT_SECONDS = 1.0
_MISS = object()


class TTLCache:
    """Basit süreç içi TTL önbellek (senkron)."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            return None
        return value

    def get_stale(self, key: str) -> Any | None:
        """Süresi dolmuş olsa bile son iyi değer (kaynak çökünce kullanılır)."""
        item = self._data.get(key)
        return item[1] if item else None

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._data[key] = (time.monotonic() + ttl_seconds, value)

    def clear(self) -> None:
        self._data.clear()


def normalize_redis_url(url: str) -> str:
    """Upstash yalnız TLS kabul eder; panelde gösterilen redis:// adresini rediss://'e çevir."""
    url = url.strip()
    if url.startswith("redis://") and ".upstash.io" in url:
        log.warning("REDIS_URL redis:// ile başlıyor; Upstash TLS istediği için rediss:// kullanılıyor.")
        return "rediss://" + url[len("redis://"):]
    return url


class LayeredCache:
    """Bellek + Redis. Arayüz TTLCache ile aynı adlarda ama async (Redis ağ çağrısı yapıyor)."""

    def __init__(self, memory: TTLCache) -> None:
        self.memory = memory
        self._redis: Any = None
        self._redis_loop: asyncio.AbstractEventLoop | None = None
        self._redis_url: str | None = None
        self._override: Any = None

    # -- Redis istemcisi

    def set_redis(self, client: Any) -> None:
        """Testlerde sahte Redis takmak için (None: ayarlardaki REDIS_URL'e dön)."""
        self._override = client

    def enabled(self) -> bool:
        return self._override is not None or bool(get_settings().redis_url)

    def _client(self) -> Any:
        if self._override is not None:
            return self._override
        url = get_settings().redis_url
        if not url:
            return None
        loop = asyncio.get_running_loop()
        if self._redis is None or self._redis_loop is not loop or self._redis_url != url:
            import redis.asyncio as aioredis  # yalnız REDIS_URL ayarlıysa gerekiyor

            self._redis = aioredis.from_url(
                normalize_redis_url(url), decode_responses=True, socket_timeout=REDIS_TIMEOUT_SECONDS,
                socket_connect_timeout=REDIS_TIMEOUT_SECONDS, health_check_interval=30,
            )
            self._redis_loop, self._redis_url = loop, url
        return self._redis

    async def _call(self, fn: Callable[[Any], Awaitable[Any]]) -> Any:
        """Redis çağrısı; hata olursa _MISS döner, istisna fırlatmaz."""
        r = self._client()
        if r is None or not breakers.allow(SOURCE_REDIS):
            return _MISS
        try:
            result = await fn(r)
        except Exception as e:  # bağlantı, zaman aşımı, kimlik doğrulama...
            log.warning("redis error: %r", e)
            breakers.record_failure(SOURCE_REDIS, f"Redis cevap vermedi ({type(e).__name__}).")
            return _MISS
        except BaseException:
            breakers.release(SOURCE_REDIS)
            raise
        breakers.record_success(SOURCE_REDIS)
        return result

    async def _entry(self, key: str) -> dict[str, Any] | None:
        raw = await self._call(lambda r: r.get(KEY_PREFIX + key))
        if raw is _MISS or raw is None:
            return None
        try:
            entry = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return entry if isinstance(entry, dict) and "v" in entry and "exp" in entry else None

    async def ping(self) -> bool:
        return await self._call(lambda r: r.ping()) is not _MISS

    # -- önbellek arayüzü

    async def get(self, key: str) -> Any | None:
        value = self.memory.get(key)
        if value is not None:
            return value
        entry = await self._entry(key)
        if entry is None:
            return None
        left = entry["exp"] - time.time()
        if left <= 0:
            return None
        self.memory.set(key, entry["v"], left)  # bu süreçte bir daha Redis'e gitme
        return entry["v"]

    async def get_stale(self, key: str) -> Any | None:
        value = self.memory.get_stale(key)
        if value is not None:
            return value
        entry = await self._entry(key)
        return entry["v"] if entry else None

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self.memory.set(key, value, ttl_seconds)
        payload = json.dumps({"exp": time.time() + ttl_seconds, "v": value}, ensure_ascii=False)
        ex = int(ttl_seconds + STALE_RETENTION_SECONDS)
        await self._call(lambda r: r.set(KEY_PREFIX + key, payload, ex=ex))

    def clear(self) -> None:
        """Yalnız bellek katmanını temizler; Redis'teki ortak veriye dokunmaz."""
        self.memory.clear()


cache = TTLCache()
store = LayeredCache(cache)
