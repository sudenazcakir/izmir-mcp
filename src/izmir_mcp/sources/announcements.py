"""ESHOT hat duyuruları.

- Hat duyuruları CSV'si (openfiles.izmir.bel.tr): `HAT_NO;BASLIK;BASLAMA_TARIHI;BITIS_TARIHI`, noktalı virgüllü.
  Güzergah değişikliği gibi TARİHLİ duyurular. Bir duyuru birden çok hattı etkiliyorsa her hat için ayrı
  satır olarak yazılmış.
- HatlariGetir (acikveriwapi): her hattın adı ve `Duyuru` / `Aciklama` alanları. Bunlar tarihsiz, KALICI
  hat notları ("ring hat, saat uygulaması yok", "olumsuz havada şu duraklara uğramaz" gibi).
"""

import csv
import io
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import get_settings
from ..http import get_json_cached

SOURCE_CSV = "ESHOT Hat Duyuruları"
SOURCE_LINES = "ESHOT Hat Bilgileri"
ISTANBUL = timezone(timedelta(hours=3))


def line_code(text: str | None) -> str | None:
    """'302', '302 nolu hat', 'hat 302', ' 302 ' -> '302'. Rakam yoksa None."""
    m = re.search(r"\d+", text or "")
    if not m:
        return None
    return m.group(0).lstrip("0") or "0"


def _parse_time(value: str | None) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=ISTANBUL)
        except ValueError:
            continue
    return None


def parse_csv(text: str) -> list[dict[str, Any]]:
    rows = []
    for r in csv.DictReader(io.StringIO(text), delimiter=";"):
        r = {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}
        code, title = line_code(r.get("HAT_NO")), r.get("BASLIK") or ""
        starts = _parse_time(r.get("BASLAMA_TARIHI"))
        if not code or not title or starts is None:
            continue
        rows.append({"line": code, "title": title, "starts_at": starts, "ends_at": _parse_time(r.get("BITIS_TARIHI"))})
    return rows


async def csv_announcements() -> tuple[list[dict[str, Any]], bool, bool]:
    text, cached, stale = await get_json_cached(
        get_settings().eshot_announcements_csv_url, source=SOURCE_CSV, ttl=30 * 60, as_text=True)
    return parse_csv(text if isinstance(text, str) else ""), cached, stale


async def lines() -> tuple[dict[str, dict[str, Any]], bool, bool]:
    """Hat numarası -> {name, notes}. notes: Duyuru ve (farklıysa) Aciklama, boşlar atılmış."""
    base = get_settings().eshot_api_base
    data, cached, stale = await get_json_cached(
        f"{base}/AcikVeriEshot/Hatlar/HatlariGetir", source=SOURCE_LINES, ttl=6 * 3600)
    rows = data.get("Data") if isinstance(data, dict) else data
    out: dict[str, dict[str, Any]] = {}
    for r in rows if isinstance(rows, list) else []:
        code = line_code(str(r.get("HatNo") or ""))
        if not code:
            continue
        notes: list[str] = []
        for field in ("Duyuru", "Aciklama"):
            text = " ".join((r.get(field) or "").split())
            if text and text.rstrip(".").casefold() not in {n.rstrip(".").casefold() for n in notes}:
                notes.append(text)
        out[code] = {"name": (r.get("HatAdi") or "").strip() or None, "notes": notes}
    return out, cached, stale
