import json
import time

import httpx
import pytest

from izmir_mcp import tools
from izmir_mcp.breaker import BreakerState, breakers
from izmir_mcp.cache import KEY_PREFIX, SOURCE_REDIS, STALE_RETENTION_SECONDS, cache, normalize_redis_url, store

from .conftest import load

NOBETCI = "/api/ibb/nobetcieczaneler"


class FakeRedis:
    """redis.asyncio istemcisinin kullandığımız kısmı: get / set(ex=) / ping. fail=True: bağlantı hatası."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.ex: dict[str, int] = {}
        self.calls = 0
        self.fail = False

    def _check(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis down")

    async def get(self, key):
        self._check()
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self._check()
        self.data[key], self.ex[key] = value, ex
        return True

    async def ping(self):
        self._check()
        return True


@pytest.fixture
def redis(fake):
    r = FakeRedis()
    store.set_redis(r)
    fake.routes[NOBETCI] = (200, load("nobetci.json"))
    return r


def _http_calls(fake) -> int:
    return sum(1 for c in fake.calls if c == NOBETCI)


def _key(r: FakeRedis) -> str:
    return next(k for k in r.data if "nobetcieczaneler" in k)


async def test_without_redis_memory_cache_still_works(fake):
    fake.routes[NOBETCI] = (200, load("nobetci.json"))
    assert not store.enabled()
    await tools.find_pharmacies("Buca")
    second = await tools.find_pharmacies("Buca")
    assert _http_calls(fake) == 1 and second.meta.cached
    status = await tools.get_source_status(probe=False)
    assert SOURCE_REDIS not in {s.source for s in status.sources}


async def test_set_writes_to_redis_with_stale_retention(redis):
    await tools.find_pharmacies("Buca")
    key = _key(redis)
    assert key.startswith(KEY_PREFIX)
    assert redis.ex[key] == 15 * 60 + STALE_RETENTION_SECONDS
    entry = json.loads(redis.data[key])
    assert 14 * 60 < entry["exp"] - time.time() <= 15 * 60
    assert isinstance(entry["v"], list) and entry["v"]


async def test_new_process_reads_from_redis_without_calling_source(fake, redis):
    first = await tools.find_pharmacies("Buca")
    cache.clear()  # sunucu yeniden başladı: bellek boş, Redis dolu
    second = await tools.find_pharmacies("Buca")
    assert _http_calls(fake) == 1
    assert second.meta.cached and not second.meta.stale
    assert [p.name for p in second.pharmacies] == [p.name for p in first.pharmacies]


async def test_expired_redis_entry_is_used_as_stale_when_source_is_down(fake, redis):
    first = await tools.find_pharmacies("Buca")
    key = _key(redis)
    entry = json.loads(redis.data[key])
    entry["exp"] = time.time() - 60  # süresi dolmuş ama Redis'te hâlâ duruyor
    redis.data[key] = json.dumps(entry)
    cache.clear()
    fake.routes[NOBETCI] = httpx.ConnectError("down")
    r = await tools.find_pharmacies("Buca")
    assert r.error is None and r.meta.stale
    assert [p.name for p in r.pharmacies] == [p.name for p in first.pharmacies]


async def test_redis_down_falls_back_to_memory_and_trips_breaker(fake, redis):
    redis.fail = True
    r = await tools.find_pharmacies("Buca")
    assert r.error is None and r.pharmacies
    again = await tools.find_pharmacies("Buca")
    assert again.meta.cached and _http_calls(fake) == 1  # bellek katmanı çalışıyor

    for _ in range(5):
        cache.clear()
        await tools.find_pharmacies("Buca")
    assert breakers.health(SOURCE_REDIS).state == BreakerState.OPEN
    calls = redis.calls
    cache.clear()
    await tools.find_pharmacies("Buca")
    assert redis.calls == calls  # devre açık: Redis'e hiç gidilmedi, zaman aşımı beklenmedi


async def test_corrupt_redis_entry_is_ignored(fake, redis):
    await tools.find_pharmacies("Buca")
    redis.data[_key(redis)] = "bozuk{"
    cache.clear()
    r = await tools.find_pharmacies("Buca")
    assert r.error is None and r.pharmacies
    assert _http_calls(fake) == 2


async def test_source_status_reports_redis(fake, redis):
    status = await tools.get_source_status()
    by = {s.source: s for s in status.sources}
    assert by[SOURCE_REDIS].state == "up"
    redis.fail = True
    status = await tools.get_source_status()
    assert {s.source: s for s in status.sources}[SOURCE_REDIS].state == "degraded"


def test_upstash_url_is_upgraded_to_tls():
    assert normalize_redis_url("redis://default:pw@x-1.upstash.io:6379") == "rediss://default:pw@x-1.upstash.io:6379"
    assert normalize_redis_url(" rediss://default:pw@x-1.upstash.io:6379 ") == "rediss://default:pw@x-1.upstash.io:6379"
    assert normalize_redis_url("redis://localhost:6379") == "redis://localhost:6379"  # yerel Redis'e dokunma
