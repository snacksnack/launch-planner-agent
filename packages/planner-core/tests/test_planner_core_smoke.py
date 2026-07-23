import planner_core


def test_planner_core_imports_and_has_version():
    assert planner_core.__version__


def test_planner_core_has_no_llm_dependency():
    """The deterministic core must never pull in the LLM SDK at import time."""
    import sys

    # Importing planner_core should not have imported anthropic transitively.
    assert "anthropic" not in sys.modules
