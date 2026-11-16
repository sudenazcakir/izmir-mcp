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
from .models import (
    AirQualityResult,
    AnnouncementsResult,
    PharmacyResult,
    SourceStatusResult,
    StopArrivals,
    TransitDirections,
    WeatherResult,
)

mcp = MCPServer(
    name="izmir-open-data",
    title="İzmir Açık Veri",
    instructions=(
        "İzmir Büyükşehir Belediyesi açık verisi: ESHOT otobüs varışları ve hat duyuruları, nöbetçi eczaneler, "
        "hava kalitesi. "
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
    title="Toplu taşıma yol tarifi",
    description=(
        "İzmir'de bir yerden bir yere toplu taşımayla nasıl gidileceği sorulduğunda kullan (ör. 'Bornova'dan "
        "Konak'a nasıl giderim?'). İki ucu (ilçe, semt, metro/İZBAN istasyonu, iskele ya da durak) koordinata "
        "çevirir, uçlara yakın metro/İZBAN istasyonlarını ve iskeleleri listeler ve Google Haritalar toplu taşıma "
        "tarifi bağlantısı (maps_url) verir. Kullanıcı konumunu paylaştıysa ve 'buradan/konumumdan' diyorsa "
        "origin='konumum' ile origin_lat/origin_lon ver. Başlangıçtaki durağa yaklaşan otobüsler için ayrıca "
        "get_stop_arrivals kullanılabilir."
    ),
    annotations=READ_ONLY,
)
async def get_transit_directions(
    origin: Annotated[str, Field(description="Başlangıç yeri, ör. 'Bornova' ya da 'konumum'")],
    destination: Annotated[str, Field(description="Varış yeri, ör. 'Konak İskele'")],
    origin_lat: Annotated[float | None, Field(description="Kullanıcının enlemi (isteğe bağlı)")] = None,
    origin_lon: Annotated[float | None, Field(description="Kullanıcının boylamı (isteğe bağlı)")] = None,
) -> TransitDirections:
    return await tools.get_transit_directions(origin, destination, origin_lat, origin_lon)


@mcp.tool(
    title="Hava durumu ve giyim önerisi",
    description=(
        "Bir İzmir ilçesi ya da konum için şu anki hava durumunu ve bugünün/yarının sabah (07-11), öğle (12-16), "
        "akşam (17-21) dönemlerini verir: sıcaklık, hissedilen, yağış olasılığı, rüzgâr, UV, hava kalitesi (AQI) "
        "ve her dönem için kurala dayalı giyim önerileri (`clothing`). 'Ne giysem?', 'şemsiye alayım mı?', "
        "'yarın hava nasıl?' gibi sorularda kullan. Veriler Open-Meteo model tahminidir."
    ),
    annotations=READ_ONLY,
)
async def get_weather(
    district: Annotated[str | None, Field(description="İzmir ilçesi, ör. 'Bornova'")] = None,
    lat: Annotated[float | None, Field(description="Enlem (isteğe bağlı)")] = None,
    lon: Annotated[float | None, Field(description="Boylam (isteğe bağlı)")] = None,
    day: Annotated[str, Field(description="'bugün' ya da 'yarın'")] = "bugün",
) -> WeatherResult:
    return await tools.get_weather(district, lat, lon, day)


@mcp.tool(
    title="Hava kalitesi",
    description=(
        "Bir İzmir ilçesinin (ör. 'Bornova') ya da konumun hava kalitesini verir: şu anki Avrupa Hava Kalitesi "
        "İndeksi ve düzeyi, kirletici değerleri (PM2.5, PM10, NO2, SO2, O3, CO), önümüzdeki saatlerin tahmini ve "
        "en yakın belediye istasyonunun son günlük ölçümü. Anlık/saatlik değerler Open-Meteo modelinden (tahmin), "
        "istasyon değerleri belediyeden (gerçek ölçüm ama günlük ve birkaç gün gecikmeli). Cevapta bu farkı belirt. "
        "Dışarı çıkma, yürüme, bisiklet ya da toplu taşıma planı soran sorularda ulaşım araçlarıyla birlikte kullan."
    ),
    annotations=READ_ONLY,
)
async def get_air_quality(
    district: Annotated[str | None, Field(description="İzmir ilçesi, ör. 'Bornova'")] = None,
    lat: Annotated[float | None, Field(description="Enlem (isteğe bağlı)")] = None,
    lon: Annotated[float | None, Field(description="Boylam (isteğe bağlı)")] = None,
    hours: Annotated[int, Field(ge=0, le=36, description="Kaç saatlik tahmin listelensin")] = 12,
) -> AirQualityResult:
    return await tools.get_air_quality(district, lat, lon, hours)


@mcp.tool(
    title="ESHOT hat duyuruları",
    description=(
        "ESHOT otobüs hatlarının güncel duyurularını verir: geçici güzergah değişiklikleri, etkinlik nedeniyle "
        "değişiklikler gibi başlangıç/bitiş tarihli duyurular, ve hattın kalıcı notları (ör. ring hat, olumsuz "
        "havada uğranmayan duraklar). `line` hat numarasıdır (ör. '302'); verilmezse tüm hatların şu an geçerli "
        "duyuruları listelenir. Bir hatta yolculuk planlayan kullanıcıya otobüs varışlarıyla birlikte kontrol et. "
        "Duyurularda yalnızca başlık var; ayrıntı için kullanıcıyı ESHOT'a yönlendir."
    ),
    annotations=READ_ONLY,
)
async def get_announcements(
    line: Annotated[str | None, Field(description="ESHOT hat numarası, ör. '302' (isteğe bağlı)")] = None,
    include_upcoming: Annotated[bool, Field(description="İleri tarihli duyuruları da göster")] = True,
    limit: Annotated[int, Field(ge=1, le=50, description="En fazla kaç duyuru listelensin")] = 20,
) -> AnnouncementsResult:
    return await tools.get_announcements(line, include_upcoming, limit)


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
