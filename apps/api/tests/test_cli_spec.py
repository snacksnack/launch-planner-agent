"""RC1-291 — the `plan spec review` verb: exit codes, JSON, and the free path."""

from __future__ import annotations

import json
from pathlib import Path

from app.cli import main
from planner_core.spec_gate import SpecReview

VAGUE = "fixtures/spec-gate/vague-spec.md"
GOOD = "fixtures/spec-gate/good-spec.md"


def test_structural_only_reviews_the_vague_spec_without_a_credential(capsys):
    assert main(["spec", "review", VAGUE, "--structural-only"]) == 0
    out = capsys.readouterr().out
    assert "ADVISORY" in out
    assert "missing-requirement-ids" in out
    assert "Success metrics: TBD." in out


def test_structural_only_good_spec_is_clean(capsys):
    assert main(["spec", "review", GOOD, "--structural-only"]) == 0
    out = capsys.readouterr().out
    assert "0 structural" in out
    assert "clean review is a valid, complete answer" in out


def test_json_output_parses_back_into_the_model(capsys):
    assert main(["spec", "review", VAGUE, "--structural-only", "--json"]) == 0
    review = SpecReview.model_validate(json.loads(capsys.readouterr().out))
    assert review.source_document == VAGUE
    assert review.verdict.value == "advisory"
    assert len(review.structural_findings) == 5


def test_missing_file_exits_2(capsys):
    assert main(["spec", "review", "no/such/spec.md", "--structural-only"]) == 2
    assert "not found" in capsys.readouterr().err


def test_unknown_fail_on_category_exits_2(capsys):
    assert main(["spec", "review", VAGUE, "--structural-only", "--fail-on", "nonsense"]) == 2
    err = capsys.readouterr().err
    assert "unknown --fail-on category" in err
    assert "conflicting_requirement" in err  # the valid values are listed


def test_fail_on_gates_only_on_surviving_rubric_findings(capsys, monkeypatch):
    """--fail-on with a fake agent: a blocked category flips the exit code."""
    from datetime import UTC, datetime

    import agents as agents_pkg
    from planner_core.provenance import Confidence, Provenance
    from planner_core.spec_gate import FindingCategory, SpecFinding, SpecSeverity

    quote = "All users will be cut over in a single weekend migration"

    class FakeAgent:
        def __init__(self, *, model=None, client=None):
            pass

        def run(self, spec_text, structural):
            return SpecReview(
                structural_findings=list(structural),
                findings=[
                    SpecFinding(
                        category=FindingCategory.CONFLICTING_REQUIREMENT,
                        severity=SpecSeverity.BLOCKER,
                        suggested_rewrite=None,
                        provenance=Provenance(
                            reasoning="conflicts with the 90-day requirement",
                            source_quote=quote,
                            source_section="Rollout",
                            confidence=Confidence.HIGH,
                            agent="spec-review",
                            model="claude-test",
                            timestamp=datetime(2026, 8, 18, tzinfo=UTC),
                        ),
                    )
                ],
                rubric_version=1,
            )

    monkeypatch.setattr(agents_pkg, "SpecReviewAgent", FakeAgent)

    assert main(["spec", "review", VAGUE, "--fail-on", "conflicting_requirement"]) == 1
    capsys.readouterr()
    # Same review, ungated category configured: advisory exit.
    assert main(["spec", "review", VAGUE, "--fail-on", "unowned_scope"]) == 0
    assert "BLOCKED" not in capsys.readouterr().out


# --- spec gate: gate the spec, then plan it (RC1-293) ----------------------


