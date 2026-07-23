"""agents — the LLM judgment layer of the launch planner.

Work-breakdown, dependency, RAID, and status agents live here. They *propose*
structured output (schema-forced against the planner_core models); planner_core
*validates* deterministically; a human *approves*. This package may import
planner_core; the reverse is forbidden (enforced in CI).
"""

from planner_core import (
    __version__ as planner_core_version,  # noqa: F401  (dependency-direction smoke import)
)

__version__ = "0.1.0"

__all__ = ["__version__"]
