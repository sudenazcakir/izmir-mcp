from mcp import Client

from izmir_mcp.server import mcp

from .conftest import load


async def test_tools_listed():
    async with Client(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert {"get_stop_arrivals", "find_pharmacies"} <= names


async def test_call_find_pharmacies_structured(fake):
    fake.routes["/api/ibb/nobetcieczaneler"] = (200, load("nobetci.json"))
    async with Client(mcp) as c:
        res = await c.call_tool("find_pharmacies", {"district": "Buca"})
    assert not res.is_error
    assert res.structured_content["district"] == "Buca"
    assert res.structured_content["pharmacies"]
