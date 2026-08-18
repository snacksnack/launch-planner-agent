# Spec Quality Gate demo (RC1-293). Render from the repo root:
#     vhs docs/demo/spec-gate/demo.tape
# Commands are real; outputs are unedited captures of real runs, replayed via
# bin/uv so the GIF is not a minute of model latency per command. See README.md.

Output docs/spec-gate-demo.gif

Set FontSize 15
Set Width 1200
Set Height 760
Set Padding 16
Set TypingSpeed 35ms
Set Shell bash

Hide
Type "export PATH=docs/demo/spec-gate/bin:$PATH; clear"
Enter
Show

Type "# The Spec Quality Gate — review a PRD before anyone plans against it"
Enter
Sleep 2s

Type "uv run plan spec review fixtures/spec-gate/vague-spec.md"
Enter
Sleep 7s
Type "# 12 findings, readiness 0.0 — every one anchored to a verbatim quote,"
Enter
Type "# ending with the questions a reviewer would actually send back."
Enter
Sleep 4s

Type "clear"
Enter
Type "# The same rubric on a well-formed spec:"
Enter
Sleep 1s
Type "uv run plan spec review fixtures/spec-gate/good-spec.md"
Enter
Sleep 6s
Type "# Readiness 0.89, three nit-level findings. It can tell the difference."
Enter
Sleep 3s

Type "clear"
Enter
Type "# Gate the spec, then plan it — one command, straight into the planner:"
Enter
Sleep 1s
Type "uv run plan spec gate fixtures/jira-cloud-migration/"
Enter
Sleep 8s
Type "# Review written beside the plan; gate advisory; 9 epics, 22 tasks."
Enter
Sleep 4s
