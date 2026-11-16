"""Tüm araçların ortak Pydantic modelleri.

Her araç aynı zarfı döndürür: asıl veri + `meta` (kaynak, zaman, önbellek bilgisi) + isteğe bağlı `error`.
Böylece Claude bir kaynak çöktüğünde bile düzgün, açıklanabilir bir cevap alır (Demo Day senaryosu c).
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    SOURCE_UNAVAILABLE = "source_unavailable"  # kaynak cevap vermedi / 5xx / bağlantı koptu
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"  # durak/ilçe bulunamadı
    INVALID_INPUT = "invalid_input"
    BAD_RESPONSE = "bad_response"  # kaynak cevap verdi ama beklenmeyen biçimde
    CIRCUIT_OPEN = "circuit_open"  # kaynak art arda hata verdi, bir süre hiç denenmiyor


class SourceError(BaseModel):
    """Araçların döndürdüğü hata nesnesi. Exception fırlatmak yerine bunu döndürürüz."""

    code: ErrorCode
    message: str = Field(description="Kullanıcıya gösterilebilecek Türkçe açıklama")
    source: str | None = Field(default=None, description="Hataya sebep olan veri kaynağı")
    retryable: bool = False
    hint: str | None = None


class Meta(BaseModel):
    sources: list[str] = Field(description="Kullanılan veri kaynakları")
    fetched_at: datetime
    cached: bool = False
    stale: bool = Field(default=False, description="Kaynak çöktüğü için son iyi veri kullanıldıysa true")
    notes: list[str] = Field(default_factory=list)


class StopRef(BaseModel):
    id: str
    name: str | None = None
    lat: float | None = None
    lon: float | None = None


class Arrival(BaseModel):
    stop_id: str
    line_number: str
    line_name: str
    bus_id: int | None = None
    direction: int | None = None
    stops_remaining: int
    eta_minutes: int = Field(description="Tahmini varış süresi (dakika). ESHOT dakika vermez, biz hesaplarız.")
    eta_method: str = Field(description="'stops' (kalan durak), 'distance' (konum) veya 'blend' (ikisinin ortalaması)")
    distance_m: int | None = None
    wheelchair_accessible: bool | None = None
    bike_rack: bool | None = None
    bus_lat: float | None = None
    bus_lon: float | None = None


class StopArrivals(BaseModel):
    query: str
    stops: list[StopRef] = Field(default_factory=list)
    arrivals: list[Arrival] = Field(default_factory=list)
    meta: Meta
    error: SourceError | None = None


class Pharmacy(BaseModel):
    name: str
    address: str | None = None
    phone: str | None = None
    region: str | None = Field(default=None, description="Eczacı odasının nöbet bölgesi (ilçe değil)")
    region_note: str | None = None
    lat: float | None = None
    lon: float | None = None
    distance_km: float | None = Field(default=None, description="İlçe merkezine (veya verilen noktaya) uzaklık")
    on_duty: bool = False


class PharmacyResult(BaseModel):
    district: str | None = None
    on_duty: bool
    pharmacies: list[Pharmacy] = Field(default_factory=list)
    meta: Meta
    error: SourceError | None = None


class AirReading(BaseModel):
    """Tek bir kirletici değeri: belediye istasyon ölçümü (günlük) ya da Open-Meteo model değeri (saatlik)."""

    station: str
    pollutant: str = Field(description="PM10, PM2.5, NO2, SO2, O3, CO ...")
    value: float
    unit: str = "µg/m³"
    measured_at: datetime
    resolution: str = Field(default="daily", description="'daily' (belediye) veya 'hourly' (Open-Meteo)")
    source: str
    lat: float | None = None
    lon: float | None = None
    distance_km: float | None = Field(default=None, description="Ölçüm noktasının ilçe merkezine uzaklığı")


class HourlyAqi(BaseModel):
    time: datetime
    european_aqi: int | None = None
    level: str | None = Field(default=None, description="Avrupa AQI düzeyi (Türkçe)")
    pm2_5: float | None = None
    pm10: float | None = None


class AirQualityResult(BaseModel):
    district: str | None = None
    european_aqi: int | None = Field(default=None, description="Şu anki Avrupa Hava Kalitesi İndeksi (Open-Meteo, model)")
    level: str | None = Field(default=None, description="AQI düzeyi: iyi, makul, orta, kötü, çok kötü, son derece kötü")
    current: list[AirReading] = Field(default_factory=list, description="Şu anki saatlik değerler (Open-Meteo, model)")
    station_daily: list[AirReading] = Field(
        default_factory=list, description="En yakın belediye istasyonunun son günlük ortalaması (gerçek ölçüm)")
    outlook: list[HourlyAqi] = Field(default_factory=list, description="Önümüzdeki saatlerin AQI tahmini")
    meta: Meta
    error: SourceError | None = None


class SourceStatus(BaseModel):
    source: str
    state: str = Field(description="'up' (ayakta), 'degraded' (son istek hata verdi), 'down' (kapalı, "
                                   "istek atılmıyor), 'recovering' (yeniden deneniyor), 'unknown' (henüz denenmedi)")
    consecutive_failures: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    retry_in_seconds: int | None = Field(default=None, description="Kapalıysa kaç saniye sonra yeniden denenecek")


class SourceStatusResult(BaseModel):
    sources: list[SourceStatus] = Field(default_factory=list)
    all_up: bool
    meta: Meta
    error: SourceError | None = None


class Announcement(BaseModel):
    title: str
    lines: list[str] = Field(description="Duyurunun etkilediği hat numaraları")
    starts_at: datetime
    ends_at: datetime | None = None
    status: str = Field(description="'active' (şu an geçerli) veya 'upcoming' (ileri tarihli)")
    source: str


class LineNote(BaseModel):
    line: str
    note: str = Field(description="Tarihsiz, kalıcı hat notu (ör. ring hat, olumsuz havada uğranmayan duraklar)")
    source: str


class AnnouncementsResult(BaseModel):
    line: str | None = None
    line_name: str | None = None
    announcements: list[Announcement] = Field(default_factory=list)
    line_notes: list[LineNote] = Field(default_factory=list)
    total: int = Field(default=0, description="Filtreye uyan duyuru sayısı (limit uygulanmadan önce)")
    meta: Meta
    error: SourceError | None = None


# --- statik veri (gece yükleme işi ve veritabanı) ---


class District(BaseModel):
    id: int
    name: str
    lat: float
    lon: float
    neighborhood_count: int = 0


class Stop(BaseModel):
    id: str
    name: str
    lat: float | None = None
    lon: float | None = None
    lines: list[str] = Field(default_factory=list)
    district_name: str | None = None


class BusLine(BaseModel):
    number: str
    name: str
    start: str | None = None
    end: str | None = None


class Station(BaseModel):
    system: str  # 'metro' | 'izban'
    source_id: int
    name: str
    lat: float
    lon: float
    seq: int | None = None
    active: bool = True


class Pier(BaseModel):
    id: int
    name: str
    lat: float
    lon: float
    car_ferry: bool = False
    active: bool = True


class TripPoint(BaseModel):
    query: str = Field(description="Kullanıcının yazdığı yer")
    name: str = Field(description="Eşleşen yer (ilçe, istasyon, iskele, durak ya da 'Konumun')")
    kind: str = Field(description="district | metro | izban | pier | stop | user")
    lat: float
    lon: float


class NearbyStation(BaseModel):
    system: str = Field(description="metro | izban | ferry")
    name: str
    distance_km: float
    end: str = Field(description="origin | destination: hangi uca yakın")


class TransitDirections(BaseModel):
    origin: TripPoint | None = None
    destination: TripPoint | None = None
    straight_km: float | None = Field(default=None, description="İki nokta arası kuş uçuşu mesafe")
    nearby_stations: list[NearbyStation] = Field(
        default_factory=list, description="Uçlara 1,5 km'den yakın metro/İZBAN istasyonları ve iskeleler"
    )
    maps_url: str | None = Field(
        default=None, description="Google Haritalar toplu taşıma tarifi (sayfa bunu düğme olarak gösterir)"
    )
    meta: Meta
    error: SourceError | None = None


class WeatherNow(BaseModel):
    temperature_c: float | None = None
    feels_like_c: float | None = None
    description: str | None = None
    wind_kmh: float | None = None
    humidity_pct: float | None = None
    precipitation_mm: float | None = None


class DayPeriod(BaseModel):
    name: str = Field(description="sabah | öğle | akşam | gece")
    hours: str = Field(description="ör. '07-11'")
    temp_min_c: float | None = None
    temp_max_c: float | None = None
    feels_like_min_c: float | None = None
    rain_chance_pct: float | None = Field(default=None, description="En yüksek yağış olasılığı")
    rain_mm: float | None = None
    wind_max_kmh: float | None = None
    uv_max: float | None = None
    description: str | None = Field(default=None, description="Dönemin en kötü hava durumu (ör. 'sağanak')")
    european_aqi_max: float | None = Field(default=None, description="Open-Meteo hava kalitesi tahmini (en kötü saat)")
    aqi_level: str | None = None
    clothing: list[str] = Field(default_factory=list, description="Kurala dayalı giyim önerileri")


class WeatherResult(BaseModel):
    place: str | None = None
    lat: float | None = None
    lon: float | None = None
    day: str = Field(description="bugün | yarın")
    date: str | None = None
    now: WeatherNow | None = None
    periods: list[DayPeriod] = Field(default_factory=list)
    meta: Meta
    error: SourceError | None = None
