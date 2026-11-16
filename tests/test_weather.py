from datetime import UTC, datetime

import httpx
import pytest

from izmir_mcp import tools
from izmir_mcp.models import DayPeriod

from .conftest import load

FORECAST = "/v1/forecast"
AIR = "/v1/air-quality"


def forecast_json():
    times = [f"2026-11-{15 + h // 24:02d}T{h % 24:02d}:00" for h in range(48)]
    # 15 Kasım: serin sabah, ılık öğle; akşam yağmurlu ve rüzgârlı. 16 Kasım: soğuk.
    temp = [10 + (6 if 12 <= h % 24 <= 16 else 0) - (8 if h >= 24 else 0) for h in range(48)]
    code = [61 if 18 <= h <= 21 else 2 for h in range(48)]
    rain = [70 if 18 <= h <= 21 else 5 for h in range(48)]
    return {
        "utc_offset_seconds": 10800,
        "current": {"temperature_2m": 15.2, "apparent_temperature": 13.9, "precipitation": 0.0,
                    "weather_code": 2, "wind_speed_10m": 12.0, "relative_humidity_2m": 60},
        "hourly": {
            "time": times, "temperature_2m": temp, "apparent_temperature": [t - 2 for t in temp],
            "precipitation_probability": rain, "precipitation": [1.2 if r == 70 else 0 for r in rain],
            "weather_code": code, "wind_speed_10m": [15] * 48,
            "wind_gusts_10m": [40 if 18 <= h <= 21 else 20 for h in range(48)],
            "uv_index": [3] * 48, "relative_humidity_2m": [60] * 48,
        },
    }


@pytest.fixture
def wx(fake, monkeypatch):
    fake.routes[FORECAST] = (200, forecast_json())
    fake.routes[AIR] = (200, load("openmeteo_air.json"))
    monkeypatch.setattr(tools, "_now", lambda: datetime(2026, 11, 15, 6, 0, tzinfo=UTC))  # İstanbul 09:00
    return fake


async def test_today_periods_with_clothing(wx):
    r = await tools.get_weather("Bornova")
    assert r.error is None and r.place == "Bornova" and r.date == "2026-11-15"
    assert r.now.temperature_c == 15.2 and r.now.description == "parçalı bulutlu"
    names = [p.name for p in r.periods]
    assert names == ["sabah", "öğle", "akşam"]
    sabah, ogle, aksam = r.periods
    assert sabah.feels_like_min_c == 8 and "kalın mont, kazak" in sabah.clothing
    assert ogle.temp_max_c == 16
    assert aksam.description == "hafif yağmur" and aksam.rain_chance_pct == 70
    assert any("şemsiye" in c for c in aksam.clothing) and any("rüzgârlık" in c for c in aksam.clothing)
    assert sabah.european_aqi_max is not None and sabah.aqi_level is not None


async def test_past_periods_are_dropped(wx, monkeypatch):
    monkeypatch.setattr(tools, "_now", lambda: datetime(2026, 11, 15, 15, 0, tzinfo=UTC))  # İstanbul 18:00
    r = await tools.get_weather("Bornova")
    assert [p.name for p in r.periods] == ["akşam"]


async def test_tomorrow(wx):
    r = await tools.get_weather("Bornova", day="yarın")
    assert r.date == "2026-11-16" and len(r.periods) == 3
    assert "kaban, atkı, bere ve eldiven" in r.periods[0].clothing


async def test_air_failure_still_gives_weather(wx):
    wx.routes[AIR] = httpx.ConnectError("down")
    r = await tools.get_weather("Bornova")
    assert r.error is None and r.periods and r.periods[0].european_aqi_max is None
    assert any("Hava kalitesi" in n for n in r.meta.notes)


async def test_weather_down_returns_error(wx):
    wx.routes[FORECAST] = httpx.ConnectError("down")
    r = await tools.get_weather("Bornova")
    assert r.error is not None and r.periods == []


async def test_unknown_district(wx):
    r = await tools.get_weather("Narnia")
    assert r.error is not None and r.error.code == "invalid_input"


def test_clothing_rules():
    hot = DayPeriod(name="öğle", hours="12-16", temp_min_c=30, temp_max_c=33, feels_like_min_c=31, uv_max=8)
    assert "tişört, ince ve açık renkli giysiler" in tools.clothing_for(hot)
    assert any("güneş kremi" in c for c in tools.clothing_for(hot))
    smog = DayPeriod(name="sabah", hours="07-11", feels_like_min_c=18, european_aqi_max=75)
    assert any("maske" in c for c in tools.clothing_for(smog))
