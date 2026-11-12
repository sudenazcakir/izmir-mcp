"""Metro, İZBAN ve İZDENİZ istasyon/iskele listeleri (statik, günde bir kez yeterli)."""

import json
import logging
from functools import lru_cache
from importlib import resources

from ..config import get_settings
from ..http import SourceFailure, get_json_cached
from ..models import Pier, Station
from ..text import parse_float

DAY = 24 * 3600
log = logging.getLogger(__name__)


@lru_cache
def _snapshot() -> dict:
    """Paketle gelen anlık görüntü: API çökerse istasyon/iskele koordinatları yine de elde olsun."""
    return json.loads(resources.files("izmir_mcp.data").joinpath("stations_snapshot.json").read_text("utf-8"))


async def stations_or_snapshot(kind: str) -> list[Station] | list[Pier]:
    loader = {"metro": metro_stations, "izban": izban_stations, "piers": piers}[kind]
    try:
        return await loader()
    except SourceFailure:
        log.info("%s live list unavailable, using bundled snapshot", kind)
        model = Pier if kind == "piers" else Station
        return [model(**x) for x in _snapshot()[kind]]


async def metro_stations() -> list[Station]:
    base = get_settings().izmir_api_base
    data, _, _ = await get_json_cached(f"{base}/api/metro/istasyonlar", source="İzmir Metro", ttl=DAY)
    return [
        Station(system="metro", source_id=s["IstasyonId"], name=s["Adi"], lat=parse_float(s["Enlem"]),
                lon=parse_float(s["Boylam"]), seq=s.get("Sira"), active=s.get("AktifMi", True))
        for s in data
    ]


async def izban_stations() -> list[Station]:
    base = get_settings().izmir_api_base
    data, _, _ = await get_json_cached(f"{base}/api/izban/istasyonlar", source="İZBAN", ttl=DAY)
    return [
        Station(system="izban", source_id=s["IstasyonId"], name=s["IstasyonAdi"], lat=parse_float(s["Enlem"]),
                lon=parse_float(s["Boylam"]), seq=s.get("IstasyonSirasi"))
        for s in data
    ]


async def piers() -> list[Pier]:
    base = get_settings().izmir_api_base
    data, _, _ = await get_json_cached(f"{base}/api/izdeniz/iskeleler", source="İZDENİZ", ttl=DAY)
    return [
        Pier(id=p["IskeleId"], name=p["Adi"], lat=parse_float(p["Enlem"]), lon=parse_float(p["Boylam"]),
             car_ferry=bool(p.get("ArabaliVapurIskelesiMi")), active=p.get("AktifMi", True))
        for p in data
    ]
