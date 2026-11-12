# İzmir Open Data MCP — doğrulanmış veri kaynakları

Test tarihi: 2026-11-06. Tüm `openapi.izmir.bel.tr` uç noktaları **API anahtarı istemeden** cevap verdi.
Taban adres: `https://openapi.izmir.bel.tr`

## Canlı veri

| Kaynak | Uç nokta | Sonuç | Not |
|---|---|---|---|
| ESHOT durağa yaklaşan otobüsler | `/api/iztek/duragayaklasanotobusler/{durakId}` | ✅ 200, durak 40010 için 10+ otobüs | Dakika YOK. `KalanDurakSayisi`, otobüs koordinatı (`KoorX`=enlem, `KoorY`=boylam, virgüllü ondalık), `HatNumarasi`, `HatAdi`, `EngelliMi`. Varış süresini biz tahmin etmeliyiz (kalan durak × ortalama süre veya konumdan mesafe). Durak 10006 boş liste döndü (o an yaklaşan yok veya geçersiz ID). |
| ESHOT hattın durağa yaklaşan otobüsleri | `/api/iztek/hattinyaklasanotobusleri/{hatNo}/{durakId}` | ✅ 200 | Aynı alanlar, tek hat için. |
| ESHOT hattaki otobüs konumları | `/api/iztek/hatotobuskonumlari/{hatNo}` | ✅ 200 | `HatOtobusKonumlari[]`: `OtobusId`, `Yon`, koordinat. |
| Nöbetçi eczaneler | `/api/ibb/nobetcieczaneler` | ✅ 200, bugünün tarihli listesi | `Adi`, `Adres`, `Telefon`, `Bolge`, `BolgeAciklama`, `LokasyonX`(enlem) / `LokasyonY`(boylam). |
| Hava kalitesi | `/api/ibb/cevre/havadegerleri` | ✅ 200 ama **günlük** | `OlcumTarihi` dünün tarihi; istasyon (`BolgeAdi`: BORNOVA, CIGLI, SIRINYER…), `GazAdi` (PM10…), `OlcumDegeri`. "Şu an" için saatlik değil → saatlik veri için Open-Meteo Air Quality yedeği/ek kaynağı gerekli. |

## Statik / tarife

| Kaynak | Uç nokta | Sonuç |
|---|---|---|
| Eczane listesi | `/api/ibb/eczaneler` | ✅ tüm eczaneler, koordinatlı |
| Metro istasyonları | `/api/metro/istasyonlar` | ✅ `Adi`, `Enlem`, `Boylam`, `Sira` |
| Metro sefer aralıkları | `/api/metro/sefersaatleri` | ✅ saat aralığı başına `Aralik` (dk) — sefer listesi değil, sıklık |
| İZBAN istasyonları | `/api/izban/istasyonlar` | ✅ |
| İZBAN sefer saatleri | `/api/izban/sefersaatleri/{kalkisId}/{varisId}` | ✅ kalkış/varış saatleri |
| İZDENİZ iskeleleri | `/api/izdeniz/iskeleler` | ✅ koordinatlı |
| İZDENİZ günler | `/api/izdeniz/gunler` | ✅ GunId 1=Pazartesi … 7=Pazar |
| İZDENİZ iskeleden seferler | `/api/izdeniz/iskelesefersaatleri/{iskeleId}/{gunId}` | ✅ kalkış saati, varış iskeleleri, iptal bilgisi |
| İZDENİZ iki iskele arası | `/api/izdeniz/vapursaatleri/{kalkis}/{varis}/{gunTipi}/{detay}` | denenmedi |
| Muhtarlıklar (mahalle koordinatı) | `/api/ibb/cbs/muhtarliklar` | ✅ sayfalı, 1297 kayıt |
| Noktaya yakın duraklar | `/api/ibb/cbs/noktayayakinduraklar?x=&y=&inCoordSys=&outCoordSys=` | portalda listeli, denenmedi |

## CSV / GTFS dosyaları (henüz denenemedi)

Portal kataloğu (`acikveri.bizizmir.com/api/3/action/package_search`) çalışıyor. Dosyaların kendisi başka sunucularda:
- `openfiles.izmir.bel.tr` → ESHOT duraklar, hatlar, hareket saatleri, güzergahlar, **hat duyuruları** CSV'leri (bu ortamda engelli, izin listesine eklenmeli)
- GTFS: `www.eshot.gov.tr/gtfs/bus-eshot-gtfs.zip`, `www.izmirmetro.com.tr/gtfs/rail-metro-gtfs.zip`, `www.izdeniz.com.tr/gtfs/ship-izdeniz-gtfs.zip`
- `/api/ibb/duyurular` yok (404). Belediye genel duyuruları için başka kaynak aranmalı; ESHOT hat duyuruları CSV'si var.
