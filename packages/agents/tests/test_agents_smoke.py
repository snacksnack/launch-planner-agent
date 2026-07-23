import agents


def test_agents_imports_and_has_version():
    assert agents.__version__


def test_agents_depends_on_planner_core():
    """Dependency direction: agents can see planner_core."""
    import planner_core

    assert agents.planner_core_version == planner_core.__version__
