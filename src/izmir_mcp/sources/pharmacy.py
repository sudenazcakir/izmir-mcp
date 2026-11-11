"""Eczaneler: nöbetçi listesi (her sabah 08:00'de yenilenir) ve tüm eczaneler."""

from typing import Any

from ..config import get_settings
from ..http import get_json_cached
from ..models import Pharmacy
from ..text import parse_float

SOURCE = "İzmir Eczacı Odası / İBB"


def _to_model(p: dict[str, Any], on_duty: bool) -> Pharmacy:
    return Pharmacy(
        name=(p.get("Adi") or "").strip(),
        address=(p.get("Adres") or "").strip() or None,
        phone=(p.get("Telefon") or "").strip() or None,
        region=(p.get("Bolge") or "").strip() or None,
        region_note=(p.get("BolgeAciklama") or "").strip() or None,
        lat=parse_float(p.get("LokasyonX")),  # LokasyonX = enlem
        lon=parse_float(p.get("LokasyonY")),  # LokasyonY = boylam
        on_duty=on_duty,
    )


async def on_duty_pharmacies() -> tuple[list[Pharmacy], bool, bool]:
    base = get_settings().izmir_api_base
    data, cached, stale = await get_json_cached(f"{base}/api/ibb/nobetcieczaneler", source=SOURCE, ttl=15 * 60)
    return [_to_model(p, True) for p in data], cached, stale


async def all_pharmacies() -> tuple[list[Pharmacy], bool, bool]:
    base = get_settings().izmir_api_base
    data, cached, stale = await get_json_cached(f"{base}/api/ibb/eczaneler", source=SOURCE, ttl=24 * 3600)
    return [_to_model(p, False) for p in data], cached, stale


