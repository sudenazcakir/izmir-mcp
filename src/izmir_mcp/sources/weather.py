"""Hava durumu: Open-Meteo Forecast (anahtarsız, saatlik). Hava kalitesiyle aynı sağlayıcı."""

from typing import Any

from ..config import get_settings
from ..http import get_json_cached

SOURCE = "Open-Meteo Forecast"
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
