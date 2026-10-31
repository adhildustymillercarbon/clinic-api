from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application runtime settings and environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_title: str = "Clinic booking API"
    app_version: str = "1.0"
    api_key: str = "secret-api-key"
    database_url: str = "postgresql+asyncpg://clinic:clinic_secret@localhost:5432/clinic_booking"
    redis_url: str = "redis://localhost:6379/0"
    rate_limit_max_attempts: int = 5
    rate_limit_window_seconds: int = 60
    idempotency_ttl_seconds: int = 86400

    # OpenObserve Observability (Traces & Logs)
    openobserve_ai_token: str | None = None
    openobserve_token: str | None = None
    openobserve_endpoint: str = "https://api.openobserve.ai"
    openobserve_org: str = "default"
    openobserve_stream: str = "default"
    openobserve_service_name: str = "clinic-booking"
    openobserve_enabled: bool = True

    @property
    def effective_openobserve_token(self) -> str | None:
        """Return configured OpenObserve auth token."""
        return self.openobserve_ai_token or self.openobserve_token


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings singleton."""
    return Settings()
