# Judging

> How the LLM judge is validated, what that validation currently establishes,
> and — more importantly — what it does not. Companion to
> [decisions.md](decisions.md) (ADR-0033) and the running
> [forecasting](forecasting.md) note, which states its assumptions the same way.

An uncalibrated LLM judge is a second opinion with extra steps. It produces
numbers that look like measurement and aren't. So before any rubric score is
allowed to mean anything — and long before it is allowed to fail a build — the
judge has to be checked against something outside itself.

This document is the honest state of that check.

## The short version

Rubric `status-narrative-v2`, judge `judge-v2`.

| Dimension | Scored by | κ (weighted) | n | 95% CI | Gates |
| --- | --- | --- | --- | --- | --- |
| `facts-correct` | **deterministic** | n/a — exact | 36 | — | **yes** |
| `no-unsupported-claims` | judge | **0.86** | 24 | +0.63 to +1.00 | **yes** |
| `completeness` | judge | not established | 0 | — | no |
| `actionability` | judge | not established | 0 | — | no |
| `tone` | judge | not established | 0 | — | no |

**Two dimensions gate, and they are the two failure modes that matter for a
narrative**: the numbers are wrong, and it made something up. One is exact, one
is judged, and each is checked by the instrument suited to it.

`facts-correct` needs no calibration because it is not a judgement —
`evals.groundedness` checks every ticket key, date and day-count against the
input, with zero false positives on the committed corpus (RC1-251).

`no-unsupported-claims` clears the floor with **98% of its bootstrap interval
above it**. The other three remain advisory: visible in every run, unable to
fail a build.

## The rubric, and why it was split

Five dimensions, scored 0 (fails) / 1 (partial) / 2 (meets), defined in
`apps/evals/evals/rubric.py` and versioned as `status-narrative-v2`.

**v1 had a single `groundedness` dimension, and it was asking two questions.**
Its own wording collided: `2 (MEETS)` said *"appears in the facts, **or follows
from them**"* while `1 (PARTIAL)` said *"includes a **soft claim the facts do not
support**"*. An output whose numbers are all correct but which adds *"reflecting
improved schedule buffer"* satisfies one clause and violates the other.

Three of the five human-vs-judge disagreements in RC1-250 sat exactly there. The
human scored 2 (the numbers are right); the judge scored 1 (the gloss is
unsupported). **Both readings were correct — of different questions.** No amount
of extra labelling resolves that, which is why doubling n moved the estimate
without narrowing the interval.

v2 splits them:

| | Question | Scored by |
| --- | --- | --- |
| `facts-correct` | Do the values match the input? | `evals.groundedness` — exact, free |
| `no-unsupported-claims` | Does it assert anything beyond them? | the judge |

and states the boundary the collision hid: *a causal, evaluative or attributive
phrase counts as an unsupported claim **even when every number is correct***.

The result, on the same three seeds, re-scored:

| Seed | `facts-correct` | `no-unsupported-claims` |
| --- | --- | --- |
| `08-agent` | 2 | 1 — *"'driven by' … adds causal framing not stated"* |
| `10-agent` | 2 | 1 — *"'reflecting improved schedule buffer'"* |
| `11-agent` | 2 | 1 — *"'due to delays' … 'less buffer than before'"* |

The disagreement dissolved rather than being argued away: both scorers had been
right, and the rubric now has a place for each answer. Agreement went from
**κ 0.66 (34% of the interval below the floor)** to **κ 0.86 (2% below)**. A human and
the judge read the *same text* — `rubric_text()` is the single source, because a
judge scoring against different wording than the human is not a calibration but
two unrelated measurements.

Three points rather than five: a five-point scale invites a middle nobody can
define and adjacent disagreements that mean nothing. Three forces the judgement
that actually matters downstream — meets the bar, partway, fails.

The scale is **ordinal**, so agreement is measured with linear-weighted kappa as
well as plain kappa. A 0-vs-2 disagreement is two scorers reading an output
completely differently; a 1-vs-2 is a borderline call. Plain kappa cannot tell
them apart.

## Why agreement, and not accuracy

Raw agreement flatters. If 80% of outputs are fine, a judge that says "fine" to
everything agrees with a human 80% of the time while having measured nothing.
Cohen's kappa corrects for that by asking how much better than chance the
agreement is, given how often each scorer uses each score.

This is not hypothetical here. In the first pass, **completeness had 75% raw
agreement and a kappa of −0.12** — worse than chance, wearing a number that
looks like a pass.

## The gating floor, argued rather than assumed

A dimension may fail a build only at **weighted kappa ≥ 0.6**.

