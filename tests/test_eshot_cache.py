from izmir_mcp.sources import eshot

from .conftest import load

PATH = "/api/iztek/duragayaklasanotobusler/40010"


async def test_same_stop_hits_eshot_once_per_ttl(fake, monkeypatch):
    fake.routes[PATH] = (200, load("arrivals_40010.json"))
    now = [1000.0]
    monkeypatch.setattr(eshot.time, "monotonic", lambda: now[0])
    a = await eshot.approaching_buses("40010")
    b = await eshot.approaching_buses("40010")
    assert a == b and fake.calls.count(PATH) == 1 and eshot.arrivals_fresh("40010")
    now[0] += eshot.ARRIVALS_TTL + 1
    assert not eshot.arrivals_fresh("40010")
    await eshot.approaching_buses("40010")
    assert fake.calls.count(PATH) == 2
