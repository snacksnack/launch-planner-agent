"""Eval harness configuration, sourced from the environment (pydantic-settings).

Reuses the ``LPA_`` prefix and the ``.env`` file that ``app.config`` already
reads, for the same reason ``mcp_server.config`` does: one file configures the
API, the CLI, the MCP server, and this.

Only the run log lives here. Nothing a *subject* needs to run is duplicated —
``app.config`` stays the single source of truth for ``sqlite_path`` and
``plan_path``. A harness that carried its own copy of the subject's settings
would eventually measure a configuration nobody actually runs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class EvalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LPA_",
        extra="ignore",
    )

    # Where local run records accumulate when EVAL_DATABASE_URL is not set.
    # The shared store is Postgres (RC1-263); this JSONL file is the
    # credential-free default for local iteration, and stays gitignored.
    evals_runs_path: str = "./eval-runs/runs.jsonl"

    # The tool-selection probe model (RC1-328). This subject measures the tool
    # *definitions* — any capable model is a probe, unlike the agent subjects
    # where the model under test is the production model. The KPI agent's
    # cost-per-run-by-model monitor flagged sonnet-5 here at 4x haiku with no
    # pass-rate edge (haiku 85.7% vs sonnet's 78.6-92.9% band), so the probe is
    # pinned cheap. Override: LPA_TOOL_SELECTION_MODEL.
    tool_selection_model: str = "claude-haiku-4-5"

    @property
    def runs_path(self) -> Path:
        return Path(self.evals_runs_path)


@lru_cache
def get_eval_settings() -> EvalSettings:
    return EvalSettings()


#: The calibration corpus, committed alongside the code. Lives here rather than
#: in the CLI because subjects read it too, and a subject importing the CLI is a
#: cycle — the CLI is a consumer of these paths, not their owner.
CALIBRATION_DIR = Path(__file__).resolve().parents[1] / "calibration"
SEEDS_PATH = CALIBRATION_DIR / "seeds.jsonl"
LABELS_PATH = CALIBRATION_DIR / "labels.jsonl"
