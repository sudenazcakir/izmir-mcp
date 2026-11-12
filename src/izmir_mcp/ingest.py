"""Gece yükleme işi: statik verileri kaynaklardan çekip Supabase'e yazar.

    python jobs/ingest_static.py            # hepsi
    python jobs/ingest_static.py --dry-run  # veritabanına yazmadan sadece say
    python jobs/ingest_static.py --only stations piers

Her adım bağımsızdır: biri çökerse diğerleri yine yüklenir, sonunda özet basılır.
"""

import argparse
import asyncio
import logging
import re
import statistics
import sys
from collections import defaultdict
from typing import Any

from .config import get_settings
from .districts import DISTRICTS, find_district, nearest_district
from .http import get_json
from .models import BusLine, District, Pier, Station, Stop
from .sources import pharmacy, transit_static
from .text import norm, parse_float

log = logging.getLogger("ingest")

STEPS = ["districts", "stations", "piers", "pharmacies", "stops", "lines"]


# ------------------------------------------------------------------ yardımcılar


def _first_list(data: Any) -> list[dict]:
    """Kaynak bazen düz liste, bazen {'kayitlar': [...]} döndürüyor."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def _pick(row: dict, *names: str) -> Any:
    lower = {k.lower(): v for k, v in row.items()}
    for n in names:
        v = lower.get(n.lower())
        if v not in (None, ""):
            return v
    return None


# ------------------------------------------------------------------ fetch


async def fetch_districts() -> list[District]:
    base = get_settings().izmir_api_base
    data = await get_json(f"{base}/api/ibb/cbs/muhtarliklar", source="İBB Muhtarlıklar")
    groups: dict[tuple[int, str], list[tuple[float, float]]] = defaultdict(list)
    for r in _first_list(data):
        lat, lon = parse_float(r.get("ENLEM")), parse_float(r.get("BOYLAM"))
        if lat and lon and r.get("ILCEID"):
            groups[(int(r["ILCEID"]), r["ILCE"])].append((lat, lon))
    names = {d.id: d.name for d in DISTRICTS}  # düzgün yazılmış adlar ('BUCA' -> 'Buca')
    out = [
        District(id=i, name=names.get(i, n.title()), lat=round(statistics.median(a for a, _ in pts), 6),
                 lon=round(statistics.median(b for _, b in pts), 6), neighborhood_count=len(pts))
        for (i, n), pts in groups.items()
    ]
    if len(out) < 25:  # beklenmeyen cevap: elimizdeki listeyi kullan
        log.warning("muhtarlık verisinden yalnız %d ilçe çıktı, gömülü liste kullanılıyor", len(out))
        return DISTRICTS
    return out


async def fetch_stations() -> list[Station]:
    return await transit_static.metro_stations() + await transit_static.izban_stations()


async def fetch_piers() -> list[Pier]:
    return await transit_static.piers()


async def fetch_pharmacies() -> list[dict]:
    base = get_settings().izmir_api_base
    data = await get_json(f"{base}/api/ibb/eczaneler", source=pharmacy.SOURCE)
    out = []
    for r in _first_list(data):
        p = pharmacy._to_model(r, on_duty=False)
        if not r.get("EczaneId") or r["EczaneId"] < 0:
            continue
        out.append({"id": r["EczaneId"], **p.model_dump(include={"name", "address", "phone", "region", "lat", "lon"})})
    return out


def _code(v: Any) -> str:
    """ESHOT kodları ondalıklı geliyor: 41154.0 -> '41154'."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip()
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def parse_eshot_stops(data: Any) -> list[Stop]:
    """ESHOT DuraklariGetir cevabı.

    Gerçek biçim (2026-11-11): DurakKodu (canlı API'deki durak numarası; `Id` iç kayıt numarası, kullanılmaz),
    DurakAdi, X_Koordinati = enlem, y_koordinati = boylam (virgüllü), IlceAdi, AcikKapali (1 açık, 2 kapalı).
    """
    out = []
    for r in _first_list(data):
        sid = _pick(r, "DurakKodu", "DURAK_KODU", "DURAK_ID", "DurakId")
        name = _pick(r, "DurakAdi", "DURAK_ADI", "Adi")
        if sid is None or not name:
            continue
        if _pick(r, "AcikKapali") == 2:  # kapalı durak
            continue
        lines_raw = _pick(r, "DURAKTAN_GECEN_HATLAR", "DuraktanGecenHatlar", "Hatlar") or []
        lines = lines_raw if isinstance(lines_raw, list) else [x for x in re.split(r"[^0-9A-Za-z]+", str(lines_raw)) if x]
        out.append(Stop(
            id=_code(sid), name=str(name).strip(),
            lat=parse_float(_pick(r, "X_Koordinati", "ENLEM", "Enlem", "Lat")),
            lon=parse_float(_pick(r, "Y_Koordinati", "BOYLAM", "Boylam", "Lon", "Lng")),
            lines=[_code(x) for x in lines],
            district_name=_pick(r, "IlceAdi", "ILCE"),
        ))
    return out


