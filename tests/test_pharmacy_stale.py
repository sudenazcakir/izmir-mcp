"""Nöbetçi eczane listesi günlüktür: kaynak çökünce dünün listesi "eski veri" olarak bile gösterilmemeli."""

import json
from datetime import datetime

import httpx

from izmir_mcp import tools
from izmir_mcp.cache import cache, store
from izmir_mcp.models import ErrorCode
from izmir_mcp.sources.pharmacy import ISTANBUL, duty_day_start, duty_ttl

from .conftest import load
from .test_cache import FakeRedis

NOBETCI = "/api/ibb/nobetcieczaneler"


def _ist(*args) -> datetime:
    return datetime(*args, tzinfo=ISTANBUL)


def _key() -> str:
    return next(k for k in store._stored_at if "nobetcieczaneler" in k)


def _expire_memory():
    for k, (_exp, v) in list(cache._data.items()):
        cache._data[k] = (0, v)


def test_duty_day_starts_at_eight():
    assert duty_day_start(_ist(2026, 11, 16, 7, 59)) == _ist(2026, 11, 15, 8, 0)
    assert duty_day_start(_ist(2026, 11, 16, 8, 0)) == _ist(2026, 11, 16, 8, 0)
    assert duty_day_start(_ist(2026, 11, 16, 23, 30)) == _ist(2026, 11, 16, 8, 0)


def test_cache_never_outlives_the_duty_change():
    assert duty_ttl(_ist(2026, 11, 16, 7, 55)) == 5 * 60
    assert duty_ttl(_ist(2026, 11, 16, 12, 0)) == 15 * 60


async def test_same_duty_day_list_is_shown_as_stale(fake):
    fake.routes[NOBETCI] = (200, load("nobetci.json"))
    first = await tools.find_pharmacies("Buca")
    _expire_memory()
    fake.routes[NOBETCI] = httpx.ConnectError("down")
    r = await tools.find_pharmacies("Buca")
    assert r.error is None and r.meta.stale
    assert [p.name for p in r.pharmacies] == [p.name for p in first.pharmacies]


async def test_previous_duty_day_list_is_not_shown(fake):
    fake.routes[NOBETCI] = (200, load("nobetci.json"))
    await tools.find_pharmacies("Buca")
    _expire_memory()
    store._stored_at[_key()] = duty_day_start().timestamp() - 60  # dünkü nöbet gününde alınmış
    fake.routes[NOBETCI] = httpx.ConnectError("down")
    r = await tools.find_pharmacies("Buca")
    assert r.pharmacies == []
    assert r.error is not None and r.error.code == ErrorCode.SOURCE_UNAVAILABLE
    assert "Önceki günün listesi" in r.error.hint


async def test_previous_duty_day_list_in_redis_is_not_shown(fake):
    redis = FakeRedis()
    store.set_redis(redis)
    fake.routes[NOBETCI] = (200, load("nobetci.json"))
    await tools.find_pharmacies("Buca")
    rkey = next(k for k in redis.data if "nobetcieczaneler" in k)
    entry = json.loads(redis.data[rkey])
    entry["at"] = duty_day_start().timestamp() - 3600  # Redis'te 2 gün duruyor ama dünkü liste
    entry["exp"] = entry["at"] + 900
    redis.data[rkey] = json.dumps(entry)
    cache.clear()  # sunucu yeniden başladı
    fake.routes[NOBETCI] = httpx.ConnectError("down")
    r = await tools.find_pharmacies("Buca")
    assert r.pharmacies == [] and r.error is not None


async def test_all_pharmacies_list_still_uses_stale_copy(fake):
    fake.routes["/api/ibb/eczaneler"] = (200, load("nobetci.json"))  # tüm eczaneler listesi günlük değişmez
    await tools.find_pharmacies("Buca", on_duty=False)
    _expire_memory()
    for k in list(store._stored_at):
        store._stored_at[k] = 0  # çok eski
    fake.routes["/api/ibb/eczaneler"] = httpx.ConnectError("down")
    r = await tools.find_pharmacies("Buca", on_duty=False)
    assert r.error is None and r.meta.stale and r.pharmacies