def _fake_wbs_agent(monkeypatch):
    """A deterministic WorkBreakdownAgent double, installed via monkeypatch."""
    from datetime import UTC, datetime

    import agents as agents_pkg
    from planner_core import Epic, Task, ThreePointEstimate, WorkBreakdown
    from planner_core.provenance import Confidence, Provenance

    prov = Provenance(
        reasoning="stated in the PRD",
        source_quote="not a verbatim quote — flagged as a warning, which is allowed",
        source_section=None,
        confidence=Confidence.HIGH,
        agent="work-breakdown",
        model="claude-test",
        timestamp=datetime(2026, 8, 18, tzinfo=UTC),
    )

    class FakeWbs:
        def __init__(self, *, model=None, client=None):
            pass

        def run(self, prd_text, team):
            return WorkBreakdown(
                epics=[Epic(id="epic-1", name="Migration", description=None, provenance=prov)],
                tasks=[
                    Task(
                        id="task-1",
                        name="Migrate projects",
                        description=None,
                        epic_id="epic-1",
                        owner_id=None,
                        estimate=ThreePointEstimate(optimistic=2, likely=4, pessimistic=8),
                        provenance=prov,
                    )
                ],
            )

    monkeypatch.setattr(agents_pkg, "WorkBreakdownAgent", FakeWbs)


FIXTURE = "fixtures/jira-cloud-migration"


def test_gate_writes_sidecar_then_plans_and_the_chain_still_resolves(
    tmp_path, capsys, monkeypatch
):
    from app.cli import resolve_prd, spec_review_sidecar
    from planner_core import Plan
    from planner_core.spec_gate import SpecReview

    _fake_wbs_agent(monkeypatch)
    out = tmp_path / "plan.json"

    assert main(["spec", "gate", FIXTURE, "--structural-only", "--out", str(out)]) == 0

    # The review sidecar sits beside the plan and loads through the model.
    review = SpecReview.model_validate_json(spec_review_sidecar(out).read_text())
    assert review.source_document == f"{FIXTURE}/prd.md"
    # The full chain: the written plan's source_document still resolves to the
    # PRD text, which is what dependencies/raid/status re-read (RC1-293's AC).
    plan = Plan.model_validate_json(out.read_text())
    assert resolve_prd(plan, out, None) == (Path(FIXTURE) / "prd.md").read_text()
    assert "proceeding to work breakdown" in capsys.readouterr().out


def test_gate_is_additive_the_plan_matches_a_plain_breakdown(tmp_path, capsys, monkeypatch):
    _fake_wbs_agent(monkeypatch)
    gated, plain = tmp_path / "gated.json", tmp_path / "plain.json"

    assert main(["spec", "gate", FIXTURE, "--structural-only", "--out", str(gated)]) == 0
    assert main(["breakdown", FIXTURE, "--out", str(plain)]) in (0, 1)

    assert gated.read_text() == plain.read_text()


def test_gate_blocked_refuses_to_plan(tmp_path, capsys, monkeypatch):
    import agents as agents_pkg

    _fake_wbs_agent(monkeypatch)

    from datetime import UTC, datetime

    from planner_core.provenance import Confidence, Provenance
    from planner_core.spec_gate import (
        FindingCategory,
        SpecFinding,
        SpecReview,
        SpecSeverity,
    )

    class FakeSpecAgent:
        def __init__(self, *, model=None, client=None):
            pass

        def run(self, spec_text, structural):
            return SpecReview(
                findings=[
                    SpecFinding(
                        category=FindingCategory.CONFLICTING_REQUIREMENT,
                        severity=SpecSeverity.BLOCKER,
                        suggested_rewrite=None,
                        provenance=Provenance(
                            reasoning="cannot both hold",
                            # Verbatim from the flagship PRD, so it survives verification.
                            source_quote=" ".join(
                                (Path(FIXTURE) / "prd.md").read_text().split()[:6]
                            ),
                            source_section=None,
                            confidence=Confidence.HIGH,
                            agent="spec-review",
                            model="claude-test",
                            timestamp=datetime(2026, 8, 18, tzinfo=UTC),
                        ),
                    )
                ]
            )

    monkeypatch.setattr(agents_pkg, "SpecReviewAgent", FakeSpecAgent)
    out = tmp_path / "plan.json"

    code = main(
        ["spec", "gate", FIXTURE, "--fail-on", "conflicting_requirement", "--out", str(out)]
    )

    assert code == 1
    assert not out.exists()  # blocked means no plan was produced
    assert "BLOCKED" in capsys.readouterr().err


def test_gate_missing_prd_exits_2(tmp_path, capsys):
    assert main(["spec", "gate", str(tmp_path), "--structural-only"]) == 2
    assert "prd.md not found" in capsys.readouterr().err
