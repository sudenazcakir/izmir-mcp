# İzmir Open Data MCP

İzmir Büyükşehir Belediyesi açık verisini (ESHOT, Metro, İZBAN, İZDENİZ, eczaneler) Claude gibi
yapay zekâ asistanlarına **MCP araçları** olarak sunan sunucu. Yaşar Üniversitesi, Güz 2026 dönem projesi
(Proje 09). Ekip: Sude Naz Çakır, Bahar Dokur.

> "Bornova Metro durağına sıradaki otobüs ne zaman?"  
> "Buca'da nöbetçi eczane hangisi?"

## Araçlar

| Araç | Ne yapar | Kaynak |
|---|---|---|
| `get_stop_arrivals(stop, limit=8)` | Durağa yaklaşan otobüsler ve **tahmini** varış dakikası. `stop` durak numarası (`40010`) ya da ad (`Bornova Metro`) olabilir. | ESHOT canlı, İBB CBS yakın duraklar |
| `find_pharmacies(district, on_duty=True, limit=5, lat?, lon?)` | İlçedeki nöbetçi (ya da tüm) eczaneler, ilçe merkezine uzaklığa göre sıralı. | İzmir Eczacı Odası / İBB |

Her araç aynı zarfı döndürür: veri + `meta` (kaynak, zaman, önbellek, notlar) + kaynak çökerse `error`
(`source_unavailable`, `timeout`, `not_found`, `invalid_input`, `bad_response`). Araçlar hata durumunda
istisna fırlatmaz, açıklamalı bir hata nesnesi döndürür; böylece Claude "kaynak şu an cevap vermiyor" diyebilir.

**Varış süresi nasıl hesaplanıyor?** ESHOT dakika vermiyor, sadece kalan durak sayısı ve otobüsün konumunu
veriyor. Tahmin = (kalan durak × 1,5 dk + otobüsün durağa yol mesafesi ÷ 18 km/sa) / 2. Katsayılar
`.env` içinden ayarlanabilir.

**Durak adı nasıl bulunuyor?** Önce veritabanındaki `stops` tablosuna bakılır. Tablo boşsa ya da
veritabanı yoksa, addaki metro/İZBAN istasyonu, iskele ya da ilçe adının koordinatı bulunur ve o noktaya
yakın duraklar arasından ada göre seçilir. Bornova Metro gibi aktarma merkezlerinde aynı adlı 10 perona
birden bakılır.

## Kurulum