Landis & Koch's conventional bands put 0.61–0.80 at "substantial" and 0.41–0.60
at "moderate". 0.6 is the bottom of "substantial" — the point at which two
scorers can be said to be applying the same standard rather than merely
correlating. It is a convention, not a law, and it is deliberately set where a
dimension has to earn gating rather than default into it.

Anything below the floor is reported as **advisory**: visible in every run,
never able to fail a build. `CharacteristicResult.advisory` carries this through
the run record, and the CLI marks advisory misses with `~` and the word
`[advisory]` so a reader can never mistake one for the other.

## What went wrong with the human labels

The seed set is 36 outputs. Under v1, human labelling took four attempts.

| Pass | Scope | Result |
| --- | --- | --- |
| `human` | all 36, all 4 dimensions | 144 judgements in one sitting; labeller reported afterwards that it was not careful |
| `human-careful` | 15, groundedness only | scored the construct **inverted** |
| `human-v2` | 12, groundedness only, corrected prompt | scored the construct **inverted** again |
| `human-v3` | 12 → 24, groundedness only | **usable** — κ 0.66 at n=24, construct correctly oriented |

The fourth pass is the one that worked, and the difference was scope: twelve
items, one dimension, deliberately. The first pass asked for twelve times that
much judgement in a single sitting, which is a design choice masquerading as a
labeller problem.

Two of the three failures were failures of the instrument, not the labeller:

* **The seed id leaked the answer.** Ids are `status-narrative-10-degraded`, and
  the labelling header printed them. A labeller who knows an output came from the
  deliberately-degraded prompt scores it low for that reason — calibrating the
  judge against a hint the judge never gets. Headers now show an opaque
  reference. Found by rendering a real seed and reading it; the test that was
  supposed to catch it had been written *around* it, stripping the id before
  asserting.
* **The scale could be entered backwards.** Options were listed descending
  (2, 1, 0) under a prompt that read "score 0/1/2". The mapping is now spelled
  out in the prompt itself and the options are listed ascending.
* **144 judgements is too many.** The rubric changed on every keystroke.
  Labelling is now dimension-major (`--dimension groundedness`), so one question
  is held across the whole set — standard annotation practice, for this reason.

The third failure is simply that careful labelling did not happen. That is a
real constraint, not a moral failing, and the method below is what makes progress
possible without it.

## Construct validity: the check that needs no human

The seed set contains its own ground truth.

* `fallback` outputs come from `planner_core.fallback_narrative`, a **template
  over the facts** — it restates them and was assumed to be fully grounded by
  construction. That assumption turned out to be too strong: both scorers gave
  some of its outputs a 1 (human mean 1.43, judge 1.71). It is *cleaner* than the
  degraded variant, which is all the check needs, but it is not a guaranteed 2.
* `degraded` outputs come from a prompt written to invite exactly what the
  rubric penalises — reassurance, diplomacy, invented causes, team sentiment.

Whether a scorer ranks the first above the second is a fact about that scorer,
and nobody has to label anything for it to be true. The metric is the fraction
of clean-vs-planted pairs ranked correctly, ties counting half — a rank
statistic, so a judge being uniformly harsh or generous does not register as
signal.

    uv run evals construct

**Only where the degradation actually attacks the dimension.** The degraded
prompt manufactures unsupported claims (groundedness) and the wrong register
(tone). It does not attack completeness — an expansive narrative covers *more*
of the facts — and its effect on actionability is ambiguous. Run unscoped, the
check reported completeness at 33%, below chance, which reads as a judge failure
and is really a statement about the prompt. Untargeted dimensions are now
reported as out of scope rather than scored.

### The result

Under `status-narrative-v2`:

| Scorer | `no-unsupported-claims` | `tone` |
| --- | --- | --- |
| `judge-v2` | **89%** | **99%** |
| `human-v4` (the usable pass) | **83%** | not scored |

And under v1, which is why the tripwire exists at all:

| Scorer | groundedness | tone |
| --- | --- | --- |
| `judge-v1` | 71% | 100% |
| `human-v3` | 71% | not scored |
| `human` (fast) | 53% — chance | 50% — chance |
| `human-careful` | **0% — inverted** | not scored |
| `human-v2` | **0% — inverted** | not scored |

The judge separates outputs we degraded on purpose, and so does the one careful
human pass — at exactly the same rate. The three unusable passes do not, which
is what makes this a useful tripwire: **run it on a label set before spending
effort calibrating against it.** All three could have been discarded in seconds.

## The `no-unsupported-claims` calibration, in full

Twenty-four seeds, scored by a human and by `judge-v2` independently, under
`status-narrative-v2`.

    no-unsupported-claims   24   92%   κ 0.86   CI +0.63 to +1.00   gates
      2% of the interval is below the 0.6 floor

