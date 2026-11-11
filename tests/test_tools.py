import httpx

from izmir_mcp import tools
from izmir_mcp.districts import find_district
from izmir_mcp.models import ErrorCode
from izmir_mcp.text import norm, parse_float

from .conftest import load


def test_norm_turkish():
    assert norm("BUCA 1") == "buca 1"
    assert norm("Şirinyer İSKELE") == "sirinyer iskele"
    assert norm("KARŞIYAKA") == "karsiyaka"


def test_parse_float_comma():
    assert parse_float("38,39156667") == 38.39156667
    assert parse_float("") is None


def test_find_district_with_suffix():
    assert find_district("Buca'da").name == "Buca"
    assert find_district("bornovadaki").name == "Bornova"
    assert find_district("KARŞIYAKA").name == "Karşıyaka"
    assert find_district("Atlantis") is None


def test_eta_estimate():
    assert tools.estimate_eta(0, None) == (0, "stops")
    assert tools.estimate_eta(4, None) == (6, "stops")
    minutes, method = tools.estimate_eta(3, 1.0)  # (4.5 + 1*1.3/18*60) / 2 ~= 4.4
    assert method == "blend" and minutes == 4


async def test_arrivals_by_stop_id(fake):
    fake.routes["/api/iztek/duragayaklasanotobusler/40010"] = (200, load("arrivals_40010.json"))
    r = await tools.get_stop_arrivals("40010")
    assert r.error is None
    assert len(r.arrivals) == 4
    assert r.arrivals == sorted(r.arrivals, key=lambda a: a.eta_minutes)
    assert all(a.eta_method == "stops" for a in r.arrivals)  # durak koordinatı yok


async def test_arrivals_by_name_uses_landmark_and_nearby_stops(fake):
    fake.routes["/api/metro/istasyonlar"] = httpx.ConnectError("reset")  # paketteki anlık görüntüye düşmeli
    fake.routes["/api/izban/istasyonlar"] = httpx.ConnectError("reset")
    fake.routes["/api/izdeniz/iskeleler"] = httpx.ConnectError("reset")
    fake.routes["/api/ibb/cbs/noktayayakinduraklar"] = (200, load("nearby_bornova.json"))
    fake.routes["/api/iztek/duragayaklasanotobusler*"] = (200, load("arrivals_30005.json"))
    r = await tools.get_stop_arrivals("Bornova Metro durağı")
    assert r.error is None
    assert {s.name for s in r.stops} == {"Bornova Metro"}
    assert r.arrivals and all(a.eta_method == "blend" for a in r.arrivals)
    bus_ids = [a.bus_id for a in r.arrivals]
    assert len(bus_ids) == len(set(bus_ids))  # aynı otobüs farklı peronlarda tekrar etmez


async def test_arrivals_source_down_returns_error_not_exception(fake):
    fake.routes["/api/iztek/duragayaklasanotobusler"] = httpx.ConnectError("reset")
    r = await tools.get_stop_arrivals("40010")
    assert r.error is not None
    assert r.error.code == ErrorCode.SOURCE_UNAVAILABLE
    assert r.error.retryable


async def test_arrivals_unknown_name(fake):
    r = await tools.get_stop_arrivals("Uydurma Durak Adı")
    assert r.error.code == ErrorCode.NOT_FOUND


async def test_pharmacies_buca(fake):
    fake.routes["/api/ibb/nobetcieczaneler"] = (200, load("nobetci.json"))
    r = await tools.find_pharmacies("Buca'da")
    assert r.error is None and r.district == "Buca"
    assert r.pharmacies
    assert all(p.on_duty for p in r.pharmacies)
    assert r.pharmacies == sorted(r.pharmacies, key=lambda p: p.distance_km)
    assert any("BUCA" in (p.region or "") for p in r.pharmacies)
    assert all(p.distance_km < 6 or "BUCA" in p.region for p in r.pharmacies)


async def test_pharmacies_cesme_excludes_cesmealti(fake):
    fake.routes["/api/ibb/nobetcieczaneler"] = (200, load("nobetci.json"))
    r = await tools.find_pharmacies("Çeşme", limit=10)
    assert r.pharmacies
    assert all(p.region != "ÇEŞMEALTI-İSKELE" for p in r.pharmacies)


async def test_pharmacies_unknown_district(fake):
    r = await tools.find_pharmacies("Atlantis")
    assert r.error.code == ErrorCode.INVALID_INPUT
    assert "Buca" in r.error.hint


async def test_pharmacies_stale_fallback(fake):
    fake.routes["/api/ibb/nobetcieczaneler"] = (200, load("nobetci.json"))
    first = await tools.find_pharmacies("Buca")
    from izmir_mcp.cache import cache

    for k, (_exp, v) in list(cache._data.items()):  # süreyi doldur
        cache._data[k] = (0, v)
    fake.routes["/api/ibb/nobetcieczaneler"] = httpx.ConnectError("down")
    second = await tools.find_pharmacies("Buca")
    assert second.error is None and second.meta.stale
    assert [p.name for p in second.pharmacies] == [p.name for p in first.pharmacies]
