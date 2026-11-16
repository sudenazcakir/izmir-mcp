"""Araçların iş mantığı. server.py bunları MCP aracı olarak kaydeder; testler doğrudan çağırır."""

import asyncio
from datetime import UTC, datetime

from . import db
from .breaker import BreakerState, breakers
from .cache import SOURCE_REDIS, store
from .config import get_settings
from .districts import DISTRICTS, find_district
from .geo import haversine_km
from .http import SourceFailure, get_json
from .models import (
    AirQualityResult,
    AirReading,
    Announcement,
    AnnouncementsResult,
    Arrival,
    ErrorCode,
    HourlyAqi,
    LineNote,
    Meta,
    NearbyStation,
    Pharmacy,
    PharmacyResult,
    SourceError,
    SourceStatus,
    SourceStatusResult,
    StopArrivals,
    StopRef,
    TransitDirections,
    TripPoint,
)
from .sources import air, announcements, eshot, pharmacy, transit_static
from .text import norm, parse_float, strip_stop_words

MAX_STOPS_PER_QUERY = 10  # Bornova Metro gibi aktarma merkezlerinde aynı adlı 10 peron var


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- get_stop_arrivals


def estimate_eta(stops_remaining: int, distance_km: float | None) -> tuple[int, str]:
    """ESHOT dakika vermiyor: kalan durak sayısı ve (varsa) otobüsün durağa uzaklığından tahmin."""
    s = get_settings()
    by_stops = stops_remaining * s.avg_minutes_per_stop
    if distance_km is None:
        minutes, method = by_stops, "stops"
    else:
        by_distance = distance_km * s.road_factor / s.avg_bus_speed_kmh * 60
        minutes, method = (by_stops + by_distance) / 2, "blend"
    if stops_remaining <= 0 and (distance_km is None or distance_km < 0.15):
        return 0, method
    return max(1, round(minutes)), method


async def _landmark_coords(q: str) -> tuple[float, float, str] | None:
    """'bornova metro' -> Bornova metro istasyonunun koordinatı. Metro, İZBAN, iskele, ilçe sırasıyla bakar."""
    candidates: list[tuple[str, float, float]] = []
    for kind in ("metro", "izban", "piers"):
        candidates += [(x.name, x.lat, x.lon) for x in await transit_static.stations_or_snapshot(kind)]
    candidates += [(d.name, d.lat, d.lon) for d in DISTRICTS]
    best: tuple[int, tuple[float, float, str]] | None = None
    for name, lat, lon in candidates:
        n = norm(name)
        if n and (n == q or f" {n} " in f" {q} " or q in n):
            score = len(n) + (100 if n == q else 0)
            if best is None or score > best[0]:
                best = (score, (lat, lon, name))
    return best[1] if best else None


async def resolve_stops(query: str) -> tuple[list[StopRef], list[str]]:
    notes: list[str] = []
    raw = query.strip()
    if raw.isdigit():
        row = await db.get_stop(raw)
        if row:
            return [StopRef(id=raw, name=row["name"], lat=row["lat"], lon=row["lon"])], notes
        return [StopRef(id=raw)], notes

    q = strip_stop_words(raw)
    rows = await db.search_stops(q, limit=MAX_STOPS_PER_QUERY)
    if rows:
        notes.append("Durak adı veritabanından eşleşti.")
        return [StopRef(id=str(r["id"]), name=r["name"], lat=r["lat"], lon=r["lon"]) for r in rows], notes

    landmark = await _landmark_coords(q)
    if landmark is None:
        return [], notes
    lat, lon, label = landmark
    near = await eshot.nearby_stops(lat, lon)
    exact = [s for s in near if norm(s["name"]) == q]
    partial = [s for s in near if q in norm(s["name"]) or norm(s["name"]) in q]
    chosen = exact or partial or near[:2]
    if not (exact or partial):
        notes.append(f"'{raw}' adlı durak bulunamadı; {label} çevresindeki en yakın duraklar kullanıldı.")
    else:
        notes.append(f"Durak, {label} çevresindeki duraklar arasından ada göre seçildi.")
    return [StopRef(id=s["id"], name=s["name"], lat=s["lat"], lon=s["lon"]) for s in chosen[:MAX_STOPS_PER_QUERY]], notes


