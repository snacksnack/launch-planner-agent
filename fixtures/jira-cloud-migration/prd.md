# PRD: On-Prem Jira → Jira Cloud Migration ("Project Skyline")

**Owner:** Priya Nair (TPM) · **Status:** Draft v0.3 · **Last updated:** 2026-07-20

> Working doc. Some of this is firm, some is still being argued about in the
> #skyline channel. Dates below are targets unless I've said "hard".

## Background

We are still running Jira Data Center on-prem. It has become the system of record
for 20 projects across 15 teams — roughly 450 active users, plus a long tail of
read-only stakeholders. The install is old, the plugins are a mess, and the box it
runs on is out of warranty. Finance has also made it clear they don't want to renew
the on-prem license again: **our current Data Center license expires 2027-04-30, and
that date is hard** — everything has to be off on-prem and the old instance
decommissioned before it lapses. There is no extension. That's the wall we're
planning backwards from.

The goal is to move everything to Jira Cloud (Premium) with as little disruption to
the 15 teams as we can manage. We are treating **2027-03-15 as the target go-live**
for the full production cutover, which leaves a few weeks of buffer before the
license wall for decommissioning and cleanup.

## Why now / what success looks like

- All 20 projects, their workflows, custom fields, and history live in Cloud.
- All 450 users migrated with the right permissions and single sign-on working.
- No data loss: ticket counts, comments, and attachments reconcile against on-prem.
- On-prem instance shut down and decommissioned before the license expires.

## Scope & known problems

**Discovery first.** Nobody actually has a current inventory. Before we can plan
waves we need to inventory all 20 projects, their workflows, and the custom fields —
there are a lot of bespoke fields that different teams rely on and some of them
overlap or conflict. Expect surprises here.

**Plugins are the scary part.** We lean on a pile of marketplace apps. An early look
suggests at least three of our critical plugins have no direct Cloud equivalent, so
we'll need to audit every marketplace plugin for Cloud compatibility and then either
find a replacement, rebuild the capability with automation/scripts, or convince the
team to live without it. We should not migrate a single real user until the plugin
story is settled — **a plugin compatibility audit sign-off is a prerequisite for
migrating users.** The QA lead owns that sign-off.

**Integrations.** CI/CD pipelines and the Slack notifications both talk to the
on-prem Jira API. Those integrations will have to be rebuilt against the Cloud REST
API; they can't just be re-pointed.

**People & budget.** The in-house team can't absorb the bulk migration on top of
their day jobs. We want to bring on two migration contractors to run the wave
migrations — but **contractor spend has to be approved by Finance before anyone gets
onboarded**, and that approval is not yet secured. Grace in the PMO is chasing it.

## The parts Legal and Security care about

Several of these 20 projects contain client data under contract. **Legal has to sign
off before any client data moves to Cloud** — this is non-negotiable and applies to
every project that touches client records, not just the pilot. Helen in Legal is the
approver, and historically these reviews are slow, so start it early.

Separately, Security and SRE need to review the Cloud configuration — SSO, IP
allow-listing, audit logging — and **there will be no production cutover without a
security and SRE sign-off.** Treat that as a gate on the cutover itself, not a
nice-to-have.

## Sequencing and the freeze

Rough shape of the waves:

1. Stand up the Cloud org, SSO, and security config.
2. Pilot-migrate two low-risk internal projects to prove the runbook end to end.
3. Validate the pilot with those project owners, then bulk-migrate the remaining 18
   projects in waves.
4. Cut over to Cloud in production, hypercare, then decommission on-prem.

One big scheduling landmine: **the company-wide Q4 change freeze runs from
2026-11-15 to 2027-01-04, and no production migration activity is allowed during that
window.** Prep and non-production work can continue, but nothing that touches
production data or user access moves during the freeze. We would like the pilot done
*before* the freeze so we go into the new year with a validated runbook.

## Rough milestones

- **Pilot migration complete** (2 projects) — aiming for ~2026-11-10, i.e. just
  before the freeze.
- **Bulk migration complete** — end of February 2027.
- **Production go-live / cutover** — 2027-03-15 (target).
- **On-prem decommissioned** — by ~2027-04-15, ahead of the license wall.

## Explicitly out of scope (for now)

- Re-designing workflows "while we're in there." We lift-and-shift; workflow cleanup
  is a later project.
- Migrating Confluence. Separate effort, separate PRD.

## Open questions

- Do we need a formal data reconciliation step signed off by each project owner, or
  is a spot check enough? (Assuming formal for now — too risky otherwise.)
- Who owns hypercare after cutover — IT support or the migration team?
