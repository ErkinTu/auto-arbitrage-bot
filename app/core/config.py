from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://car_user:car_password@localhost:5432/car_analytics"

    mashina_api_url: str = "https://mashina.kg/api/mbank-proxy/v1/ads/listings"
    request_timeout_seconds: float = 20.0
    request_delay_seconds: float = 2.0
    request_retry_attempts: int = 5
    request_retry_backoff_seconds: float = 10.0
    user_agent: str = "CarAnalyticsMVP/0.1 (contact: your-email@example.com)"

    allowed_brands: str = "BMW,Audi,Toyota,Kia"
    missing_full_scans_before_deactivation: int = 2
    default_page_size: int = 100
    default_max_pages: int = 5
    default_max_items: int = 500
    default_sync_interval_minutes: int = 60

    api_cache_ttl_seconds: int = 300

    cors_origins: str = "http://localhost:5173,http://localhost:5174,http://localhost:5175"

    telegram_bot_token: str = ""
    telegram_access_chat_id: str = ""  # id закрытой группы/канала вида -100...; бот должен быть админом
    bot_fresh_interval_minutes: int = 25
    bot_fresh_period: str = "1d"
    bot_new_lookback_minutes: int = 60
    bot_below_avg_factor: float = 0.95
    bot_below_avg_min_samples: int = 5
    bot_access_cache_ttl_seconds: int = 600

    @property
    def allowed_brand_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_brands.split(",") if item.strip()]

    @property
    def cors_allow_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