async def get_stop_arrivals(stop: str, limit: int = 8) -> StopArrivals:
    started = _now()
    if not stop or not stop.strip():
        return StopArrivals(query=stop, meta=Meta(sources=[], fetched_at=started), error=SourceError(
            code=ErrorCode.INVALID_INPUT, message="Durak adı veya durak numarası gerekli."))

    try:
        stops, notes = await resolve_stops(stop)
    except SourceFailure as f:
        return StopArrivals(query=stop, meta=Meta(sources=[eshot.SOURCE_STOPS], fetched_at=started), error=f.error)
    if not stops:
        return StopArrivals(query=stop, meta=Meta(sources=[], fetched_at=started, notes=notes), error=SourceError(
            code=ErrorCode.NOT_FOUND,
            message=f"'{stop}' için durak bulunamadı.",
            hint="Durak numarası (ör. 40010) ya da metro/İZBAN istasyonu, iskele veya ilçe adı içeren bir ad deneyin.",
        ))

    sem = asyncio.Semaphore(5)

    async def fetch(ref: StopRef):
        async with sem:
            return ref, await eshot.approaching_buses(ref.id)

    results = await asyncio.gather(*(fetch(s) for s in stops), return_exceptions=True)
    failures = [r for r in results if isinstance(r, SourceFailure)]
    ok = [r for r in results if not isinstance(r, BaseException)]
    unexpected = [r for r in results if isinstance(r, BaseException) and not isinstance(r, SourceFailure)]
    if unexpected and not ok and not failures:
        raise unexpected[0]

    best: dict[tuple, Arrival] = {}
    for ref, buses in ok:
        for b in buses:
            bus_lat, bus_lon = parse_float(b.get("KoorX")), parse_float(b.get("KoorY"))
            dist_km = None
            if None not in (bus_lat, bus_lon, ref.lat, ref.lon):
                dist_km = haversine_km(bus_lat, bus_lon, ref.lat, ref.lon)
            remaining = int(b.get("KalanDurakSayisi") or 0)
            eta, method = estimate_eta(remaining, dist_km)
            a = Arrival(
                stop_id=ref.id,
                line_number=str(b.get("HatNumarasi")),
                line_name=(b.get("HatAdi") or "").strip(),
                bus_id=b.get("OtobusId"),
                direction=b.get("HattinYonu"),
                stops_remaining=remaining,
                eta_minutes=eta,
                eta_method=method,
                distance_m=round(dist_km * 1000) if dist_km is not None else None,
                wheelchair_accessible=b.get("EngelliMi"),
                bike_rack=b.get("BisikletAparatliMi"),
                bus_lat=bus_lat,
                bus_lon=bus_lon,
            )
            key = (a.bus_id, a.line_number)  # aynı otobüs birden çok perona yaklaşıyor görünebilir
            if key not in best or a.eta_minutes < best[key].eta_minutes:
                best[key] = a

    arrivals = sorted(best.values(), key=lambda a: (a.eta_minutes, a.stops_remaining))[: max(1, limit)]
    notes.append(
        "Varış süreleri tahminidir: ESHOT yalnızca kalan durak sayısı ve otobüs konumunu veriyor "
        f"(durak başına ~{get_settings().avg_minutes_per_stop} dk, ortalama {get_settings().avg_bus_speed_kmh} km/sa)."
    )
    if ok and not arrivals:
        notes.append("Şu anda bu durağa yaklaşan otobüs görünmüyor.")
    error = None
    if failures and not ok:
        error = failures[0].error
    elif failures:
        notes.append(f"{len(failures)} peron için ESHOT cevap vermedi; sonuçlar eksik olabilir.")
    return StopArrivals(
        query=stop, stops=stops, arrivals=arrivals, error=error,
        meta=Meta(sources=[eshot.SOURCE], fetched_at=_now(), notes=notes),
    )


