# Spec-gate demo GIF — how it was made

`../spec-gate-demo.gif` is rendered with [vhs](https://github.com/charmbracelet/vhs)
from `demo.tape`:

```bash
vhs docs/demo/spec-gate/demo.tape       # from the repo root
```

**The commands in the GIF are real, and the outputs are real** — captures of
actual runs (rubric v1, claude-sonnet-5, 2026-08-18) stored in `captures/`,
unedited except for one redaction: two ephemeral local output paths in the
gate capture are shortened to `/tmp/…/` (they were machine-specific noise, not
demo content). The one artifice is timing: `bin/uv` is a PATH shim that replays
each command's captured output instantly, because the live rubric takes about
a minute per document and a GIF of model latency demos nothing. Drop the shim
from PATH and the identical commands run live:

```bash
uv run plan spec review fixtures/spec-gate/vague-spec.md   # ~60s, ~$0.10
uv run plan spec review fixtures/spec-gate/good-spec.md
uv run plan spec gate fixtures/jira-cloud-migration/       # gate + breakdown
```

| Capture | What it is |
| --- | --- |
| `captures/review-vague.txt` | The planted-defect fixture: 12 findings, readiness 0.0, questions block. |
| `captures/review-good.txt` | The well-formed fixture: 3 nit-level findings, readiness 0.89. |
| `captures/gate-flagship.txt` | `spec gate` on the flagship migration PRD: 10 real findings (advisory), then the work breakdown it handed off to. |

Regenerating a capture after a rubric change is one live command redirected to
the file — and a materially different result is a finding, not a reason to
keep the old capture.
