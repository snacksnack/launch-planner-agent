# Audit Log Export — PRD

Admins need to hand auditors a record of who did what in the Admin Console.
This adds a self-serve export of audit events.

## Goal

An admin with the `audit-admin` role can export audit events for a chosen date
range and hand the file to an external auditor without filing a support ticket.

## Non-goals

- No streaming or webhook delivery of audit events (tracked separately).
- No changes to what is audited; this exports the existing event stream.
- No customer-facing surface; Admin Console only.

## Scope and requirements

- REQ-1 — An `audit-admin` can request a CSV export of audit events for any
  date range up to 92 days, from the Admin Console's Audit page.
- REQ-2 — Export generation is asynchronous: the request returns immediately
  and the admin is emailed a download link when the file is ready.
- REQ-3 — The export contains, per event: timestamp (UTC, ISO 8601), actor id,
  actor email at event time, action code, target resource id, and source IP.
- REQ-4 — Download links require an authenticated `audit-admin` session; the
  act of downloading an export is itself written to the audit log.
- REQ-5 — Exports expire and are deleted 7 days after generation.
- REQ-6 — The Audit page lists the requester, date range, and expiry of every
  export generated in the last 30 days.

## Acceptance criteria

- Requesting an export for the last 30 days on the staging dataset (about 400k
  events) delivers the email link within 10 minutes.
- The CSV for a seeded fixture range matches the seeded events row-for-row.
- A user without `audit-admin` receives a 403 from the export endpoint, and the
  attempt appears in the audit log.
- An expired link returns 410 and the file is confirmed absent from storage.

## Non-functional requirements

- Latency: p95 export generation under 30 seconds per 1M events, measured at
  the worker, on the production instance class.
- Availability: export requests ride the existing Admin Console SLO (99.9%
  monthly); a failed generation retries three times, then emails the requester.
- Security: files are encrypted at rest, keyed per export; links are
  single-tenant and signed, valid only for the requesting org.
- Retention: generated files live 7 days (REQ-5); request metadata lives 30
  days (REQ-6); neither outlives the org on deletion.
- Cost: worker plus storage for projected volume stays under $40/month at
  current event rates; alert at $30.

## Rollback

The feature ships behind the `audit_export` flag, default off, enabled
per-org. Disabling the flag hides the UI and rejects new requests while
existing files age out on their own; no data migration is involved in either
direction.

## Owners

- Feature and rollout: Maya Lindqvist (Admin Console PM)
- Backend and worker: Dev Okafor
- Console UI: Priya Natarajan
- Security review sign-off: Jonas Beck (AppSec)

## Timeline

Behind-flag build complete by 2026-10-02; security review the week of
2026-10-05; first org enabled 2026-10-12; general availability 2026-10-26.

## Open questions

- Does the auditor need JSON in addition to CSV? Default answer is no until an
  auditor asks; the worker interface keeps the formatter pluggable either way.
