from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    izmir_api_base: str = "https://openapi.izmir.bel.tr"
    eshot_api_base: str = "https://acikveriwapi.izmir.bel.tr"
    open_meteo_air_base: str = "https://air-quality-api.open-meteo.com"
    open_meteo_forecast_base: str = "https://api.open-meteo.com"
    met_no_base: str = "https://api.met.no"  # Open-Meteo sınıra takılırsa yedek hava kaynağı
    eshot_announcements_csv_url: str = "https://openfiles.izmir.bel.tr/211488/docs/eshot-otobus-hat-duyurulari.csv"
    http_timeout_seconds: float = 8.0
    http_retries: int = 3

    # Demo c) için: virgülle ayrılmış kaynak adları (ör. "ESHOT"). Bu kaynaklara istek atılmaz, çökmüş gibi
    # davranılır. Yalnız demo ve deneme için; canlı sunucuda BOŞ olmalı.
    demo_down_sources: str = ""

    # devre kesici: art arda bu kadar hata -> kaynak bu kadar saniye "kapalı" sayılır
    breaker_failure_threshold: int = 3
    breaker_cooldown_seconds: float = 60.0

    database_url: str | None = None

    avg_minutes_per_stop: float = 1.5
    avg_bus_speed_kmh: float = 18.0
    road_factor: float = 1.3  # kuş uçuşu mesafeyi yol mesafesine çeviren katsayı

    redis_url: str | None = None
    mcp_api_key_required: bool = False

    # Web sohbet sayfası (izmir-mcp-web)
    anthropic_api_key: str | None = None
    chat_model: str = "claude-haiku-5-5"
    chat_effort: str = "low"  # low | medium | high: sohbet için low hızlı ve ucuz
    chat_max_tokens: int = 2048
    chat_rate_limit: int = 20  # IP başına, 10 dakikada en fazla bu kadar soru
    chat_global_rate_limit: int = 200  # tüm IP'ler toplamı, 10 dakikada (harcama tavanı)
    # /mcp ucunu koruyan paylaşılan anahtar. Boşsa /mcp herkese açıktır (yalnız yerelde böyle bırak).
    mcp_api_key: str | None = None
    # Claude API'nin MCP sunucumuza ulaşacağı herkese açık adres. Render bunu RENDER_EXTERNAL_URL olarak kendisi verir.
    public_base_url: str | None = None
    render_external_url: str | None = None

    @property
    def mcp_public_url(self) -> str | None:
        base = self.public_base_url or self.render_external_url
        return f"{base.rstrip('/')}/mcp" if base else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
