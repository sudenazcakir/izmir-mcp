"""Hava durumu: Open-Meteo Forecast (anahtarsız, saatlik). Hava kalitesiyle aynı sağlayıcı.

Render'ın ücretsiz katmanında çıkış IP'si başka uygulamalarla paylaşılıyor; Open-Meteo'nun IP başına ücretsiz
kotası o yüzden bizim dışımızda dolabiliyor (429). O zaman MET Norway (yr.no) verisi aynı biçime çevrilip kullanılır.
"""

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import get_settings
from ..http import SourceFailure, get_json_cached

log = logging.getLogger(__name__)

SOURCE = "Open-Meteo Forecast"
SOURCE_MET = "MET Norway"
ISTANBUL_OFFSET = 3 * 3600  # Türkiye 2016'dan beri yaz saati uygulamıyor: sabit UTC+3
HOURLY = [
    "temperature_2m", "apparent_temperature", "precipitation_probability", "precipitation",
    "weather_code", "wind_speed_10m", "wind_gusts_10m", "uv_index", "relative_humidity_2m",
]
CURRENT = ["temperature_2m", "apparent_temperature", "precipitation", "weather_code", "wind_speed_10m",
           "relative_humidity_2m"]

# WMO hava kodları -> Türkçe kısa açıklama
WMO: dict[int, str] = {
    0: "açık", 1: "çoğunlukla açık", 2: "parçalı bulutlu", 3: "kapalı",
    45: "sisli", 48: "kırağılı sis",
    51: "hafif çisenti", 53: "çisenti", 55: "yoğun çisenti", 56: "dondurucu çisenti", 57: "dondurucu çisenti",
    61: "hafif yağmur", 63: "yağmur", 65: "kuvvetli yağmur", 66: "dondurucu yağmur", 67: "dondurucu yağmur",
    71: "hafif kar", 73: "kar", 75: "yoğun kar", 77: "kar taneleri",
    80: "hafif sağanak", 81: "sağanak", 82: "şiddetli sağanak", 85: "kar sağanağı", 86: "yoğun kar sağanağı",
    95: "gök gürültülü fırtına", 96: "dolulu fırtına", 99: "şiddetli dolulu fırtına",
}


def describe(code: Any) -> str | None:
    return WMO.get(int(code)) if isinstance(code, int | float) else None


async def forecast(lat: float, lon: float) -> tuple[dict[str, Any], bool, bool]:
    """Open-Meteo biçiminde veri döner; Open-Meteo çökerse MET Norway'e düşer (veride "_source" alanı olur)."""
    try:
        return await _open_meteo(lat, lon)
    except SourceFailure as first:
        log.warning("Open-Meteo kullanılamadı (%s), MET Norway deneniyor", first.error.code)
        try:
            raw, cached, stale = await get_json_cached(
                f"{get_settings().met_no_base}/weatherapi/locationforecast/2.0/complete",
                source=SOURCE_MET, ttl=30 * 60, params={"lat": f"{lat:.4f}", "lon": f"{lon:.4f}"},
            )
        except SourceFailure:
            raise first from None
        data = from_met(raw)
        if not (data.get("hourly") or {}).get("time"):
            raise first from None
        return data, cached, stale


async def _open_meteo(lat: float, lon: float) -> tuple[dict[str, Any], bool, bool]:
    base = get_settings().open_meteo_forecast_base
    data, cached, stale = await get_json_cached(
        f"{base}/v1/forecast",
        source=SOURCE,
        ttl=30 * 60,
        params={
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
            "current": ",".join(CURRENT), "hourly": ",".join(HOURLY),
            "timezone": "Europe/Istanbul", "forecast_days": "2", "wind_speed_unit": "kmh",
        },
    )
    return (data if isinstance(data, dict) else {}), cached, stale


