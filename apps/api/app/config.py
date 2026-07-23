"""Application configuration, sourced from the environment (pydantic-settings).

All settings are env-backed with the ``LPA_`` prefix and documented in
``.env.example``. Credentials are optional so the app can boot and report a
config sanity check without any secrets present.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LPA_",
        extra="ignore",
    )

    app_name: str = "launch-planner-agent"
    environment: str = "development"

    # Where committed plan-of-record snapshots and baselines live (P1.8+).
    database_url: str = "sqlite:///./launch_planner.db"

    # LLM layer — optional so the service boots without credentials.
    anthropic_api_key: str | None = Field(default=None, repr=False)
    anthropic_model: str = "claude-sonnet-5"

    @property
    def has_llm_credentials(self) -> bool:
        return bool(self.anthropic_api_key)

    def sanity_check(self) -> dict[str, object]:
        """A credential-free summary of effective configuration."""
        return {
            "app_name": self.app_name,
            "environment": self.environment,
            "database_url": self.database_url,
            "anthropic_model": self.anthropic_model,
            "anthropic_api_key": "set" if self.has_llm_credentials else "missing",
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
