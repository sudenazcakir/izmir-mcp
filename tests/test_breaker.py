import httpx
import pytest

from izmir_mcp import breaker, tools
from izmir_mcp.breaker import BreakerState, breakers
from izmir_mcp.config import get_settings
from izmir_mcp.models import ErrorCode
from izmir_mcp.sources import eshot, pharmacy, transit_static

from .conftest import load

ARRIVALS = "/api/iztek/duragayaklasanotobusler"


@pytest.fixture
def clock(monkeypatch):
    """Devre kesicinin saatini elle ilerletmek için."""
    now = [1000.0]
    monkeypatch.setattr(breaker, "_monotonic", lambda: now[0])
    monkeypatch.setattr(get_settings(), "breaker_failure_threshold", 3)
    monkeypatch.setattr(get_settings(), "breaker_cooldown_seconds", 60.0)

    def advance(seconds: float) -> None:
        now[0] += seconds

    return advance


def _eshot_calls(fake) -> int:
    return sum(1 for c in fake.calls if c.startswith(ARRIVALS))


async def test_opens_after_threshold_and_stops_calling_source(fake, clock):
    fake.routes[ARRIVALS] = httpx.ConnectError("down")
    for _ in range(3):
        r = await tools.get_stop_arrivals("40010")
        assert r.error.code == ErrorCode.SOURCE_UNAVAILABLE
    assert _eshot_calls(fake) == 3
    assert breakers.health(eshot.SOURCE).state == BreakerState.OPEN

    r = await tools.get_stop_arrivals("40010")
    assert _eshot_calls(fake) == 3  # devre açık: kaynağa hiç gidilmedi
    assert r.error.code == ErrorCode.CIRCUIT_OPEN
    assert r.error.retryable
    assert "kapalı" in r.error.message


async def test_half_open_trial_success_closes_circuit(fake, clock):
    fake.routes[ARRIVALS] = httpx.ConnectError("down")
    for _ in range(3):
        await tools.get_stop_arrivals("40010")

    fake.routes[ARRIVALS] = (200, load("arrivals_40010.json"))  # kaynak geri geldi
    clock(30)
    r = await tools.get_stop_arrivals("40010")
    assert r.error.code == ErrorCode.CIRCUIT_OPEN  # bekleme süresi dolmadı

    clock(31)
    r = await tools.get_stop_arrivals("40010")
    assert r.error is None and r.arrivals
    h = breakers.health(eshot.SOURCE)
    assert h.state == BreakerState.CLOSED and h.consecutive_failures == 0


async def test_half_open_trial_failure_reopens_immediately(fake, clock):
    fake.routes[ARRIVALS] = httpx.ConnectError("down")
    for _ in range(3):
        await tools.get_stop_arrivals("40010")
    clock(61)
    r = await tools.get_stop_arrivals("40010")  # tek deneme isteği, yine hata
    assert r.error.code == ErrorCode.SOURCE_UNAVAILABLE
    assert breakers.health(eshot.SOURCE).state == BreakerState.OPEN
    calls = _eshot_calls(fake)
    r = await tools.get_stop_arrivals("40010")
    assert r.error.code == ErrorCode.CIRCUIT_OPEN and _eshot_calls(fake) == calls


async def test_half_open_allows_single_trial():
    breakers.reset()
    for _ in range(get_settings().breaker_failure_threshold):
        breakers.record_failure("X", "down")
    breakers.health("X").opened_at = -1e9  # bekleme süresi çoktan doldu
    assert breakers.allow("X") is True
    assert breakers.allow("X") is False  # aynı anda ikinci deneme yok
    breakers.record_success("X")
    assert breakers.allow("X") is True
    breakers.reset()


async def test_not_found_does_not_trip(fake, clock):
    for _ in range(5):
        r = await tools.get_stop_arrivals("40010")  # FakeIzmir tanımsız yola 404 döner
        assert r.error.code == ErrorCode.NOT_FOUND
    assert breakers.health(eshot.SOURCE).state == BreakerState.CLOSED


async def test_success_resets_failure_count(fake, clock):
    fake.routes[ARRIVALS] = httpx.ConnectError("down")
    await tools.get_stop_arrivals("40010")
    await tools.get_stop_arrivals("40010")
    fake.routes[ARRIVALS] = (200, load("arrivals_40010.json"))
    await tools.get_stop_arrivals("40010")
    fake.routes[ARRIVALS] = httpx.ConnectError("down")
    await tools.get_stop_arrivals("40010")
    assert breakers.health(eshot.SOURCE).state == BreakerState.CLOSED  # art arda değil: 2 + başarı + 1


async def test_open_circuit_still_serves_stale_cache(fake, clock):
    fake.routes["/api/ibb/nobetcieczaneler"] = (200, load("nobetci.json"))
    first = await tools.find_pharmacies("Buca")
    from izmir_mcp.cache import cache

    for k, (_exp, v) in list(cache._data.items()):  # süreyi doldur
        cache._data[k] = (0, v)
    fake.routes["/api/ibb/nobetcieczaneler"] = httpx.ConnectError("down")
    for _ in range(4):
        r = await tools.find_pharmacies("Buca")
        assert r.error is None and r.meta.stale
    assert breakers.health(pharmacy.SOURCE).state == BreakerState.OPEN
    assert [p.name for p in r.pharmacies] == [p.name for p in first.pharmacies]


async def test_open_circuit_without_cache_returns_error(fake, clock):
    fake.routes["/api/ibb/nobetcieczaneler"] = httpx.ConnectError("down")
    for _ in range(3):
        await tools.find_pharmacies("Buca")
    r = await tools.find_pharmacies("Buca")
    assert r.error.code == ErrorCode.CIRCUIT_OPEN
    assert r.error.source == pharmacy.SOURCE


async def test_source_status_reports_up_and_down(fake, clock):
    fake.routes["/api/iztek/duragayaklasanotobusler"] = (200, [])
    fake.routes["/api/ibb/cbs/noktayayakinduraklar"] = (200, load("nearby_bornova.json"))
    fake.routes["/api/ibb/nobetcieczaneler"] = httpx.ConnectError("down")
    fake.routes["/api/metro/istasyonlar"] = (200, [])
    fake.routes["/api/izban/istasyonlar"] = (200, [])
    fake.routes["/api/izdeniz/iskeleler"] = httpx.ConnectError("down")
    for _ in range(3):
        await tools.find_pharmacies("Buca")  # eczane kaynağının devresini aç

    r = await tools.get_source_status()
    by = {s.source: s for s in r.sources}
    assert by[eshot.SOURCE].state == "up"
    assert by[transit_static.SOURCE_METRO].state == "up"
    assert by[pharmacy.SOURCE].state == "down"
    assert by[pharmacy.SOURCE].retry_in_seconds == 60
    assert by[transit_static.SOURCE_IZDENIZ].state == "degraded"  # tek hata, eşik aşılmadı
    assert not r.all_up
    assert any(pharmacy.SOURCE in n for n in r.meta.notes)
    assert sum(1 for c in fake.calls if c == "/api/ibb/nobetcieczaneler") == 3  # kapalı kaynak yoklanmadı


async def test_source_status_without_probe_does_not_call_sources(fake, clock):
    r = await tools.get_source_status(probe=False)
    assert fake.calls == []
    assert all(s.state == "unknown" for s in r.sources)
