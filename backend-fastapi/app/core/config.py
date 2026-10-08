from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    environment: str = "development"
    database_url: str = "sqlite:///./connects_senior.db"
    cors_origins: str = "http://localhost:4200"
    firebase_project_id: str = ""
    firebase_credentials_path: str = ""
    firebase_web_api_key: str = ""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def firebase_credentials_file(self) -> Path | None:
        configured_path = self.firebase_credentials_path.strip()
        if not configured_path:
            return None

        credentials_path = Path(configured_path)
        if credentials_path.is_absolute():
            return credentials_path
        return (BACKEND_DIR / credentials_path).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
