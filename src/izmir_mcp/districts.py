"""İzmir'in 30 ilçesi ve merkez koordinatları.

Koordinatlar `/api/ibb/cbs/muhtarliklar` verisinden (1297 muhtarlık) her ilçedeki muhtarlıkların
medyan enlem/boylamı alınarak hesaplandı (2026-11-11). Gece yükleme işi aynı hesabı tekrar yapıp
`districts` tablosunu günceller; bu liste veritabanı yokken de araçların çalışması içindir.
"""

from .geo import haversine_km
from .models import District
from .text import norm

# (belediye ILCEID, ad, enlem, boylam, muhtarlık sayısı)
_DATA: list[tuple[int, str, float, float, int]] = [
    (3, "Aliağa", 38.7932, 27.0250, 31),
    (14, "Balçova", 38.3912, 27.0568, 8),
    (7, "Bayındır", 38.2225, 27.6454, 59),
    (11, "Bayraklı", 38.4704, 27.1655, 25),
    (999, "Bergama", 39.1773, 27.2000, 137),
    (993, "Beydağ", 38.0894, 28.2213, 25),
    (2, "Bornova", 38.4465, 27.2195, 45),
    (8, "Buca", 38.3861, 27.1702, 47),
    (995, "Dikili", 39.0880, 26.9032, 30),
    (4, "Foça", 38.6792, 26.8526, 16),
    (15, "Gaziemir", 38.3223, 27.1363, 16),
    (1, "Güzelbahçe", 38.3581, 26.8866, 12),
    (19, "Karabağlar", 38.3853, 27.1086, 58),
    (1000, "Karaburun", 38.6119, 26.4889, 16),
    (20, "Karşıyaka", 38.4697, 27.1084, 27),
    (6, "Kemalpaşa", 38.4169, 27.4831, 49),
    (996, "Kınık", 39.0756, 27.3815, 37),
    (994, "Kiraz", 38.2257, 28.2698, 56),
    (21, "Konak", 38.4149, 27.1400, 112),
    (13, "Menderes", 38.1975, 27.1352, 44),
    (17, "Menemen", 38.6089, 27.0727, 65),
    (5, "Narlıdere", 38.3932, 27.0056, 11),
    (998, "Ödemiş", 38.2284, 27.9859, 98),
    (12, "Seferihisar", 38.1948, 26.8427, 23),
    (10, "Selçuk", 37.9475, 27.3705, 14),
    (997, "Tire", 38.0845, 27.7357, 88),
    (9, "Torbalı", 38.1717, 27.3720, 60),
    (18, "Urla", 38.3229, 26.7621, 37),
    (1001, "Çeşme", 38.3180, 26.3300, 25),
    (16, "Çiğli", 38.5044, 27.0603, 26),
]

DISTRICTS: list[District] = [
    District(id=i, name=n, lat=la, lon=lo, neighborhood_count=c) for i, n, la, lo, c in _DATA
]
_BY_NORM = {norm(d.name): d for d in DISTRICTS}


def find_district(text: str) -> District | None:
    """'buca', 'BUCA', "Buca'da", 'Bucada' -> Buca."""
    q = norm(text)
    if not q:
        return None
    if q in _BY_NORM:
        return _BY_NORM[q]
    for word in q.split():
        if word in _BY_NORM:
            return _BY_NORM[word]
    # Türkçe ekler: "bucada", "bornovadaki", "karsiyakada"
    for key in sorted(_BY_NORM, key=len, reverse=True):
        if q.replace(" ", "").startswith(key.replace(" ", "")):
            return _BY_NORM[key]
    return None


def nearest_district(lat: float, lon: float) -> District:
    return min(DISTRICTS, key=lambda d: haversine_km(lat, lon, d.lat, d.lon))
