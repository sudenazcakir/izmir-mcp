"""ESHOT canlı otobüs verisi (openapi.izmir.bel.tr, anahtarsız)."""

import time
from typing import Any

from ..config import get_settings
from ..http import get_json, get_json_cached
from ..text import parse_float

SOURCE = "ESHOT"
SOURCE_STOPS = "İBB CBS (yakın duraklar)"


ARRIVALS_TTL = 10.0  # aynı durak için ESHOT'a en fazla 10 sn'de bir gidilir (sohbet + canlı harita ortak)
_ARRIVALS_MAX = 2000
_arrivals_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def arrivals_fresh(stop_id: str) -> bool:
    hit = _arrivals_cache.get(stop_id)
    return hit is not None and time.monotonic() - hit[0] < ARRIVALS_TTL


def clear_arrivals_cache() -> None:
    _arrivals_cache.clear()


async def approaching_buses(stop_id: str) -> list[dict[str, Any]]:
    """Durağa yaklaşan otobüsler. Alanlar: HatNumarasi, HatAdi, KalanDurakSayisi, KoorX(enlem), KoorY(boylam)…"""
    hit = _arrivals_cache.get(stop_id)
    if hit is not None and time.monotonic() - hit[0] < ARRIVALS_TTL:
        return hit[1]
    base = get_settings().izmir_api_base
    data = await get_json(f"{base}/api/iztek/duragayaklasanotobusler/{stop_id}", source=SOURCE)
    result = data if isinstance(data, list) else []
    if len(_arrivals_cache) >= _ARRIVALS_MAX:
        now = time.monotonic()
        for k in [k for k, (t, _) in _arrivals_cache.items() if now - t >= ARRIVALS_TTL]:
            del _arrivals_cache[k]
        if len(_arrivals_cache) >= _ARRIVALS_MAX:
            _arrivals_cache.clear()
    _arrivals_cache[stop_id] = (time.monotonic(), result)
    return result


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