# ---------------------------------------------------------------- find_pharmacies


def _match_district(p: Pharmacy, district_norm: str) -> bool:
    """'BUCA 1', 'ESKİ FOÇA' eşleşir; 'ÇEŞMEALTI-İSKELE' (Urla'da) Çeşme ile eşleşmez."""
    return bool(p.region) and district_norm in norm(p.region).split()


async def find_pharmacies(district: str | None = None, on_duty: bool = True, limit: int = 5,
                          lat: float | None = None, lon: float | None = None) -> PharmacyResult:
    started = _now()
    meta = Meta(sources=[pharmacy.SOURCE], fetched_at=started)

    d = find_district(district) if district else None
    if district and d is None:
        return PharmacyResult(district=district, on_duty=on_duty, meta=meta, error=SourceError(
            code=ErrorCode.INVALID_INPUT,
            message=f"'{district}' İzmir ilçeleri arasında bulunamadı.",
            hint="Geçerli ilçeler: " + ", ".join(x.name for x in DISTRICTS),
        ))
    if d is None and (lat is None or lon is None):
        return PharmacyResult(district=None, on_duty=on_duty, meta=meta, error=SourceError(
            code=ErrorCode.INVALID_INPUT, message="İlçe adı ya da konum (lat, lon) gerekli."))

    try:
        items, cached, stale = await (pharmacy.on_duty_pharmacies() if on_duty else pharmacy.all_pharmacies())
    except SourceFailure as f:
        error = f.error
        if on_duty and error.hint is None:
            error = error.model_copy(update={"hint": (
                "Bugünün nöbet listesi alınamadı. Önceki günün listesi bugün geçerli olmadığı için gösterilmiyor; "
                "kullanıcıyı eczane kapılarındaki nöbet listesine ya da İzmir Eczacı Odası'na yönlendir.")})
        return PharmacyResult(district=d.name if d else None, on_duty=on_duty, meta=meta, error=error)

    ref_lat, ref_lon = (lat, lon) if lat is not None and lon is not None else (d.lat, d.lon)
    for p in items:
        if p.lat is not None and p.lon is not None:
            p.distance_km = round(haversine_km(ref_lat, ref_lon, p.lat, p.lon), 2)

    if d is not None and lat is None:
        dn = norm(d.name)
        named = [p for p in items if _match_district(p, dn)]
        # Nöbet bölgeleri ilçe adıyla birebir örtüşmüyor (ör. Buca'da 'ŞİRİNYER', 'EVKA 1' bölgeleri de var),
        # bu yüzden ilçe merkezine 5 km'den yakın olanları da ekliyoruz.
        nearby = [p for p in items if p.distance_km is not None and p.distance_km <= 5 and p not in named]
        pool = named + nearby
    else:
        pool = [p for p in items if p.distance_km is not None]
    pool.sort(key=lambda p: (p.distance_km if p.distance_km is not None else 1e9))
    result = pool[: max(1, limit)]

    if on_duty:
        meta.notes.append("Nöbet listesi her sabah 08:00'de değişir; gitmeden önce telefonla teyit edin.")
    if not result:
        meta.notes.append("Bu bölgede kayıtlı eczane bulunamadı.")
    meta.cached, meta.stale = cached, stale
    if stale:
        meta.notes.append("Kaynak şu an cevap vermiyor; en son başarıyla alınan liste gösteriliyor.")
    meta.fetched_at = _now()
    return PharmacyResult(district=d.name if d else None, on_duty=on_duty, pharmacies=result, meta=meta)


# ---------------------------------------------------------------- get_air_quality

