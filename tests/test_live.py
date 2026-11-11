"""Gerçek İzmir API'sine karşı duman testi: `pytest -m live`"""

import pytest

from izmir_mcp import tools

pytestmark = pytest.mark.live


async def test_live_bornova_metro():
    r = await tools.get_stop_arrivals("Bornova Metro")
    assert r.error is None, r.error
    assert any(s.name == "Bornova Metro" for s in r.stops)


async def test_live_buca_pharmacies():
    r = await tools.find_pharmacies("Buca")
    assert r.error is None, r.error
    assert r.pharmacies
