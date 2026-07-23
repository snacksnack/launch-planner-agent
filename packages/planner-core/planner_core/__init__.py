"""planner_core — the deterministic heart of the launch planner.

Task graph, dependency model, CPM/critical-path scheduling, validation, and
plan-store models live here. This package has **zero LLM dependencies** by
design: the scheduling math must be inspectable and testable without any model
in the loop. The `agents` package depends on this one, never the reverse — a
rule enforced in CI by import-linter.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]

