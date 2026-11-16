from urllib.parse import parse_qs, urlparse

from izmir_mcp import tools


async def test_district_to_district(fake):
    r = await tools.get_transit_directions("Bornova", "Konak")
    assert r.error is None
    assert r.origin.name == "Bornova" and r.destination.name == "Konak"
    assert 5 < r.straight_km < 15
    q = parse_qs(urlparse(r.maps_url).query)
    assert q["travelmode"] == ["transit"]
    assert q["origin"][0].startswith(f"{r.origin.lat:.6f}")
    systems = {(n.system, n.end) for n in r.nearby_stations}
    assert ("metro", "origin") in systems and ("metro", "destination") in systems


async def test_iskele_prefers_pier(fake):
    r = await tools.get_transit_directions("Bornova Metro", "Karşıyaka İskele")
    assert r.origin.kind == "metro"
    assert r.destination.kind == "pier" and r.destination.name == "Karşıyaka"


async def test_user_location_as_origin(fake):
    r = await tools.get_transit_directions("konumum", "Alsancak", 38.39, 27.17)
    assert r.origin.kind == "user" and r.origin.lat == 38.39
    assert "origin=38.390000,27.170000" in r.maps_url


async def test_named_origin_wins_over_location(fake):
    r = await tools.get_transit_directions("Bornova", "Konak", 38.0, 27.0)
    assert r.origin.name == "Bornova"


async def test_unknown_place(fake):
    r = await tools.get_transit_directions("Narnia", "Konak")
    assert r.error is not None and r.error.code == "not_found" and "Narnia" in r.error.message
    assert r.maps_url is None