# MET Norway sembol kodu (son ek _day/_night/_polartwilight atılmış) -> WMO kodu
MET_SYMBOLS: dict[str, int] = {
    "clearsky": 0, "fair": 1, "partlycloudy": 2, "cloudy": 3, "fog": 45,
    "lightrain": 61, "rain": 63, "heavyrain": 65,
    "lightrainshowers": 80, "rainshowers": 81, "heavyrainshowers": 82,
    "lightsleet": 66, "sleet": 66, "heavysleet": 67,
    "lightsleetshowers": 66, "sleetshowers": 66, "heavysleetshowers": 67,
    "lightsnow": 71, "snow": 73, "heavysnow": 75,
    "lightsnowshowers": 85, "snowshowers": 85, "heavysnowshowers": 86,
}


def met_code(symbol: Any) -> int | None:
    if not isinstance(symbol, str):
        return None
    base = symbol.split("_")[0]
    if "thunder" in base:
        return 95
    return MET_SYMBOLS.get(base)


def feels_like(t: float | None, wind_kmh: float | None, rh: float | None) -> float | None:
    """Hissedilen sıcaklık: soğukta rüzgâr soğuğu, sıcakta nem etkisi (MET Norway bunu vermiyor)."""
    if t is None:
        return None
    if t <= 10 and wind_kmh is not None and wind_kmh > 4.8:
        v = wind_kmh ** 0.16
        return round(13.12 + 0.6215 * t - 11.37 * v + 0.3965 * t * v, 1)
    if t >= 27 and rh is not None:
        e = rh / 100 * 6.105 * math.exp(17.27 * t / (237.7 + t))  # Steadman / Avustralya formülü (rüzgârsız)
        return round(t + 0.33 * e - 4.0, 1)
    return t


def from_met(raw: Any) -> dict[str, Any]:
    """MET Norway locationforecast/complete cevabını Open-Meteo'nun current/hourly biçimine çevirir."""
    series = ((raw or {}).get("properties") or {}).get("timeseries") if isinstance(raw, dict) else None
    hourly: dict[str, list[Any]] = {k: [] for k in ["time", *HOURLY]}
    current: dict[str, Any] = {}
    for item in series or []:
        try:
            t = datetime.fromisoformat(item["time"].replace("Z", "+00:00")).astimezone(UTC)
            data = item.get("data") or {}
            inst = (data.get("instant") or {}).get("details") or {}
            n1 = data.get("next_1_hours")
        except (KeyError, AttributeError, TypeError, ValueError):
            continue
        if not isinstance(n1, dict):  # 2-3 gün sonrası 6 saatlik; bize ilk 2 günün saatlikleri yeter
            continue
        temp = inst.get("air_temperature")
        wind = inst.get("wind_speed")
        gust = inst.get("wind_speed_of_gust")
        wind_kmh = round(wind * 3.6, 1) if isinstance(wind, int | float) else None
        rh = inst.get("relative_humidity")
        det = n1.get("details") or {}
        code = met_code((n1.get("summary") or {}).get("symbol_code"))
        app = feels_like(temp, wind_kmh, rh) if isinstance(temp, int | float) else None
        local = t + timedelta(seconds=ISTANBUL_OFFSET)
        hourly["time"].append(local.strftime("%Y-%m-%dT%H:00"))
        hourly["temperature_2m"].append(temp)
        hourly["apparent_temperature"].append(app)
        hourly["precipitation_probability"].append(det.get("probability_of_precipitation"))
        hourly["precipitation"].append(det.get("precipitation_amount"))
        hourly["weather_code"].append(code)
        hourly["wind_speed_10m"].append(wind_kmh)
        hourly["wind_gusts_10m"].append(round(gust * 3.6, 1) if isinstance(gust, int | float) else None)
        hourly["uv_index"].append(inst.get("ultraviolet_index_clear_sky"))
        hourly["relative_humidity_2m"].append(rh)
        if not current:
            current = {"temperature_2m": temp, "apparent_temperature": app,
                       "precipitation": det.get("precipitation_amount"), "weather_code": code,
                       "wind_speed_10m": wind_kmh, "relative_humidity_2m": rh}
    return {"utc_offset_seconds": ISTANBUL_OFFSET, "current": current, "hourly": hourly, "_source": SOURCE_MET}
