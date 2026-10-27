"""Settings come from environment variables (or a local .env file)."""
import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)
_DEV_SECRET = "dev-only-insecure-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    openai_live_model: str = "gpt-live-1"
    openai_delegation_model: str = "gpt-6-luna"
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "restaurant_voice"
    default_restaurant_id: str = "demo-restaurant"
    app_env: str = "development"
    session_secret: str = ""
    owner_password: str = ""
    max_call_seconds: int = 600
    max_concurrent_calls: int = 5
    idle_timeout_seconds: int = 30  # no heartbeat from the browser -> server closes the call

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    def effective_secret(self) -> str:
        if self.session_secret:
            return self.session_secret
        if self.is_production:
            raise RuntimeError("SESSION_SECRET must be set when APP_ENV=production")
        log.warning("SESSION_SECRET not set; using an insecure development secret")
        return _DEV_SECRET


@lru_cache
def get_settings() -> Settings:
    return Settings()
