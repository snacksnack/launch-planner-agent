# PRD: Quarterly Dependency Bump — Internal Reporting Service

**Owner:** Dana Whitfield (Eng) · **Status:** Approved

## Summary

Routine quarterly maintenance on the internal reporting service. We upgrade the
pinned Python dependencies to their current patch releases, run the existing test
suite, and deploy. This is the eleventh time we have run this exact procedure and
the runbook has not changed since the third.

## Scope

Bump the pinned versions in `requirements.txt` to the latest patch release within
the same minor version. No major or minor upgrades, so no API changes are
expected. Run the full test suite, which has been green on every one of the last
forty nightly runs and covers the reporting paths end to end.

## Rollout

Deploy to staging, let it soak for one working day, then deploy to production
during the regular Thursday afternoon window. The service is internal-only, used
by roughly a dozen people on the finance team, and has no external SLA.

Rollback is a one-command revert to the previous image, which has been exercised
twice and takes under two minutes. The previous image is retained for thirty days.

## Non-goals

No feature work. No schema changes. No infrastructure changes. No new
dependencies — this is a version bump on existing ones only.

## Timeline

There is no deadline. The work is expected to take one engineer about two days
and is not blocking anything else.