STATION_NOTE_KM = 10  # bundan uzaktaki istasyon için "uzak" notu düş
STATION_MAX_KM = 30  # bundan uzaktaki istasyonu hiç gösterme (ilçeyi temsil etmez)


def _open_meteo_readings(data: dict, label: str) -> tuple[list[AirReading], int | None, list[HourlyAqi]]:
    offset = data.get("utc_offset_seconds")
    cur = data.get("current") or {}
    units = data.get("current_units") or {}
    if not cur.get("time"):
        return [], None, []
    now = air.parse_local_time(cur["time"], offset)
    readings = [
        AirReading(
            station=f"Open-Meteo modeli ({label})", pollutant=name, value=float(cur[var]),
            unit=units.get(var) or "µg/m³", measured_at=now, resolution="hourly", source=air.SOURCE_OPEN_METEO,
            lat=data.get("latitude"), lon=data.get("longitude"),
        )
        for var, name in air.POLLUTANTS.items()
        if cur.get(var) is not None
    ]
    aqi = cur.get("european_aqi")
    aqi = round(aqi) if aqi is not None else None

    hourly = data.get("hourly") or {}
    outlook: list[HourlyAqi] = []
    for i, t in enumerate(hourly.get("time") or []):
        when = air.parse_local_time(t, offset)
        if when <= now:
            continue

        def at(var: str, i: int = i):
            values = hourly.get(var) or []
            return values[i] if i < len(values) else None

        h_aqi = at("european_aqi")
        outlook.append(HourlyAqi(
            time=when, european_aqi=round(h_aqi) if h_aqi is not None else None, level=air.aqi_level(h_aqi),
            pm2_5=at("pm2_5"), pm10=at("pm10"),
        ))
    return readings, aqi, outlook


def _nearest_station(rows: list[dict], lat: float, lon: float) -> tuple[list[AirReading], float | None]:
    located = [r for r in rows if r["lat"] is not None and r["lon"] is not None]
    if not located:
        return [], None
    best = min(located, key=lambda r: haversine_km(lat, lon, r["lat"], r["lon"]))
    dist = round(haversine_km(lat, lon, best["lat"], best["lon"]), 1)
    latest = max(r["measured_at"] for r in located if r["key"] == best["key"])
    readings = [
        AirReading(
            station=r["station"], pollutant=r["pollutant"], value=r["value"], measured_at=r["measured_at"],
            resolution="daily", source=air.SOURCE_IBB, lat=r["lat"], lon=r["lon"], distance_km=dist,
        )
        for r in located
        if r["key"] == best["key"] and r["measured_at"] == latest
    ]
    return readings, dist


