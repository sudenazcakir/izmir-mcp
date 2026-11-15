from datetime import UTC, datetime

import httpx
import pytest

from izmir_mcp import tools
from izmir_mcp.models import ErrorCode
from izmir_mcp.sources import air

from .conftest import load

IBB = "/api/ibb/cevre/havadegerleri"
OPEN_METEO = "/v1/air-quality"


@pytest.fixture
def air_ok(fake, monkeypatch):
    fake.routes[IBB] = (200, load("havadegerleri.json"))  # 2026-11-12 tarihli günlük ortalamalar
    fake.routes[OPEN_METEO] = (200, load("openmeteo_air.json"))  # şu an 2026-11-15 17:00
    monkeypatch.setattr(tools, "_now", lambda: datetime(2026, 11, 15, 14, 0, tzinfo=UTC))  # İstanbul 17:00
    return fake


def test_aqi_levels():
    assert air.aqi_level(None) is None
    assert air.aqi_level(12) == "iyi"
    assert air.aqi_level(35) == "makul"
    assert air.aqi_level(42) == "orta"
    assert air.aqi_level(75) == "kötü"
    assert air.aqi_level(95) == "çok kötü"
    assert air.aqi_level(130) == "son derece kötü"


async def test_bornova_combines_model_and_station(air_ok):
    r = await tools.get_air_quality("Bornova'da")
    assert r.error is None and r.district == "Bornova"
    assert r.european_aqi == 42 and r.level == "orta"

    current = {x.pollutant: x for x in r.current}
    assert current["PM2.5"].value == 16.0
    assert {"PM2.5", "PM10", "NO2", "SO2", "O3", "CO"} == set(current)
    assert all(x.resolution == "hourly" and x.source == air.SOURCE_OPEN_METEO for x in r.current)

    daily = {x.pollutant: x.value for x in r.station_daily}
    assert daily == {"PM10": 34.0, "SO2": 6.0}
    assert all(x.station == "Bornova" and x.resolution == "daily" for x in r.station_daily)
    assert r.station_daily[0].distance_km < 3
    assert any("3 gün önce" in n for n in r.meta.notes)
    assert any("model" in n for n in r.meta.notes)


async def test_outlook_is_only_future_hours(air_ok):
    r = await tools.get_air_quality("Bornova")
    assert [h.time.hour for h in r.outlook] == [18, 19, 20, 21, 22, 23]
    assert r.outlook[0].european_aqi == 45 and r.outlook[0].level == "orta"
    assert r.outlook[0].pm2_5 == 19.4
    short = await tools.get_air_quality("Bornova", hours=2)
    assert len(short.outlook) == 2


async def test_buca_uses_nearest_station_sirinyer(air_ok):
    r = await tools.get_air_quality("Buca")
    assert {x.station for x in r.station_daily} == {"Şirinyer"}


async def test_empty_station_values_are_skipped(air_ok):
    r = await tools.get_air_quality("Güzelbahçe")
    assert {x.pollutant for x in r.station_daily} == {"SO2"}  # PM10 boş geliyor


async def test_far_district_does_not_borrow_distant_station(air_ok):
    r = await tools.get_air_quality("Çeşme")
    assert r.station_daily == []
    assert r.european_aqi == 42  # model değeri yine var
    assert any("temsil etmediği" in n for n in r.meta.notes)


async def test_station_source_down_still_returns_model(air_ok):
    air_ok.routes[IBB] = httpx.ConnectError("down")
    r = await tools.get_air_quality("Bornova")
    assert r.error is None
    assert r.european_aqi == 42 and r.station_daily == []
    assert any("istasyonları şu an cevap vermiyor" in n for n in r.meta.notes)


async def test_model_source_down_still_returns_station(air_ok):
    air_ok.routes[OPEN_METEO] = httpx.ConnectError("down")
    r = await tools.get_air_quality("Bornova")
    assert r.error is None
    assert r.european_aqi is None and r.current == [] and r.outlook == []
    assert {x.pollutant for x in r.station_daily} == {"PM10", "SO2"}
    assert any("Open-Meteo şu an cevap vermiyor" in n for n in r.meta.notes)


async def test_both_sources_down_returns_error(air_ok):
    air_ok.routes[IBB] = httpx.ConnectError("down")
    air_ok.routes[OPEN_METEO] = httpx.ConnectError("down")
    r = await tools.get_air_quality("Bornova")
    assert r.error is not None
    assert r.error.code == ErrorCode.SOURCE_UNAVAILABLE


async def test_unknown_district_and_missing_input(fake):
    r = await tools.get_air_quality("Atlantis")
    assert r.error.code == ErrorCode.INVALID_INPUT and "Bornova" in r.error.hint
    r = await tools.get_air_quality()
    assert r.error.code == ErrorCode.INVALID_INPUT
    assert fake.calls == []


async def test_location_instead_of_district(air_ok):
    r = await tools.get_air_quality(lat=38.4352, lon=27.1436)  # Alsancak istasyonunun yanı
    assert r.district is None
    assert {x.station for x in r.station_daily} == {"Alsancak"}
    assert {x.pollutant for x in r.station_daily} == {"SO2"}  # Alsancak PM10 boş
