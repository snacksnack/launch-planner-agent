"""app — FastAPI service for the launch planner.

Owns HTTP ingestion, agent orchestration, and the database connection. Depends
on both planner_core (deterministic) and agents (LLM); nothing depends on it.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
