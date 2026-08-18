import planner_core


def test_planner_core_imports_and_has_version():
    assert planner_core.__version__


def test_planner_core_has_no_llm_dependency():
    """The deterministic core must never pull in the LLM SDK at import time."""
    import sys

    # Importing planner_core should not have imported anthropic transitively.
    assert "anthropic" not in sys.modules


def test_spec_gate_is_importable_and_stays_deterministic():
    """spec_gate (RC1-229) lives inside the core, so the same boundary applies.

    Only the SDK is asserted here: `agents` legitimately sits in sys.modules
    once its own tests have run in the same process. Keeping spec_gate from
    importing `agents` is the import-linter contract's job (ADR-0038).
    """
    import sys

    import planner_core.spec_gate  # noqa: F401

    assert "anthropic" not in sys.modules
