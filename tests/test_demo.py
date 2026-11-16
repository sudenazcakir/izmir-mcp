"""Demo c): bir kaynak kapalıyken araçlar uydurmadan "şu an veri alınamıyor" demeli.

DEMO_DOWN_SOURCES ile kaynak gerçekten çökmeden aynı yol (hata -> devre kesici -> circuit_open) çalıştırılır.
"""

import pytest

from izmir_mcp import http, tools
from izmir_mcp.breaker import BreakerState, breakers
from izmir_mcp.config import get_settings
from izmir_mcp.models import ErrorCode
from izmir_mcp.sources import eshot, pharmacy

from .conftest import load

ARRIVALS = "/api/iztek/duragayaklasanotobusler"


@pytest.fixture
def eshot_down(fake, monkeypatch):
    fake.routes[ARRIVALS] = (200, load("arrivals_40010.json"))  # kaynak aslında ayakta
    fake.routes["/api/ibb/nobetcieczaneler"] = (200, load("nobetci.json"))
    monkeypatch.setattr(get_settings(), "demo_down_sources", " eshot , İZBAN")
    monkeypatch.setattr(get_settings(), "breaker_failure_threshold", 3)
    return fake


def test_demo_down_matching(eshot_down):
    assert http.demo_down("ESHOT") and http.demo_down("İZBAN") and http.demo_down("izban")
    assert not http.demo_down("ESHOT Hat Duyuruları")  # ad tam eşleşmeli
    assert not http.demo_down(pharmacy.SOURCE)


def test_demo_down_off_by_default(fake):
    assert not http.demo_down("ESHOT")


async def test_source_reported_unavailable_without_calling_it(eshot_down):
    r = await tools.get_stop_arrivals("40010")
    assert r.arrivals == []
    assert r.error.code == ErrorCode.SOURCE_UNAVAILABLE and r.error.retryable
    assert "cevap vermiyor" in r.error.message
    assert not any(c.startswith(ARRIVALS) for c in eshot_down.calls)


async def test_repeated_failures_open_the_circuit(eshot_down):
    for _ in range(3):
        await tools.get_stop_arrivals("40010")
    assert breakers.health(eshot.SOURCE).state == BreakerState.OPEN
    r = await tools.get_stop_arrivals("40010")
    assert r.error.code == ErrorCode.CIRCUIT_OPEN
    assert "kapalı" in r.error.message and "sn sonra" in r.error.message

    status = await tools.get_source_status()
    by = {s.source: s for s in status.sources}
    assert by[eshot.SOURCE].state == "down" and by[eshot.SOURCE].retry_in_seconds
    assert not status.all_up
    assert any("ESHOT" in n for n in status.meta.notes)


async def test_other_sources_keep_working(eshot_down):
    r = await tools.find_pharmacies("Buca")
    assert r.error is None and r.pharmacies


async def test_recovers_when_switch_is_turned_off(eshot_down, monkeypatch):
    for _ in range(3):
        await tools.get_stop_arrivals("40010")
    monkeypatch.setattr(get_settings(), "demo_down_sources", "")
    h = breakers.health(eshot.SOURCE)
    h.opened_at -= get_settings().breaker_cooldown_seconds  # bekleme süresi doldu
    r = await tools.get_stop_arrivals("40010")
    assert r.error is None and r.arrivals
    assert breakers.health(eshot.SOURCE).state == BreakerState.CLOSED
