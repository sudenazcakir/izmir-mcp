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
    """Hava kalitesi ölçümü (2. hafta, Bahar). Şimdiden ortak modelde duruyor."""

    station: str
    pollutant: str = Field(description="PM10, PM2.5, NO2, SO2, O3, CO ...")
    value: float
    unit: str = "µg/m³"
    measured_at: datetime
    resolution: str = Field(default="daily", description="'daily' (belediye) veya 'hourly' (Open-Meteo)")
    source: str
    lat: float | None = None
    lon: float | None = None


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
