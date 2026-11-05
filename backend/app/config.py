"""Application configuration separate from the per-call real-time voice settings."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide database, legacy simulation, logging, and administrator-access
    settings. Pydantic reads environment variables before the current directory's .env and
    defaults. API_KEY remains SecretStr; real-time provider/ingress configuration lives
    separately in VoiceSettings. Application lifespan owns resources built from these
    values.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./pa.db"
    provider_mode: str = "mock"
    log_level: str = "INFO"
    api_key: SecretStr | None = None
    otlp_endpoint: str | None = Field(default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")