async def get_air_quality(district: str | None = None, lat: float | None = None, lon: float | None = None,
                          hours: int = 12) -> AirQualityResult:
    started = _now()
    meta = Meta(sources=[air.SOURCE_OPEN_METEO, air.SOURCE_IBB], fetched_at=started)

    d = find_district(district) if district else None
    if district and d is None:
        return AirQualityResult(district=district, meta=meta, error=SourceError(
            code=ErrorCode.INVALID_INPUT,
            message=f"'{district}' İzmir ilçeleri arasında bulunamadı.",
            hint="Geçerli ilçeler: " + ", ".join(x.name for x in DISTRICTS),
        ))
    if d is None and (lat is None or lon is None):
        return AirQualityResult(meta=meta, error=SourceError(
            code=ErrorCode.INVALID_INPUT, message="İlçe adı ya da konum (lat, lon) gerekli."))

    ref_lat, ref_lon = (lat, lon) if lat is not None and lon is not None else (d.lat, d.lon)
    label = f"{d.name} merkezi" if d and lat is None else "verilen konum"

    om, ibb = await asyncio.gather(air.open_meteo(ref_lat, ref_lon), air.station_daily(), return_exceptions=True)
    for r in (om, ibb):
        if isinstance(r, BaseException) and not isinstance(r, SourceFailure):
            raise r

    result = AirQualityResult(district=d.name if d else None, meta=meta)
    notes = meta.notes
    stale = False
    failures: list[SourceFailure] = []

    if isinstance(om, SourceFailure):
        failures.append(om)
        notes.append("Open-Meteo şu an cevap vermiyor; anlık ve saatlik değerler gösterilemiyor.")
    else:
        data, _cached, om_stale = om
        stale |= om_stale
        result.current, result.european_aqi, outlook = _open_meteo_readings(data, label)
        result.level = air.aqi_level(result.european_aqi)
        result.outlook = outlook[: max(0, hours)]
        notes.append("Anlık ve saatlik değerler ölçüm değil, Open-Meteo (CAMS) hava kalitesi modelinin tahminidir.")

    if isinstance(ibb, SourceFailure):
        failures.append(ibb)
        notes.append("Belediye hava kalitesi istasyonları şu an cevap vermiyor; istasyon ölçümü gösterilemiyor.")
    else:
        rows, _cached, ibb_stale = ibb
        stale |= ibb_stale
        readings, dist = _nearest_station(rows, ref_lat, ref_lon)
        if dist is not None and dist > STATION_MAX_KM:
            notes.append(f"En yakın belediye istasyonu {dist} km uzakta; bu bölgeyi temsil etmediği için gösterilmedi.")
        elif readings:
            result.station_daily = readings
            measured = readings[0].measured_at
            age = (_now().astimezone(air.ISTANBUL).date() - measured.date()).days
            when = f"{measured:%d.%m.%Y} tarihli günlük ortalama"
            notes.append(f"Belediye istasyonu ({readings[0].station}): {when}"
                         + (f" ({age} gün önce)." if age >= 1 else "."))
            if dist is not None and dist > STATION_NOTE_KM:
                notes.append(f"Bu istasyon ilçe merkezine {dist} km uzakta.")
        else:
            notes.append("Belediye istasyonlarında bu bölge için güncel ölçüm yok.")

    if len(failures) == 2:
        result.error = failures[0].error
    meta.stale = stale
    if stale:
        notes.append("Kaynaklardan biri şu an cevap vermiyor; en son başarıyla alınan değerler gösteriliyor.")
    meta.fetched_at = _now()
    return result


# ---------------------------------------------------------------- get_announcements


