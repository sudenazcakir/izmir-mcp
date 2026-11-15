"""İzmir Open Data MCP sunucusu.

Yerel (Claude Desktop):   izmir-mcp            -> stdio
Uzak (Render, 3. hafta):  izmir-mcp --http     -> Streamable HTTP, /mcp
"""

import argparse
import logging
import os
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import tools
from .models import PharmacyResult, SourceStatusResult, StopArrivals

mcp = MCPServer(
    name="izmir-open-data",
    title="İzmir Açık Veri",
    instructions=(
        "İzmir Büyükşehir Belediyesi açık verisi: ESHOT otobüs varışları, nöbetçi eczaneler. "
        "Varış süreleri tahminidir (ESHOT dakika vermez). Bir araç `error` döndürürse kullanıcıya "
        "kaynağın şu an cevap vermediğini açıkça söyle, veri uydurma. `circuit_open` hatası, kaynağın art arda "
        "hata verdiği için bir süre denenmediği anlamına gelir. "
        "`meta.stale` true ise veri eskidir, bunu belirt. Hangi kaynakların ayakta olduğunu `get_source_status` söyler."
    ),
    version="0.1.0",
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=True)


@mcp.tool(
    title="Durağa yaklaşan otobüsler",
    description=(
        "Bir ESHOT otobüs durağına yaklaşan otobüsleri ve tahmini varış süresini (dakika) verir. "
        "`stop` durak numarası (ör. '40010') ya da durak adı olabilir (ör. 'Bornova Metro', 'Konak İskele'). "
        "Aynı adlı birden çok peron varsa hepsine bakar. Süreler kalan durak sayısı ve otobüs konumundan tahmindir."
    ),
    annotations=READ_ONLY,
)
async def get_stop_arrivals(
    stop: Annotated[str, Field(description="Durak numarası veya durak adı")],
    limit: Annotated[int, Field(ge=1, le=20, description="En fazla kaç otobüs listelensin")] = 8,
) -> StopArrivals:
    return await tools.get_stop_arrivals(stop, limit)


@mcp.tool(
    title="Eczane bul",
    description=(
        "İzmir'de bir ilçedeki (ör. 'Buca', 'Bornova') eczaneleri ilçe merkezine yakınlığa göre listeler. "
        "Varsayılan olarak bugünün NÖBETÇİ eczanelerini döndürür (on_duty=false: tüm eczaneler). "
        "İlçe yerine lat/lon verilirse o noktaya en yakınları döndürür."
    ),
    annotations=READ_ONLY,
)
async def find_pharmacies(
    district: Annotated[str | None, Field(description="İzmir ilçesi, ör. 'Buca'")] = None,
    on_duty: Annotated[bool, Field(description="Sadece nöbetçi eczaneler")] = True,
    limit: Annotated[int, Field(ge=1, le=20)] = 5,
    lat: Annotated[float | None, Field(description="Enlem (isteğe bağlı)")] = None,
    lon: Annotated[float | None, Field(description="Boylam (isteğe bağlı)")] = None,
) -> PharmacyResult:
    return await tools.find_pharmacies(district, on_duty, limit, lat, lon)


@mcp.tool(
    title="Veri kaynaklarının durumu",
    description=(
        "İzmir veri kaynaklarının (ESHOT, eczaneler, Metro, İZBAN, İZDENİZ…) şu an ayakta olup olmadığını söyler. "
        "Bir araç hata döndürdüğünde ya da kullanıcı 'sistem çalışıyor mu' diye sorduğunda kullan. "
        "Art arda hata veren kaynak bir süre 'down' sayılır ve ona istek atılmaz."
    ),
    annotations=READ_ONLY,
)
async def get_source_status(
    probe: Annotated[bool, Field(description="Kaynaklara şimdi küçük bir deneme isteği at (false: son bilinen durum)")]
    = True,
) -> SourceStatusResult:
    return await tools.get_source_status(probe)


def main() -> None:
    parser = argparse.ArgumentParser(description="İzmir Open Data MCP sunucusu")
    parser.add_argument("--http", action="store_true", help="stdio yerine Streamable HTTP ile çalış")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)  # stdio modunda loglar stderr'e gider, protokolü bozmaz
    if args.http:
        mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
