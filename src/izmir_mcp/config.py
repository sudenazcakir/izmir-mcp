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


@lru_cache
def get_settings() -> Settings:
    return Settings()