async def get_announcements(line: str | None = None, include_upcoming: bool = True,
                            limit: int = 20) -> AnnouncementsResult:
    started = _now()
    meta = Meta(sources=[announcements.SOURCE_CSV, announcements.SOURCE_LINES], fetched_at=started)
    code = announcements.line_code(line) if line else None
    if line and code is None:
        return AnnouncementsResult(line=line, meta=meta, error=SourceError(
            code=ErrorCode.INVALID_INPUT, message=f"'{line}' bir hat numarası değil.",
            hint="Hat numarasını rakamla yazın, ör. 302. Hat vermezseniz tüm güncel duyurular listelenir."))

    csv_res, lines_res = await asyncio.gather(
        announcements.csv_announcements(),
        announcements.lines() if code else _none(),
        return_exceptions=True,
    )
    for r in (csv_res, lines_res):
        if isinstance(r, BaseException) and not isinstance(r, SourceFailure):
            raise r

    result = AnnouncementsResult(line=code, meta=meta)
    notes, stale, failures = meta.notes, False, []

    line_info = None
    if code:
        if isinstance(lines_res, SourceFailure):
            failures.append(lines_res)
            notes.append("ESHOT hat bilgileri şu an alınamıyor; kalıcı hat notları gösterilemiyor.")
        else:
            info, _c, s_ = lines_res
            stale |= s_
            line_info = info.get(code)
            if line_info:
                result.line_name = line_info["name"]
                result.line_notes = [
                    LineNote(line=code, note=n, source=announcements.SOURCE_LINES) for n in line_info["notes"]]

    if isinstance(csv_res, SourceFailure):
        failures.append(csv_res)
        notes.append("ESHOT hat duyuruları dosyası şu an alınamıyor; güncel duyurular gösterilemiyor.")
    else:
        rows, _c, s_ = csv_res
        stale |= s_
        now = _now()
        grouped: dict[tuple, dict] = {}
        for r in rows:
            if r["ends_at"] is not None and r["ends_at"] < now:
                continue  # süresi bitmiş
            status = "upcoming" if r["starts_at"] > now else "active"
            if status == "upcoming" and not include_upcoming:
                continue
            key = (r["title"], r["starts_at"], r["ends_at"])
            g = grouped.setdefault(key, {**r, "lines": [], "status": status})
            if r["line"] not in g["lines"]:
                g["lines"].append(r["line"])
        items = [g for g in grouped.values() if code is None or code in g["lines"]]
        items.sort(key=lambda g: (g["status"] != "active", -g["starts_at"].timestamp() if g["status"] == "active"
                                  else g["starts_at"].timestamp()))
        result.total = len(items)
        result.announcements = [
            Announcement(title=g["title"], lines=sorted(g["lines"], key=lambda x: (len(x), x)),
                         starts_at=g["starts_at"], ends_at=g["ends_at"], status=g["status"],
                         source=announcements.SOURCE_CSV)
            for g in items[: max(1, limit)]
        ]
        if result.total > len(result.announcements):
            notes.append(f"{result.total} duyurudan ilk {len(result.announcements)} tanesi gösteriliyor.")
        if code and not items:
            notes.append(f"{code} numaralı hat için şu an geçerli bir duyuru yok.")

    if code and line_info is None and not isinstance(lines_res, SourceFailure) and not result.announcements:
        result.error = SourceError(
            code=ErrorCode.NOT_FOUND, message=f"{code} numaralı bir ESHOT hattı bulunamadı.",
            hint="Hat numarasını kontrol edin; hat vermezseniz tüm güncel duyurular listelenir.")
    elif failures and (len(failures) == 2 or not code):
        result.error = failures[0].error
    meta.stale = stale
    if stale:
        notes.append("Kaynaklardan biri şu an cevap vermiyor; en son başarıyla alınan bilgiler gösteriliyor.")
    meta.fetched_at = _now()
    return result


async def _none():
    return None


# ---------------------------------------------------------------- get_source_status

PROBE_STOP_ID = "40010"  # sık kullanılan bir ESHOT durağı; boş liste dönmesi de "ayakta" demek