Twenty-two of twenty-four agree exactly. `judge-v2` used all three scores across
the corpus (12 / 13 / 11), so there is real variance to measure — v1's
groundedness was lopsided at 1 / 12 / 23, which is part of why its kappa was both
lower and less stable.

### Why n was doubled, and what that showed

The first pass was twelve seeds and returned **κ 0.82**, comfortably above the
floor. A bootstrap put its 95% interval at 0.43–1.00 with an 11.6% chance of
being below 0.6 — imprecise enough to be worth a second twelve.

Doubling n moved the estimate **down to 0.66** and left the interval essentially
as wide (0.33–0.93). That is the informative part. Sampling noise halves with
4× the data; an interval that shifts without narrowing points at genuine
variance in how the rubric is applied to the middle of the range. **More
labelling is unlikely to resolve this** — the rubric is.

The first twelve were a favourable draw. Reporting 0.82 would have been the easy
version and would have gated a dimension that has not earned it.

Mean score by variant, which neither scorer could see:

| | `fallback` | `agent` | `degraded` |
| --- | --- | --- | --- |
| human | 2.00 | 2.00 | 1.00 |
| judge | 2.00 | 1.75 | 1.00 |

Both independently put every degraded output at 1 — the rubric's "broadly
faithful, but includes at least one soft claim the facts do not support". That
is the two scorers agreeing on the *reason*, not only the number.

The single disagreement is a defensible borderline. On an `agent` output the
human scored 2 and the judge scored 1, saying:

> *"adds an unsupported causal gloss ('reflecting improved schedule buffer')
> that isn't stated in the facts, though all numbers/dates match"*

Which is a fair reading of the rubric. A judge whose one disagreement is
articulate is in a different position from one whose disagreements are noise.

## What this establishes, and what it does not

**Establishes:** on `no-unsupported-claims`, `judge-v2` and a human applying the
same rubric agree at κ 0.86 over 24 items, with 98% of the bootstrap interval
above the floor. Combined with `facts-correct` — which is exact rather than
judged — the two failure modes that matter for a narrative are both covered by an
instrument suited to them.

**Does not establish** — and these are the reasons the other three dimensions
stay advisory:

* **Nothing about completeness, actionability, or tone.** No careful human
  labels exist for them. Tone passes the construct check, which earns it nothing
  under the floor.
* **Nothing about the bottom of the scale.** Neither scorer used **0** on any of
  the twelve seeds. The calibration covers the *meets-vs-partial* distinction and
  says nothing about *fails* — which is the score that would most obviously gate
  a build, since an invented ticket key is a 0. RC1-251's deterministic
  pre-checks are the right place to catch those anyway, and they need no judge.
* **n is twelve.** Enough to clear the floor, not enough to be precise about
  where above it the true value sits.
* **One subject, one model, one rubric version.** Nothing here says the judge
  behaves the same on the drift digest (RC1-252).

## Adding a dimension later

Same shape as the passes that worked — small, single-dimension, tripwire first:

```bash
uv run evals label --dimension actionability --scorer human-v5 --limit 24
uv run evals construct --scorer human-v5    # seconds: is this pass usable?
uv run evals calibrate --scorer human-v5    # kappa and interval
```

Step two is the part worth keeping regardless of what it returns. Three v1
passes were wasted before it existed; each could have been discarded in seconds
rather than after a full calibration. **Run it on any label set before trusting
one.**

Two things learned the hard way and worth repeating:

* **Scope, not volume.** 36 seeds × 4 dimensions is 144 judgements and produced
  labels the labeller told us to discard. 24 seeds × 1 dimension produced two
  usable calibrations in a row.
* **The interval decides, not the point estimate.** `calibrate` now prints a
  bootstrap CI and withholds gating when more than 20% of it sits below the
  floor. RC1-250 measured 0.66 — above the floor — and the honest call was still
  not to gate.

## Limits worth stating plainly

* **n is small.** 36 seeds, one subject, one model. A kappa over twelve items is
  not a kappa over four hundred, and `n` is printed next to every figure so the
  two cannot be confused.
* **One subject.** All seeds are status narratives. Nothing here says the judge
  behaves the same on the drift digest (RC1-252).
* **The rubric is unproven.** Rubric quality and judge quality are entangled: a
  low agreement can mean either. Intra-rater agreement — does a human agree with
  themselves? — would separate them, and has not been measured, because no pass
  was careful enough to serve as either side of that comparison.
* **The judge is one model at one version.** `judge-v2` and
  `status-narrative-v2` are recorded on every label. Comparing labels across
  versions is refused rather than averaged, because a score of 1 under different
  wording is not the same measurement.
* **Prompt caching and cost.** The judge is billed and runs outside
  `uv run pytest`, which stays credential-free (ADR-0031).
