from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    host: str = Field("127.0.0.1", validation_alias="APP_FETCH_API_HOST")
    port: int = Field(8080, validation_alias="APP_FETCH_API_PORT")
    cors_origins: str = Field("http://localhost:4200,http://127.0.0.1:4200", validation_alias="APP_CORS_ORIGINS")
    headless: bool = Field(False, validation_alias="APP_AUTH_BROWSER_HEADLESS")
    crawl_headless: bool = Field(True, validation_alias="APP_CRAWL_BROWSER_HEADLESS")
    douyin_crawl_headless: bool = Field(False, validation_alias="APP_DOUYIN_CRAWL_HEADLESS")
    security_verification_timeout_seconds: int = Field(
        180, ge=30, le=600, validation_alias="APP_SECURITY_VERIFICATION_TIMEOUT_SECONDS"
    )
    cookie_secure: bool = Field(False, validation_alias="APP_COOKIE_SECURE")
    internal_api_token: str = Field("change-this-internal-token", validation_alias="APP_INTERNAL_API_TOKEN")
    analyze_api_host: str = Field("127.0.0.1", validation_alias="APP_ANALYZE_API_HOST")
    analyze_api_port: int = Field(8090, validation_alias="APP_ANALYZE_API_PORT")
    mimo_api_key: str = Field("", validation_alias="APP_MIMO_API_KEY")
    data_dir_value: Path = Field(Path("fetch-api/data"), validation_alias="APP_DATA_DIR")
    database_url: str = Field("sqlite:///fetch-api/data/fetch.db", validation_alias="APP_DATABASE_URL")
    admin_username: str = Field("root", validation_alias="APP_ADMIN_USERNAME")
    admin_password: str = Field("change-this-password", validation_alias="APP_ADMIN_PASSWORD")
    public_video_url_ttl_seconds: int = Field(1800, ge=60, le=86400, validation_alias="APP_PUBLIC_VIDEO_URL_TTL_SECONDS")
    crawl_delay_sloth: float = Field(10, ge=0, validation_alias="APP_CRAWL_DELAY_SLOTH")
    crawl_delay_human: float = Field(5, ge=0, validation_alias="APP_CRAWL_DELAY_HUMAN")
    crawl_delay_default: float = Field(1, ge=0, validation_alias="APP_CRAWL_DELAY_DEFAULT")
    crawl_delay_flash: float = Field(0, ge=0, validation_alias="APP_CRAWL_DELAY_FLASH")
    max_concurrent_crawls: int = Field(2, ge=1, le=32, validation_alias="APP_MAX_CONCURRENT_CRAWLS")
    crawl_proxies: str = Field("", validation_alias="APP_CRAWL_PROXIES")
    # CDP mode (upstream MediaCrawler default): reuse the user's real Chrome over
    # the DevTools protocol for the lowest risk-control odds.
    douyin_cdp_enabled: bool = Field(False, validation_alias="APP_DOUYIN_CDP_ENABLED")
    douyin_cdp_url: str = Field("http://127.0.0.1:9222", validation_alias="APP_DOUYIN_CDP_URL")
    douyin_cdp_connect_timeout: int = Field(30, ge=5, le=120, validation_alias="APP_DOUYIN_CDP_CONNECT_TIMEOUT_SECONDS")
    # Feishu (Lark) spreadsheet export. Create a self-built app at
    # https://open.feishu.cn with the "sheets:spreadsheet" permission.
    feishu_app_id: str = Field("", validation_alias="FEISHU_APP_ID")
    feishu_app_secret: str = Field("", validation_alias="FEISHU_APP_SECRET")
    feishu_spreadsheet_token: str = Field("", validation_alias="FEISHU_SPREADSHEET_TOKEN")
    # Human owner openid (ou_...) for auto-created spreadsheets; the export
    # transfers the app-created document's ownership to this account.
    feishu_owner_openid: str = Field("", validation_alias="FEISHU_OWNER_OPENID")

    @property
    def data_dir(self) -> Path:
        return self.data_dir_value if self.data_dir_value.is_absolute() else ROOT_DIR / self.data_dir_value

    @property
    def database_path(self) -> Path:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("The current database adapter only supports sqlite:/// URLs")
        path = Path(self.database_url[len(prefix):])
        return path if path.is_absolute() else ROOT_DIR / path

    @property
    def auth_dir(self) -> Path:
        return self.data_dir / "auth"

    @property
    def crawl_delays(self) -> dict[str, float]:
        return {"sloth": self.crawl_delay_sloth, "human": self.crawl_delay_human,
                "default": self.crawl_delay_default, "flash": self.crawl_delay_flash}


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.auth_dir.mkdir(parents=True, exist_ok=True)
settings.database_path.parent.mkdir(parents=True, exist_ok=True)
