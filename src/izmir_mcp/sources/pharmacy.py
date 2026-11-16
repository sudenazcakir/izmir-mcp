"""Eczaneler: nöbetçi listesi (her sabah 08:00'de yenilenir) ve tüm eczaneler."""

from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import get_settings
from ..http import get_json_cached
from ..models import Pharmacy
from ..text import parse_float

SOURCE = "İzmir Eczacı Odası / İBB"
ISTANBUL = timezone(timedelta(hours=3))
DUTY_CHANGE_HOUR = 8  # nöbet listesi her sabah 08:00'de değişir
ON_DUTY_TTL_SECONDS = 15 * 60


def duty_day_start(now: datetime | None = None) -> datetime:
    """Şu an geçerli nöbet gününün başlangıcı: bugün 08:00, saat 08:00'den önceyse dün 08:00."""
    now = (now or datetime.now(ISTANBUL)).astimezone(ISTANBUL)
    start = now.replace(hour=DUTY_CHANGE_HOUR, minute=0, second=0, microsecond=0)
    return start if now >= start else start - timedelta(days=1)


def duty_ttl(now: datetime | None = None) -> float:
    """Önbellek süresi nöbet değişimini (08:00) geçmesin: 07:55'te alınan liste 08:05'te taze sayılmasın."""
    now = (now or datetime.now(ISTANBUL)).astimezone(ISTANBUL)
    left = (duty_day_start(now) + timedelta(days=1) - now).total_seconds()
    return max(1.0, min(ON_DUTY_TTL_SECONDS, left))


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
    """Bugünün nöbetçileri. Kaynak çökerse yalnız AYNI nöbet gününde alınmış liste "eski veri" olarak
    gösterilir; önceki günün listesi (Redis'te 2 gün dursa bile) kullanılmaz, çünkü bugün yanlış olur."""
    base = get_settings().izmir_api_base
    now = datetime.now(ISTANBUL)
    data, cached, stale = await get_json_cached(
        f"{base}/api/ibb/nobetcieczaneler", source=SOURCE, ttl=duty_ttl(now),
        stale_not_before=duty_day_start(now).timestamp())
    return [_to_model(p, True) for p in data], cached, stale


async def all_pharmacies() -> tuple[list[Pharmacy], bool, bool]:
    base = get_settings().izmir_api_base
    data, cached, stale = await get_json_cached(f"{base}/api/ibb/eczaneler", source=SOURCE, ttl=24 * 3600)
    return [_to_model(p, False) for p in data], cached, stale


