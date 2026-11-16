"""Hava kalitesi kaynakları.

- Belediye istasyonları (`/api/ibb/cevre/havadegerleri`): gerçek ölçüm ama yalnızca GÜNLÜK ortalama, yalnızca
  PM10 ve SO2, ve genelde 1-3 gün geriden geliyor. Bazı değerler boş string olarak dönüyor.
- Open-Meteo Air Quality (anahtarsız): saatlik değerler ve Avrupa Hava Kalitesi İndeksi. Ölçüm değil,
  CAMS Avrupa hava kalitesi modelinin tahmini (yaklaşık 11 km çözünürlük).
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import get_settings
from ..http import get_json_cached
from ..text import norm, parse_float

SOURCE_IBB = "İBB Hava Kalitesi İstasyonları"
SOURCE_OPEN_METEO = "Open-Meteo Air Quality"
ISTANBUL = timezone(timedelta(hours=3))

# API istasyon koordinatı vermiyor. Konumlar istasyonların bilinen yerlerinden, yaklaşık (±1 km).
# anahtar: norm(BolgeAdi) -> (görünen ad, enlem, boylam)
STATIONS: dict[str, tuple[str, float, float]] = {
    "alsancak": ("Alsancak", 38.4352, 27.1436),
    "bornova": ("Bornova", 38.4557, 27.2133),
    "guzelyali": ("Güzelyalı", 38.3957, 27.0806),
    "karsiyaka": ("Karşıyaka", 38.4590, 27.1130),
    "sirinyer": ("Şirinyer", 38.3925, 27.1413),
    "cigli": ("Çiğli", 38.4962, 27.0614),
    "bayrakli": ("Bayraklı", 38.4628, 27.1665),
    "guzelbahce": ("Güzelbahçe", 38.3710, 26.8890),
    "bergama": ("Bergama", 39.1210, 27.1790),
}

# Open-Meteo değişken adı -> gösterilen kirletici adı
POLLUTANTS: dict[str, str] = {
    "pm2_5": "PM2.5",
    "pm10": "PM10",
    "nitrogen_dioxide": "NO2",
    "sulphur_dioxide": "SO2",
    "ozone": "O3",
    "carbon_monoxide": "CO",
}
OPEN_METEO_VARS = ["european_aqi", *POLLUTANTS]


def aqi_level(aqi: float | None) -> str | None:
    """Avrupa AQI bantları (EEA): 0-20 iyi, 20-40 makul, 40-60 orta, 60-80 kötü, 80-100 çok kötü, 100+ son derece kötü."""
    if aqi is None:
        return None
    for limit, label in ((20, "iyi"), (40, "makul"), (60, "orta"), (80, "kötü"), (100, "çok kötü")):
        if aqi < limit:
            return label
    return "son derece kötü"


async def station_daily() -> tuple[list[dict[str, Any]], bool, bool]:
    """Belediye istasyonlarının son günlük ortalamaları. Boş ölçümler atlanır."""
    base = get_settings().izmir_api_base
    data, cached, stale = await get_json_cached(f"{base}/api/ibb/cevre/havadegerleri", source=SOURCE_IBB, ttl=3600)
    out: list[dict[str, Any]] = []
    for r in data if isinstance(data, list) else []:
        value = parse_float(r.get("OlcumDegeri"))
        raw_name = (r.get("BolgeAdi") or "").strip()
        try:
            measured = datetime.fromisoformat(r.get("OlcumTarihi") or "").replace(tzinfo=ISTANBUL)
        except ValueError:
            continue
        if value is None or not raw_name:
            continue
        name, lat, lon = STATIONS.get(norm(raw_name), (raw_name.title(), None, None))
        out.append({
            "key": norm(raw_name), "station": name, "lat": lat, "lon": lon,
            "pollutant": (r.get("GazAdi") or "").strip(), "value": value, "measured_at": measured,
        })
    return out, cached, stale


async def open_meteo(lat: float, lon: float) -> tuple[dict[str, Any], bool, bool]:
    """Open-Meteo'dan şu anki değerler + bugün ve yarının saatlik tahmini (Europe/Istanbul saatiyle)."""
    base = get_settings().open_meteo_air_base
    variables = ",".join(OPEN_METEO_VARS)
    data, cached, stale = await get_json_cached(
        f"{base}/v1/air-quality",
        source=SOURCE_OPEN_METEO,
        ttl=15 * 60,
        params={
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
            "current": variables, "hourly": variables,
            "timezone": "Europe/Istanbul", "forecast_days": "2",
        },
    )
    return (data if isinstance(data, dict) else {}), cached, stale


def parse_local_time(value: str, utc_offset_seconds: int | None) -> datetime:
    """Open-Meteo saatleri istenen saat diliminde, ofsetsiz döner: '2026-11-15T17:00'."""
    tz = timezone(timedelta(seconds=utc_offset_seconds)) if utc_offset_seconds is not None else ISTANBUL
    return datetime.fromisoformat(value).replace(tzinfo=tz)
