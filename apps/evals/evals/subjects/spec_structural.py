"""Spec-gate structural checks — the free half of RC1-292.

Deterministic, credential-free, zero-cost: `run_structural_checks` over the
spec-gate corpus, scored against the exact structural golden. The pytest suite
already asserts the same equality; this subject exists so the property shows up
on the trend page with a version attached, and so a cost above zero — a model
creeping into the deterministic half — surfaces loudly (the groundedness
subject's reasoning, unchanged).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_evals.case import Case
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage
from planner_core.spec_gate import SpecReview, parse_sections, run_structural_checks

NAME = "spec-structural"

_REPO_ROOT = Path(__file__).resolve().parents[4]
SPEC_GATE = _REPO_ROOT / "fixtures" / "spec-gate"

CASES: tuple[Case, ...] = (
    Case(
        id="vague-spec",
        input={"spec": "vague-spec.md"},
        expect=("matches-the-structural-golden",),
        tags=("spec-gate", "structural"),
    ),
    Case(
        id="good-spec",
        input={"spec": "good-spec.md"},
        expect=("clean-on-the-good-spec",),
        tags=("spec-gate", "structural"),
    ),
)


def version() -> SubjectVersion:
    from mcp_server import __version__ as code_version

    return SubjectVersion(
        subject=NAME,
        code_version=code_version,
        # Deterministic — no model, no prompt. See `SubjectVersion`.
        model=None,
        prompt_version=None,
    )


def _golden_structural(spec: str):
    golden = json.loads((SPEC_GATE / "golden-findings.json").read_text())
    return SpecReview.model_validate(golden[spec]).structural_findings


def run(case: Case, tmp_root: Path, client=None) -> CaseResult:
    spec = case.input["spec"]
    started = time.perf_counter()
    produced = run_structural_checks(parse_sections((SPEC_GATE / spec).read_text()))
    latency_ms = (time.perf_counter() - started) * 1000

    expected = _golden_structural(spec)
    if "clean-on-the-good-spec" in case.expect:
        result = CharacteristicResult(
            name="clean-on-the-good-spec",
            passed=produced == [] and expected == [],
            detail=(
                "zero structural findings, as the golden expects"
                if not produced
                else f"{len(produced)} finding(s) on the good spec: "
                + ", ".join(f.code for f in produced)
            ),
        )
    else:
        result = CharacteristicResult(
            name="matches-the-structural-golden",
            passed=produced == expected,
            detail=(
                f"exact match: {len(expected)} finding(s)"
                if produced == expected
                else f"produced {[f.code for f in produced]} vs golden "
                f"{[f.code for f in expected]}"
            ),
        )
    return CaseResult(
        case_id=case.id,
        characteristics=[result],
        usage=Usage(latency_ms=latency_ms),
        observations={"codes": [f.code for f in produced]},
    )
