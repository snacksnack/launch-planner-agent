"""MCP server configuration, sourced from the environment (pydantic-settings).

Reuses the ``LPA_`` prefix and the ``.env`` file that ``app.config`` already
reads, so one file configures the API, the CLI, and this server.

Plan-side settings are **not** duplicated here — ``app.config.get_settings()``
stays the single source of truth for ``sqlite_path`` and ``project_start_date``,
and this class adds only what is specific to the MCP layer. Two settings objects
over one ``.env`` is deliberate: duplicating ``plan_path`` here is exactly how
the MCP server and the CLI would end up disagreeing about which plan is current.

Drift settings are optional at boot. The server starts with no drift service
configured and reports it unavailable, so a local client works against the
planner alone.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LPA_",
        extra="ignore",
    )

    # The drift detector (tpm-automation-platform) — the only thing this server
    # reaches over the network. Everything else is an in-process call.
    drift_base_url: str | None = None  # e.g. https://drift.internal
    drift_run_token: str | None = Field(default=None, repr=False)

    # Bounded network behaviour. Deliberately small: an MCP client is
    # interactive, and a model blocked for 30s on a dead service is worse than a
    # prompt "drift is unavailable".
    drift_timeout_seconds: float = Field(default=5.0, gt=0)
    drift_max_attempts: int = Field(default=2, ge=1)  # total tries, not retries

    @property
    def drift_configured(self) -> bool:
        """Drift is configured once we know where it lives.

        The token is separate on purpose: some deployments front the detector
        with network-level auth instead, so a base URL alone is enough to try.
        """
        return bool(self.drift_base_url)


@lru_cache
def get_mcp_settings() -> McpSettings:
    return McpSettings()
