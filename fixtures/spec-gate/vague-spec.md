# Unified SSO Migration — PRD (Project Gatehouse)

We are consolidating authentication for all internal and customer-facing
applications onto a single identity provider. This document describes the
migration.

## Background

Today, eleven applications maintain their own username/password stores. Password
reset volume is the single largest support ticket category, and security has
flagged credential reuse across systems in two consecutive audits.

## Goals

Retire the legacy username/password stack and move every application to single
sign-on through the new identity provider. The login flow must be fast and the
experience should feel seamless to end users.

## Requirements

Token refresh must complete in under a second. The system must support a large
number of concurrent sessions during peak sign-in. The new IdP will handle all
authentication for internal and customer-facing apps. Legacy login remains
available for 90 days after cutover so stragglers can migrate. Documentation
will be improved.

## Rollout

All users will be cut over in a single weekend migration. The legacy auth stack
will be decommissioned immediately after the cutover weekend. Once the Okta
tenant is provisioned by IT, app teams can begin wiring their integrations.
Mobile clients will pick up the new flow automatically.

## Timeline

Departments will migrate one at a time through Q4. Success metrics: TBD.

## Ownership and communications

The platform team will own the migration runbook. Training materials will be
produced before launch. Someone from support will draft the customer comms.
