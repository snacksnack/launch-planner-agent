"""The LLM judge — the thing being validated, not the thing being trusted.

It reads the same `rubric_text()` a human reads and returns the same 0/1/2 per
dimension. That symmetry is the whole design: a judge scoring against different
wording than the human would produce a number that looks like agreement and
isn't.

**Versioned, and the version travels with every score.** A judge prompt edit
invalidates the measured agreement exactly the way a rubric edit invalidates the
labels — so `JUDGE_VERSION` is the `scorer` on every label it writes, and
comparing labels from two judge versions is something the loader simply will not
do for you.

The judge is shown the facts and the output, and nothing else. Not the variant,
not the generator, not the human's score. Any of those would leak the answer.
"""

from __future__ import annotations

import json

from app.config import get_settings
from pydantic import BaseModel, ConfigDict, Field

from evals.rubric import JUDGED_KEYS, RUBRIC_VERSION, Score, rubric_text
from evals.seeds import Label, Seed

#: Bump on any change to `SYSTEM_PROMPT` or the scoring schema.
JUDGE_VERSION = "judge-v2"

SYSTEM_PROMPT = f"""\
You are scoring a weekly executive status update against a fixed rubric.

You will be given the FACTS the update was written from, and the UPDATE itself.
Score each dimension 0, 1, or 2 using the rubric below. Apply the rubric as
written — do not substitute your own standard for what a good status update is.

Score each dimension independently. An update can be perfectly grounded and
useless, or well written and wrong; those are different dimensions and a low
score on one is not a reason to lower another.

Whether the numbers, dates and names are *correct* is checked separately and is
NOT your job. Do not lower a score because a value looks wrong — judge only
whether the output claims things the facts do not contain.

RUBRIC

{rubric_text()}

For each dimension give a one-sentence reason citing something specific in the
update or the facts, then the score.
"""


class _Scored(BaseModel):
    """Schema-forced so the judge cannot answer in prose.

    `facts-correct` is absent on purpose: it is adjudicated by
    `evals.groundedness`, exactly and for free. Asking the judge for it would
    add cost, add a second opinion on a settled question, and invite it to
    lower `no-unsupported-claims` for a wrong number — which is the conflation
    v1 had (RC1-260).
    """

    model_config = ConfigDict(extra="forbid")

    no_unsupported_claims_reason: str
    no_unsupported_claims: int = Field(ge=0, le=2)
    completeness_reason: str
    completeness: int = Field(ge=0, le=2)
    actionability_reason: str
    actionability: int = Field(ge=0, le=2)
    tone_reason: str
    tone: int = Field(ge=0, le=2)

    def scores(self) -> dict[str, Score]:
        return {key: Score(getattr(self, _attr(key))) for key in JUDGED_KEYS}

    def reasons(self) -> str:
        return " | ".join(f"{k}: {getattr(self, f'{_attr(k)}_reason')}" for k in JUDGED_KEYS)


def preflight() -> None:
    if not get_settings().anthropic_api_key:
        raise RuntimeError(
            "LPA_ANTHROPIC_API_KEY is not set. Running the judge drives a real model."
        )


def _default_client():
    import anthropic

    preflight()
    return anthropic.Anthropic(api_key=get_settings().anthropic_api_key)


def build_user_prompt(seed: Seed) -> str:
    return (
        f"FACTS\n{json.dumps(seed.facts, indent=2, sort_keys=True)}\n\n"
        f"UPDATE\n{seed.rendered_output()}"
    )


#: Four one-sentence reasons plus scores, with headroom for thinking.
#:
#: Sized by failure, not by guess: at 1024 the first run truncated mid-string
#: ("EOF while parsing a string at line 1 column 438") and died on the sixth
#: seed. `max_tokens` caps thinking *and* output together, and adaptive thinking
#: is on by default on current models — so a budget that looks generous for the
#: visible answer is not.
_MAX_TOKENS = 4096


def _attr(key: str) -> str:
    """`no-unsupported-claims` -> `no_unsupported_claims`. Rubric keys are
    hyphenated for reading; Python attributes cannot be."""
    return key.replace("-", "_")


class JudgeRefused(Exception):
    """The judge produced no scoreable answer.

    Distinct from a low score: "the judge could not answer" and "the judge said
    this output is bad" are different findings, and collapsing them would let an
    outage read as a quality signal — the same distinction `CaseResult.error`
    keeps for subjects.
    """


def score(seed: Seed, client=None, model: str | None = None) -> Label:
    """Score one seed. The label's `scorer` is the judge version, not 'judge' —
    so two prompt versions can sit in the same file without being conflated."""
    client = client or _default_client()
    model = model or get_settings().anthropic_model
    response = client.messages.parse(
        model=model,
        max_tokens=_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt(seed)}],
        output_format=_Scored,
    )
    parsed = response.parsed_output
    if parsed is None:
        raise JudgeRefused(
            f"{seed.id}: no parsed output (stop_reason={getattr(response, 'stop_reason', '?')}). "
            f"A truncated answer usually means max_tokens ({_MAX_TOKENS}) is too small — "
            "it caps thinking and output together."
        )
    return Label(
        seed_id=seed.id,
        scorer=JUDGE_VERSION,
        rubric_version=RUBRIC_VERSION,
        scores=parsed.scores(),
        note=parsed.reasons(),
    )
