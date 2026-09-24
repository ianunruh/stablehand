from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STABLEHAND_", extra="ignore")

    database_url: str = "postgresql+psycopg://stablehand:stablehand@localhost:5432/stablehand"
    secret_key: str = "dev-only-change-me"
    public_url: str = "http://localhost:8000"
    runner_api_url: str = "http://localhost:8000"
    session_cookie_secure: bool = False
    admin_email: str = ""
    admin_password: str = ""
    seed_demo: bool = False
    demo_path: str = ""
    k8s_namespace: str = "stablehand-runners"
    runner_image: str = "ghcr.io/ianunruh/stablehand:main"
    secrets_dir: str = ""

    @property
    def package_dir(self) -> Path:
        return Path(__file__).resolve().parent


@lru_cache
def get_settings() -> Settings:
    return Settings()
