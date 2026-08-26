from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    bot_username: str = ""
    public_base_url: str = "http://localhost:8000"
    feedback_username: str = ""
    material_channel_id: int = 0
    material_channel_username: str = ""
    superadmin_ids_raw: str = Field(default="", validation_alias="SUPERADMIN_IDS")

    database_url: str = "postgresql+asyncpg://cats:cats@postgres:5432/cats"
    app_secret: str = "development-only-change-me"
    login_code_pepper: str = "development-pepper"

    default_task_limit: int = 10
    login_ttl_seconds: int = 300
    default_timezone: str = "Asia/Shanghai"
    scheduler_concurrency: int = 20

    @property
    def normalized_base_url(self) -> str:
        return self.public_base_url.rstrip("/")

    @property
    def superadmin_ids(self) -> list[int]:
        return [int(item.strip()) for item in self.superadmin_ids_raw.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
