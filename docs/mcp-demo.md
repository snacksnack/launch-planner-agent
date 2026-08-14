# MCP demo script

A five-minute conversational walkthrough of the delivery-intelligence platform,
run from a laptop in any stdio MCP client. No screen-share of a Gantt chart, no
setup beyond pasting a config block.

This doubles as the **discoverability check** for RC1-243: the questions are
phrased the way a person would ask them, and the point is that the model picks
the right tool with no hints. A wrong pick means a tool description is wrong —
fix the description, not the question.

## Before recording

1. **Point at a real plan store.** The default is the flagship golden, which
   works with no credentials. For a richer Baseline/Status segment, commit a
   baseline first:

   ```bash
   GOLDEN=fixtures/jira-cloud-migration/golden/expected-plan.json
   uv run plan baseline $GOLDEN --by "Priya Nair" --note "initial plan"
   ```

2. **Decide whether drift is in the recording.** `LPA_DRIFT_BASE_URL` must point
   at a deployed `tpm-automation-platform` with the RC1-244 read endpoints. Left
   unset, `drift.check` reports unavailable — which is honest, but skip questions
   4 and 5 rather than showing a failure.

3. **Client config** (Claude Desktop, `claude_desktop_config.json`):

   ```json
   {
     "mcpServers": {
       "launch-planner": {
         "command": "uv",
         "args": ["run", "--directory", "/absolute/path/to/launch-planner-agent",
                  "launch-planner-mcp"],
         "env": { "LPA_DRIFT_BASE_URL": "https://your-drift-service" }
       }
     }
   }
   ```

4. **Sanity check before you hit record** — ask *"is the planner healthy?"*. It
   should route to `platform.health` and report the store readable and drift
   reachable. If drift says unavailable, fix that before recording, not during.

## The script

Ask these verbatim. Each names the tool it should route to and what to look for.

### 1. "What plans do you have?"

→ `plan.list`

Establishes that the model can discover the surface cold. Note it echoes a
`canonical_ref` it can use next — no ID formats to guess.

### 2. "When does the Jira Cloud migration launch, and what's driving that date?"

→ `plan.get`, then `plan.critical_path`

The launch date is **2026-10-12**, 51 working days. The interesting beat is the
second tool: **two** converging critical chains, 11 distinct critical tasks. If
the model reports a single path, the description needs work.

Worth saying aloud: the chains are ordered along real dependency edges, and the
owners come back with them — *"the critical work runs through Sven Lindqvist
three times"* is the kind of thing a Gantt makes you hunt for.

### 3. "What if the legal sign-off slips a month?"

→ `plan.simulate`

The headline moment. Legal review has **6 working days of float**, so a 30-day
slip moves the launch by **24** — Oct 12 → Nov 13 — and the critical path
reroutes through legal review while three other tasks drop off it.

Then immediately: **"and if it only slips two days?"**

Same tool, opposite finding: **absorbed by float**, launch date unmoved. This is
the beat that shows the tool is doing real critical-path arithmetic rather than
adding days to a date, and that it distinguishes "absorbed" from "nothing
happened."

### 4. "How confident are we in that October date?"

→ `plan.forecast`

**P80 is Oct 23**, P50 Oct 19, P90 Oct 27 — and the plan's own Oct 12 was
achieved in **19% of 1,000 runs**. The model should lead with the band, not the
plan date.

Say the seed out loud: the run is reproducible, which is what makes the number
defensible in a meeting.

### 5. "Anything drifting right now?" *(needs the drift service)*

→ `drift.check`

Findings by severity from the last scheduled run. The model should say **when**
that run was — this is stored data, not a live scan.

### 6. "Why did that first one fire?" *(needs the drift service)*

→ `drift.explain`

Evidence, not prose: the two tickets, the dates, and the change that triggered
the rule. Enough to agree or disagree without opening Jira.

### 7. "Draft me the weekly status update."

→ `status.draft`

Health decided by rule with its reasons, facts traceable to diff entries, and
ready-to-paste Markdown. Closing line for the recording: **it drafts and never
sends** — no email, no Slack, by construction.

## The read-only beat

Worth one sentence somewhere near the end, because it is the thing that makes
the rest trustworthy:

> Every one of these is read-only. Committing a plan, generating Jira tickets,
> and running a fresh drift scan are all human-gated actions that this surface
> cannot reach — enforced by an import contract, a tool allowlist, and a
> call-level sweep, each verified by deliberately breaking it.

## Recording the discoverability result

**This is no longer a manual pass** (RC1-249). Every question above runs as an
eval case, alongside paraphrases and indirect phrasings the script does not
cover:

```bash
uv run evals run tool-selection        # needs LPA_ANTHROPIC_API_KEY; spends tokens
```

It reports routing accuracy and a **confusion matrix** — which wrong tool was
chosen when the intended one wasn't, which is the part that tells you *which
description to fix*. A wrong pick still means a tool description is wrong; fix
the description, not the question.

The earlier argument for keeping this manual — that it needs a real model and
would otherwise put a nondeterministic, credential-requiring test in a suite
whose defining property is that it runs credential-free — still holds, and is
why the eval is a **separate command** rather than part of `uv run pytest`. The
suite stays credential-free; the eval is billed and run deliberately. See
ADR-0031.

This script remains the **recording** script: it is paced for a five-minute
walkthrough and says what to narrate. The eval covers a superset of its
questions and none of its narration.

The pair to watch is **2b vs 4**: "what's driving the date" and "how confident
are we" are easy to conflate, and both tools talk about critical paths.
`plan.critical_path` is one deterministic pass; `plan.forecast` is the
criticality index across sampled runs. Their descriptions name each other
specifically to keep them apart — and the eval carries that pair as an explicit
near-miss case in both directions, scoring "chose the right tool" separately
from "avoided the confusable sibling", because those are two different
description bugs with two different fixes.