def parse_eshot_lines(data: Any) -> list[BusLine]:
    out = []
    for r in _first_list(data):
        no = _pick(r, "HAT_NO", "HatNo", "HatNumarasi")
        name = _pick(r, "HAT_ADI", "HatAdi", "Adi")
        if no is None or not name:
            continue
        out.append(BusLine(number=_code(no), name=str(name).strip(),
                           start=_pick(r, "HatBaslangic", "HAT_BASLANGIC", "Baslangic"),
                           end=_pick(r, "HatBitis", "HAT_BITIS", "Bitis")))
    return out


async def fetch_stops() -> list[Stop]:
    base = get_settings().eshot_api_base
    data = await get_json(f"{base}/AcikVeriEshot/Duraklar/DuraklariGetir", source="ESHOT Duraklar")
    stops = parse_eshot_stops(data)
    if not stops:
        raise RuntimeError("ESHOT durak listesi boş ya da tanınmayan biçimde geldi")
    return stops


async def fetch_lines() -> list[BusLine]:
    base = get_settings().eshot_api_base
    data = await get_json(f"{base}/AcikVeriEshot/Hatlar/HatlariGetir", source="ESHOT Hatlar")
    lines = parse_eshot_lines(data)
    if not lines:
        raise RuntimeError("ESHOT hat listesi boş ya da tanınmayan biçimde geldi")
    return lines


# ------------------------------------------------------------------ write


def _district_id(lat: float | None, lon: float | None) -> int | None:
    # Yaklaşık: en yakın ilçe merkezi. İlçe sınırı poligonu eklenince değiştirilecek.
    return nearest_district(lat, lon).id if lat and lon else None


def _stop_district_id(s: Stop) -> int | None:
    d = find_district(s.district_name) if s.district_name else None
    return d.id if d else _district_id(s.lat, s.lon)


# Tam liste geldiğinde, bu çalıştırmada güncellenmeyen (kaldırılmış ya da eski biçimde yazılmış) satırları sil.
# now() işlem başladığı anı verir, yani bu işlemde yazılan her satırın updated_at'i now()'a eşittir.
_PRUNE = {"stops": "stops", "lines": "bus_lines", "stations": "stations", "piers": "piers", "pharmacies": "pharmacies"}
_PRUNE_MIN_ROWS = {"stops": 1000, "lines": 100, "stations": 20, "piers": 5, "pharmacies": 500}