Gereken: Python 3.11+ ve [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/sudenazcakir/izmir-mcp.git
cd izmir-mcp
uv sync --extra dev
cp .env.example .env        # DATABASE_URL'yi doldur (isteğe bağlı)
uv run pytest               # çevrimdışı testler
uv run pytest -m live       # gerçek İzmir API'sine karşı duman testi
```

Veritabanı olmadan da iki araç çalışır; veritabanı durak adı aramasını hızlandırır ve 3. haftadaki API
anahtarları ile kullanım kayıtları için gerekir.

## Veritabanı (Supabase)

1. Supabase projesinde **Connect > Direct (Connection string) > Session pooler** adresini kopyala, şifreyi yerleştir,
   `.env` içine `DATABASE_URL=` olarak yaz. (Direct connection yalnız IPv6, Render'dan bağlanamaz.)
2. Şemayı kur: Supabase **SQL Editor**'e `supabase/migrations/0001_init.sql` dosyasını yapıştırıp çalıştır,
   ya da `psql "$DATABASE_URL" -f supabase/migrations/0001_init.sql`. Dosya tekrar çalıştırılabilir.
3. Statik verileri yükle:

```bash
uv run python jobs/ingest_static.py --dry-run   # önce yazmadan dene
uv run python jobs/ingest_static.py             # districts, stations, piers, pharmacies, stops, lines
```

Tablolar: `districts` (30 ilçe), `stops`, `bus_lines`, `stations` (metro + İZBAN), `piers`, `pharmacies`,
`api_keys`, `usage_logs`. Tüm tablolarda RLS açık ve politika yok: Supabase'in herkese açık REST ucundan
okunamazlar, sunucu doğrudan Postgres bağlantısıyla erişir.

## MCP Inspector ile deneme

```bash
npx @modelcontextprotocol/inspector uv run izmir-mcp
```

Tarayıcıda açılan arayüzde **Connect > Tools > List Tools**, sonra `find_pharmacies` için `district=Buca`.
Arayüzsüz:

```bash
npx @modelcontextprotocol/inspector --cli uv run izmir-mcp --method tools/call \
  --tool-name get_stop_arrivals --tool-arg "stop=Bornova Metro"
```

## Claude Desktop'a bağlama (stdio)

Claude Desktop > **Settings > Developer > Edit Config** ile `claude_desktop_config.json` dosyasını aç ve
şunu ekle (yolu kendi bilgisayarına göre değiştir):

macOS:

```json
{
  "mcpServers": {
    "izmir": {
      "command": "/Users/KULLANICI/.local/bin/uv",
      "args": ["--directory", "/Users/KULLANICI/izmir-mcp", "run", "izmir-mcp"]
    }
  }
}
```

Windows:

```json
{
  "mcpServers": {
    "izmir": {
      "command": "C:\\Users\\KULLANICI\\.local\\bin\\uv.exe",
      "args": ["--directory", "C:\\Users\\KULLANICI\\izmir-mcp", "run", "izmir-mcp"]
    }
  }
}
```

`uv`'nin tam yolunu `which uv` (macOS) ya da `where uv` (Windows) ile bulabilirsin; Claude Desktop
terminalin PATH'ini görmediği için tam yol gerekiyor. Kaydedip Claude Desktop'ı **tamamen kapatıp** yeniden
aç. Sohbet kutusundaki araç simgesinde `izmir` görünmeli. Sorun olursa log: macOS
`~/Library/Logs/Claude/mcp-server-izmir.log`, Windows `%APPDATA%\Claude\logs\mcp-server-izmir.log`.

## Uzak sunucu (3. hafta)

`uv run izmir-mcp --http` Streamable HTTP ile `/mcp` yolunda çalışır (`PORT` ortam değişkeni). Render'a
Docker ile kurulum ve API anahtarı doğrulaması 3. haftada eklenecek.

## Klasör yapısı

```
src/izmir_mcp/
  server.py            MCP sunucusu, araç tanımları
  tools.py             araçların iş mantığı
  models.py            ortak Pydantic modelleri (Arrival, Pharmacy, AirReading, Meta, SourceError…)
  http.py, cache.py    zaman aşımı + tekrar deneme, TTL önbellek + son iyi değer (2. haftada Redis)
  sources/             kaynak başına adaptör (eshot, pharmacy, transit_static)
  districts.py         30 ilçe ve merkez koordinatı
  ingest.py            gece yükleme işinin mantığı
  data/                API çökerse kullanılacak istasyon/iskele anlık görüntüsü
jobs/ingest_static.py  gece yükleme işi
supabase/migrations/   veritabanı şeması
docs/sources.md        denenmiş veri kaynakları ve uç noktalar
tests/                 çevrimdışı testler (+ `-m live` ile canlı testler)
```

## Bilinen sınırlar

- Varış süreleri tahmindir, ESHOT'un resmî dakikası değildir.
- Nöbet bölgeleri (ör. `BUCA 1`, `ŞİRİNYER`) ilçe sınırıyla birebir örtüşmez; ilçe adıyla eşleşenler ve ilçe
  merkezine 5 km'den yakın olanlar birlikte gösterilir.
- ESHOT durak ve hat listesi `acikveriwapi.izmir.bel.tr` üzerinden geliyor; alan adları belgelerde net
  olmadığı için yükleyici birkaç olası yazımı dener. İlk gerçek çalıştırmada `stops` ve `lines` adımlarının
  sonucunu kontrol edin.
- `pharmacies.district_id` en yakın ilçe merkezine göre atanır (yaklaşık).

## Lisans

MIT
