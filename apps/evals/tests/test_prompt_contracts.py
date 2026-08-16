"""A prompt edit that breaks a characteristic must fail CI (RC1-255).

Free, deterministic, credential-free — and it closes a gap that was real and
invisible until it was measured.

Every prompt-dependent characteristic in this repo is scored by a **billed**
subject, and billed subjects deliberately stay out of `pytest` and out of CI
(ADR-0031) so the suite needs no credentials. The consequence went unnoticed:
editing a system prompt could break a gating characteristic while ruff, pytest
and every free subject stayed green.

It was demonstrated in `tpm-automation-platform` first, by deleting a template
rule that `evals degrade` had *measured* as load-bearing. CI passed.

The fix is the pattern the two n8n repos arrived at independently: each subject
declares the prompt clauses its characteristics depend on, and a free test
asserts they are still there. It cannot prove the prompt is *good* — only that
it still says the things the scoring assumes. That is a smaller claim than the
billed suite makes, and it is the one worth having on every push.
"""

from __future__ import annotations

import pytest
from evals.subjects import SUBJECTS

#: Subjects whose scoring depends on prompt wording. A subject with no contract
#: is listed here as `None` with a reason, so "no contract" is a stated
#: position rather than an omission nobody noticed.
_NO_CONTRACT = {
    "health": "deterministic walking skeleton — no model, no prompt",
    "groundedness": "scores a frozen corpus with a deterministic checker",
    "status-narrative-fallback": "rule-written narrative — no model, no prompt",
    "tool-selection": (
        "its prompt is the nine MCP tool *descriptions*, and RC1-249 already "
        "verified by trying it that degrading one flips its cases — the "
        "confusion matrix is the contract there"
    ),
}


def _with_contracts():
    return [(name, mod) for name, mod in SUBJECTS.items() if hasattr(mod, "PROMPT_CONTRACT")]


def test_every_subject_either_declares_a_contract_or_says_why_not():
    """No subject may quietly have neither."""
    undeclared = [
        name
        for name, mod in SUBJECTS.items()
        if not hasattr(mod, "PROMPT_CONTRACT") and name not in _NO_CONTRACT
    ]
    assert not undeclared, (
        f"{', '.join(undeclared)} declare no PROMPT_CONTRACT and give no reason. "
        "Add one, or add an entry to _NO_CONTRACT saying why the subject's "
        "scoring does not depend on prompt wording."
    )


@pytest.mark.parametrize("name,module", _with_contracts(), ids=lambda v: getattr(v, "__name__", v))
def test_the_prompt_still_contains_every_clause_its_checks_depend_on(name, module):
    prompt = _prompt_for(module)
    missing = [
        f"{clause!r} (needed by {why})"
        for clause, why in module.PROMPT_CONTRACT
        if clause not in prompt
    ]
    assert not missing, (
        f"{name}'s prompt no longer contains: {'; '.join(missing)}. Either restore "
        "the clause, or update PROMPT_CONTRACT — but removing a clause without "
        "updating the characteristic that relies on it means the eval silently "
        "starts measuring something else."
    )


def _prompt_for(module):
    """The system prompt behind a subject, via the agent it drives."""
    from agents import dependency, raid, status, work_breakdown

    return {
        "work-breakdown": work_breakdown.SYSTEM_PROMPT,
        "dependency": dependency.SYSTEM_PROMPT,
        "raid": raid.SYSTEM_PROMPT,
        "status-narrative": status.SYSTEM_PROMPT,
    }[module.NAME]


def test_a_contract_clause_that_is_not_in_the_prompt_is_caught():
    """The test above must be able to fail."""

    class _Fake:
        NAME = "work-breakdown"
        PROMPT_CONTRACT = (("a clause no prompt contains", "a characteristic"),)

    with pytest.raises(AssertionError, match="no longer contains"):
        test_the_prompt_still_contains_every_clause_its_checks_depend_on("fake", _Fake)
