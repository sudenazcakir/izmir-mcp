from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    izmir_api_base: str = "https://openapi.izmir.bel.tr"
    eshot_api_base: str = "https://acikveriwapi.izmir.bel.tr"
    http_timeout_seconds: float = 8.0
    http_retries: int = 3

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
