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

| Dimension | Human agreement (weighted κ) | n | 95% CI | May gate a build |
| --- | --- | --- | --- | --- |
| groundedness | 0.66 | 24 | 0.33 – 0.93 | **no** |
| completeness | not established | 0 | — | no |
| actionability | not established | 0 | — | no |
| tone | not established | 0 | — | no |

**No dimension gates.** All four are advisory: they appear in every run and
cannot fail a build, which is the state RC1-255 must respect.

Groundedness is the closest, and the decision not to gate it is deliberate
rather than a missing measurement. Its point estimate clears the 0.6 floor, but
**34% of the bootstrap distribution sits below it**. The epic's own rule about
gates applies: *a flaky gate gets disabled within a week, which is worse than no
gate*. A gate with one-in-three odds of not having earned its authority is that
gate.

## The rubric

Four dimensions, scored 0 (fails) / 1 (partial) / 2 (meets), defined in
`apps/evals/evals/rubric.py` and versioned as `status-narrative-v1`. A human and
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

The seed set is 36 outputs, and human labelling took four attempts.

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

| Scorer | groundedness | tone |
| --- | --- | --- |
| `judge-v1` | **71%** | **100%** |
| `human-v3` (the usable pass) | **71%** | not scored |
| `human` (fast) | 53% — chance | 50% — chance |
| `human-careful` | 0% — inverted | not scored |
| `human-v2` | 0% — inverted | not scored |

The judge separates outputs we degraded on purpose, and so does the one careful
human pass — at exactly the same rate. The three unusable passes do not, which
is what makes this a useful tripwire: **run it on a label set before spending
effort calibrating against it.** All three could have been discarded in seconds.

## The groundedness calibration, in full

Twenty-four seeds, scored by a human and by `judge-v1` independently.

| | 0 (fails) | 1 (partial) | 2 (meets) |
| --- | --- | --- | --- |
| human | 2 | 9 | 13 |
| judge | 1 | 12 | 11 |

Nineteen of twenty-four agree exactly (83%). Both scorers used all three scores,
so this is not the degenerate case where kappa is undefined.

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

**Establishes:** on groundedness, `judge-v1` and a human applying the same rubric
agree substantially more than chance — κ 0.66 over 24 items, 83% exact agreement,
both using the full scale. That is a real signal and far from the noise an
uncalibrated judge would produce. It also detects planted degradation on
groundedness and tone without needing any labels at all.

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

## Closing the remaining gap

Same shape as the pass that worked — small, single-dimension, tripwire first:

```bash
uv run evals label --dimension completeness --scorer human-v4 --limit 12
uv run evals construct --scorer human-v4    # seconds: is this pass usable?
uv run evals calibrate --scorer human-v4    # the kappa
```

Step two is the part worth keeping regardless of what it returns. Three passes
were wasted before it existed; each could have been discarded in seconds rather
than after a full calibration. **Run it on any label set before trusting one.**

To extend the *bottom* of the scale, the cheapest route is a fourth generator
variant that plants outright fabrications — an invented ticket key or a date not
in the facts — so 0 has seeds to attach to. The `degraded` prompt produces soft
unsupported claims, which is a 1, and that is why no 0 appeared.

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
* **The judge is one model at one version.** `judge-v1` and
  `status-narrative-v1` are recorded on every label. Comparing labels across
  versions is refused rather than averaged, because a score of 1 under different
  wording is not the same measurement.
* **Prompt caching and cost.** The judge is billed and runs outside
  `uv run pytest`, which stays credential-free (ADR-0031).