def _probes() -> dict[str, object]:
    """Kaynak adı -> hafif bir sağlık isteği. Yeni bir kaynak eklenince buraya da eklenmeli."""
    base = get_settings().izmir_api_base
    konak = find_district("Konak")
    lat, lon = konak.lat, konak.lon
    probes = {
        eshot.SOURCE: lambda: get_json(f"{base}/api/iztek/duragayaklasanotobusler/{PROBE_STOP_ID}",
                                       source=eshot.SOURCE, retries=0),
        eshot.SOURCE_STOPS: lambda: get_json(
            f"{base}/api/ibb/cbs/noktayayakinduraklar", source=eshot.SOURCE_STOPS, retries=0,
            params={"x": f"{lon:.6f}", "y": f"{lat:.6f}", "inCoordSys": "EPSG:4326", "outCoordSys": "EPSG:4326"}),
        pharmacy.SOURCE: lambda: get_json(f"{base}/api/ibb/nobetcieczaneler", source=pharmacy.SOURCE, retries=0),
        transit_static.SOURCE_METRO: lambda: get_json(f"{base}/api/metro/istasyonlar",
                                                      source=transit_static.SOURCE_METRO, retries=0),
        transit_static.SOURCE_IZBAN: lambda: get_json(f"{base}/api/izban/istasyonlar",
                                                      source=transit_static.SOURCE_IZBAN, retries=0),
        transit_static.SOURCE_IZDENIZ: lambda: get_json(f"{base}/api/izdeniz/iskeleler",
                                                        source=transit_static.SOURCE_IZDENIZ, retries=0),
        air.SOURCE_IBB: lambda: get_json(f"{base}/api/ibb/cevre/havadegerleri", source=air.SOURCE_IBB, retries=0),
        air.SOURCE_OPEN_METEO: lambda: get_json(
            f"{get_settings().open_meteo_air_base}/v1/air-quality", source=air.SOURCE_OPEN_METEO, retries=0,
            params={"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "current": "european_aqi"}),
    }
    # Duyuru CSV'si (~17 KB) ve hat listesi (~250 KB) büyük: içeriği indirmeyen HEAD isteğiyle yoklanıyor.
    probes[announcements.SOURCE_CSV] = lambda: get_json(
        get_settings().eshot_announcements_csv_url, source=announcements.SOURCE_CSV, retries=0, head=True)
    probes[announcements.SOURCE_LINES] = lambda: get_json(
        f"{get_settings().eshot_api_base}/AcikVeriEshot/Hatlar/HatlariGetir", source=announcements.SOURCE_LINES,
        retries=0, head=True)
    if store.enabled():  # REDIS_URL yoksa önbellek yalnız bellekte; listede gösterme
        probes[SOURCE_REDIS] = store.ping
    return probes


def _status_of(source: str) -> SourceStatus:
    h = breakers.known().get(source)
    if h is None:
        return SourceStatus(source=source, state="unknown")
    if h.state == BreakerState.OPEN:
        state = "down"
    elif h.state == BreakerState.HALF_OPEN:
        state = "recovering"
    elif h.last_success_at is None and h.consecutive_failures == 0:
        state = "unknown"
    elif h.last_success_at is None or (h.last_failure_at and h.last_failure_at > h.last_success_at):
        state = "degraded"  # son istek hata verdi ama eşik henüz aşılmadı
    else:
        state = "up"
    return SourceStatus(
        source=source, state=state, consecutive_failures=h.consecutive_failures,
        last_success_at=h.last_success_at, last_failure_at=h.last_failure_at,
        last_error=h.last_error if state != "up" else None, retry_in_seconds=h.retry_in_seconds(),
    )


async def get_source_status(probe: bool = True) -> SourceStatusResult:
    """Her veri kaynağının durumu. probe=True: kapalı olmayan kaynaklara şimdi küçük bir istek atar."""
    probes = _probes()
    if probe:
        # Devresi açık kaynaklar (bekleme süresi dolmadıysa) get_json içinde zaten hemen reddedilir.
        await asyncio.gather(*(fn() for fn in probes.values()), return_exceptions=True)
    names = list(probes) + [s for s in breakers.known() if s not in probes]
    statuses = [_status_of(s) for s in names]
    notes = []
    if not probe:
        notes.append("Canlı deneme yapılmadı; durumlar son gerçek isteklere göre.")
    down = [s.source for s in statuses if s.state == "down"]
    if down:
        notes.append("Kapalı kaynaklar: " + ", ".join(down) + ". Bu kaynaklara bağlı sorulara şu an cevap verilemez.")
    return SourceStatusResult(
        sources=statuses,
        all_up=all(s.state == "up" for s in statuses),
        meta=Meta(sources=names, fetched_at=_now(), notes=notes),
    )


# ---------------------------------------------------------------- get_transit_directions

NEAR_STATION_KM = 1.5
USER_WORDS = {"konumum", "konumumdan", "buradan", "bulundugum yer", "bulundugum yerden", "benim konumum", "konum"}


async def _resolve_place(q: str) -> TripPoint | None:
    """Yer adını koordinata çevirir: önce istasyon/iskele/ilçe, sonra ESHOT durak adı."""
    n = norm(q)
    if not n:
        return None
    # "Karşıyaka İskele" vapuru, "Bornova Metro" metroyu kastediyor: önce o türde ara.
    hints = (("iskele", "piers", "pier"), ("vapur", "piers", "pier"), ("metro", "metro", "metro"),
             ("izban", "izban", "izban"))
    for word, k, kind in hints:
        if word in n.split():
            base = " ".join(w for w in n.split() if w not in {word, "istasyonu", "istasyon"})
            for x in await transit_static.stations_or_snapshot(k):
                if base and norm(x.name) == base:
                    return TripPoint(query=q, name=x.name, kind=kind, lat=x.lat, lon=x.lon)
    hit = await _landmark_coords(n)
    if hit is None and (s := strip_stop_words(n)) != n:
        hit = await _landmark_coords(s)
    if hit is not None:
        lat, lon, name = hit
        kind = "district"
        for k, label in (("metro", "metro"), ("izban", "izban"), ("piers", "pier")):
            if any(x.name == name for x in await transit_static.stations_or_snapshot(k)):
                kind = label
                break
        return TripPoint(query=q, name=name, kind=kind, lat=lat, lon=lon)
    rows = await db.search_stops(strip_stop_words(q), limit=1)
    if rows and rows[0].get("lat") is not None:
        r = rows[0]
        return TripPoint(query=q, name=r["name"], kind="stop", lat=r["lat"], lon=r["lon"])
    return None


def transit_maps_url(o: TripPoint, d: TripPoint) -> str:
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={o.lat:.6f},{o.lon:.6f}&destination={d.lat:.6f},{d.lon:.6f}&travelmode=transit"
    )


async def get_transit_directions(origin: str, destination: str, origin_lat: float | None = None,
                                 origin_lon: float | None = None) -> TransitDirections:
    meta = Meta(sources=["İzmir Metro", "İZBAN", "İZDENİZ", "ESHOT"], fetched_at=_now())
    points: list[TripPoint | None] = []
    for label, q in (("Başlangıç", origin), ("Varış", destination)):
        if label == "Başlangıç" and origin_lat is not None and origin_lon is not None and (
            not q.strip() or norm(q) in USER_WORDS
        ):
            points.append(TripPoint(query=q or "konumum", name="Konumun", kind="user", lat=origin_lat, lon=origin_lon))
            continue
        p = await _resolve_place(q)
        if p is None:
            return TransitDirections(meta=meta, error=SourceError(
                code=ErrorCode.NOT_FOUND,
                message=f"{label} noktası '{q}' bulunamadı.",
                hint="İlçe, semt, metro/İZBAN istasyonu, iskele ya da durak adı yaz (ör. 'Bornova Metro', 'Konak İskele').",
            ))
        points.append(p)
    o, d = points
    assert o is not None and d is not None

    near: list[NearbyStation] = []
    for kind, system in (("metro", "metro"), ("izban", "izban"), ("piers", "ferry")):
        items = await transit_static.stations_or_snapshot(kind)
        for end, pt in (("origin", o), ("destination", d)):
            best = min(((haversine_km(pt.lat, pt.lon, x.lat, x.lon), x.name) for x in items
                        if getattr(x, "active", True)), default=None)
            if best and best[0] <= NEAR_STATION_KM:
                near.append(NearbyStation(system=system, name=best[1], distance_km=round(best[0], 2), end=end))

    meta.notes.append(
        "Bu araç güzergahı kendisi hesaplamaz: uçları ve yakın raylı/vapur duraklarını verir. Seçeneklerin süre "
        "karşılaştırması ve aktarmalar maps_url'deki Google Haritalar toplu taşıma tarifinde."
    )
    return TransitDirections(
        origin=o, destination=d, straight_km=round(haversine_km(o.lat, o.lon, d.lat, d.lon), 1),
        nearby_stations=near, maps_url=transit_maps_url(o, d), meta=meta,
    )
