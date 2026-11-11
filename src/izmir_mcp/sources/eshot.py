"""ESHOT canlı otobüs verisi (openapi.izmir.bel.tr, anahtarsız)."""

from typing import Any

from ..config import get_settings
from ..http import get_json, get_json_cached
from ..text import parse_float

SOURCE = "ESHOT"
SOURCE_STOPS = "İBB CBS (yakın duraklar)"


async def approaching_buses(stop_id: str) -> list[dict[str, Any]]:
    """Durağa yaklaşan otobüsler. Alanlar: HatNumarasi, HatAdi, KalanDurakSayisi, KoorX(enlem), KoorY(boylam)…"""
    base = get_settings().izmir_api_base
    data = await get_json(f"{base}/api/iztek/duragayaklasanotobusler/{stop_id}", source=SOURCE)
    return data if isinstance(data, list) else []


async def nearby_stops(lat: float, lon: float) -> list[dict[str, Any]]:
    """Noktaya en yakın ~16 durak. Not: x=boylam, y=enlem ve EPSG:4326 verilmezse sonuç projeksiyonlu gelir."""
    base = get_settings().izmir_api_base
    data, _, _ = await get_json_cached(
        f"{base}/api/ibb/cbs/noktayayakinduraklar",
        source=SOURCE_STOPS,
        ttl=24 * 3600,
        params={"x": f"{lon:.6f}", "y": f"{lat:.6f}", "inCoordSys": "EPSG:4326", "outCoordSys": "EPSG:4326"},
    )
    out = []
    for s in data if isinstance(data, list) else []:
        out.append({
            "id": str(s.get("durakId")),
            "name": s.get("adi"),
            "lat": parse_float(s.get("enlem")),
            "lon": parse_float(s.get("boylam")),
            "distance_m": parse_float(s.get("mesafe")),
        })
    return out
