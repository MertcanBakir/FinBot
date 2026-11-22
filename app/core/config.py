from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "FinBot"
    TIMEZONE: str = "Europe/Istanbul"
    PORT: int = 8000

    # BIST Movers Configuration
    BIST_MOVER_MAX_ABS_CHANGE_PERCENT: float = 15.0

    # SMTP Configuration
    EMAIL_USER: Optional[str] = None
    EMAIL_APP_PASSWORD: Optional[str] = None
    EMAIL_TO: Optional[str] = None
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587

    # Supabase Database Configuration
    SUPABASE_DATABASE_URL: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