def write(conn, step: str, rows: list) -> None:
    with conn.cursor() as cur:
        if step == "districts":
            cur.executemany(
                """insert into districts (id, name, name_norm, lat, lon, neighborhood_count, updated_at)
                   values (%s,%s,%s,%s,%s,%s, now())
                   on conflict (id) do update set name=excluded.name, name_norm=excluded.name_norm, lat=excluded.lat,
                     lon=excluded.lon, neighborhood_count=excluded.neighborhood_count, updated_at=now()""",
                [(d.id, d.name, norm(d.name), d.lat, d.lon, d.neighborhood_count) for d in rows])
        elif step == "stations":
            cur.executemany(
                """insert into stations (system, source_id, name, name_norm, lat, lon, seq, active, updated_at)
                   values (%s,%s,%s,%s,%s,%s,%s,%s, now())
                   on conflict (system, source_id) do update set name=excluded.name, name_norm=excluded.name_norm,
                     lat=excluded.lat, lon=excluded.lon, seq=excluded.seq, active=excluded.active, updated_at=now()""",
                [(s.system, s.source_id, s.name, norm(s.name), s.lat, s.lon, s.seq, s.active) for s in rows])
        elif step == "piers":
            cur.executemany(
                """insert into piers (id, name, name_norm, lat, lon, car_ferry, active, updated_at)
                   values (%s,%s,%s,%s,%s,%s,%s, now())
                   on conflict (id) do update set name=excluded.name, name_norm=excluded.name_norm, lat=excluded.lat,
                     lon=excluded.lon, car_ferry=excluded.car_ferry, active=excluded.active, updated_at=now()""",
                [(p.id, p.name, norm(p.name), p.lat, p.lon, p.car_ferry, p.active) for p in rows])
        elif step == "pharmacies":
            cur.executemany(
                """insert into pharmacies (id, name, address, phone, region, district_id, lat, lon, updated_at)
                   values (%s,%s,%s,%s,%s,%s,%s,%s, now())
                   on conflict (id) do update set name=excluded.name, address=excluded.address, phone=excluded.phone,
                     region=excluded.region, district_id=excluded.district_id, lat=excluded.lat, lon=excluded.lon,
                     updated_at=now()""",
                [(p["id"], p["name"], p["address"], p["phone"], p["region"], _district_id(p["lat"], p["lon"]),
                  p["lat"], p["lon"]) for p in rows])
        elif step == "stops":
            cur.executemany(
                """insert into stops (id, name, name_norm, lat, lon, district_id, lines, updated_at)
                   values (%s,%s,%s,%s,%s,%s,%s, now())
                   on conflict (id) do update set name=excluded.name, name_norm=excluded.name_norm, lat=excluded.lat,
                     lon=excluded.lon, district_id=excluded.district_id, lines=excluded.lines, updated_at=now()""",
                [(s.id, s.name, norm(s.name), s.lat, s.lon, _stop_district_id(s), s.lines) for s in rows])
        elif step == "lines":
            cur.executemany(
                """insert into bus_lines (number, name, start_name, end_name, updated_at) values (%s,%s,%s,%s, now())
                   on conflict (number) do update set name=excluded.name, start_name=excluded.start_name,
                     end_name=excluded.end_name, updated_at=now()""",
                [(b.number, b.name, b.start, b.end) for b in rows])
        if step in _PRUNE and len(rows) >= _PRUNE_MIN_ROWS[step]:
            cur.execute(f"delete from {_PRUNE[step]} where updated_at < now()")  # noqa: S608 (sabit tablo adı)
            if cur.rowcount:
                log.info("%s: %d eski satır silindi", step, cur.rowcount)
    conn.commit()


FETCHERS = {
    "districts": fetch_districts,
    "stations": fetch_stations,
    "piers": fetch_piers,
    "pharmacies": fetch_pharmacies,
    "stops": fetch_stops,
    "lines": fetch_lines,
}


async def run(steps: list[str], dry_run: bool) -> dict[str, str]:
    conn = None
    if not dry_run:
        import psycopg
        import psycopg.conninfo

        url = get_settings().database_url
        if not url:
            raise SystemExit("DATABASE_URL tanımlı değil (.env). Sadece denemek için --dry-run kullan.")
        url = url.strip().strip('"').strip("'")
        if not url.startswith(("postgresql://", "postgres://")):
            # Değeri yazdırmıyoruz: içinde şifre olabilir.
            raise SystemExit("DATABASE_URL 'postgresql://' ile başlamalı. Supabase > Connect > Session pooler adresini "
                             "kopyalayıp [YOUR-PASSWORD] yerine şifreni yaz.")
        try:
            conn = psycopg.connect(url, connect_timeout=10)
        except psycopg.Error as e:
            password = psycopg.conninfo.conninfo_to_dict(url).get("password") or ""
            reason = str(e).replace(password, "****") if password else str(e)
            raise SystemExit(f"Veritabanına bağlanılamadı: {reason.strip()}\n"
                             "DATABASE_URL'deki şifreyi ve Session pooler adresini kontrol et.") from None
    summary: dict[str, str] = {}
    try:
        for step in steps:  # districts önce: diğer tablolar ona referans veriyor
            try:
                rows = await FETCHERS[step]()
                if conn is not None:
                    write(conn, step, rows)
                summary[step] = f"ok ({len(rows)})"
            except Exception as e:  # bir adım çökse de diğerleri devam etsin
                if conn is not None:
                    conn.rollback()
                log.exception("step %s failed", step)
                summary[step] = f"HATA: {e}"
    finally:
        if conn is not None:
            conn.close()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="İzmir statik verilerini Supabase'e yükler")
    parser.add_argument("--dry-run", action="store_true", help="veritabanına yazma, sadece çek ve say")
    parser.add_argument("--only", nargs="+", choices=STEPS, help="sadece bu adımlar")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    steps = [s for s in STEPS if not args.only or s in args.only]
    summary = asyncio.run(run(steps, args.dry_run))
    for step, result in summary.items():
        print(f"{step:12} {result}")
    return 1 if any(v.startswith("HATA") for v in summary.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
