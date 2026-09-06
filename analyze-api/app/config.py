from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    host: str = Field("127.0.0.1", validation_alias="APP_ANALYZE_API_HOST")
    port: int = Field(8090, validation_alias="APP_ANALYZE_API_PORT")
    mimo_api_key: str = Field("", validation_alias="APP_MIMO_API_KEY")
    mimo_base_url: str = Field("https://api.xiaomimimo.com/v1", validation_alias="APP_MIMO_BASE_URL")
    mimo_model: str = Field("mimo-v2.5", validation_alias="APP_MIMO_MODEL")
    scan_interval_seconds: int = Field(10, ge=1, validation_alias="APP_ANALYSIS_SCAN_INTERVAL_SECONDS")
    fetch_api_url: str = Field("http://127.0.0.1:8080", validation_alias="APP_FETCH_API_URL")
    internal_api_token: str = Field("change-this-internal-token", validation_alias="APP_INTERNAL_API_TOKEN")
    database_url: str = Field("sqlite:///fetch-api/data/fetch.db", validation_alias="APP_DATABASE_URL")
    max_video_bytes: int = 37_000_000
    max_attempts: int = 3
    video_input_mode: str = Field("auto", pattern="^(auto|base64|url)$", validation_alias="APP_MIMO_VIDEO_INPUT_MODE")
    public_fetch_api_url: str = Field("", validation_alias="APP_PUBLIC_FETCH_API_URL")
    public_video_url_ttl_seconds: int = Field(1800, ge=60, le=86400, validation_alias="APP_PUBLIC_VIDEO_URL_TTL_SECONDS")

    @property
    def database_path(self) -> Path:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("The current database adapter only supports sqlite:/// URLs")
        path = Path(self.database_url[len(prefix):])
        return path if path.is_absolute() else ROOT_DIR / path


settings = Settings()
