# PRD: "Aurora" Mobile App v1.0 Public Launch

**Owner:** Jordan Access (PM) · **Status:** Draft v0.2

## Summary

Aurora is our new consumer mobile app. We're taking it from an internal build to a
public v1.0 launch on both iOS and Android. Marketing has already booked a launch
moment: **the public launch is locked to 2026-10-01 — the press embargo lifts and the
launch event runs that day, and that date is fixed.** Everything works backwards from
there.

## What we're shipping

The v1.0 feature set still has a couple of screens to finish, so first we need to
complete the v1.0 feature set and stabilize the build. In parallel, the backend has
to be scaled for launch load — current capacity is sized for the internal beta, not a
public launch.

## Quality gates

Before we put the app in front of any external users we have to clear privacy review:
Aurora collects some sensitive usage data, and **the privacy and legal review of our
data collection has to be signed off before the closed beta starts.**

We also need real load and performance testing against production-scale traffic before
we ship — no launching on untested capacity.

And the obvious one: it's a mobile app, so **App Store review approval is required
before the public launch** and Apple's review timing is not fully in our control, so
submit with buffer.

## Rough plan

1. Run a closed beta with ~500 users to shake out real-world bugs.
2. Triage beta feedback and fix the P1 issues.
3. Submit to the App Store, prep marketing, and launch on the date.

## Milestones

- **Closed beta live** — around 2026-08-15.
- **Public launch (GA)** — 2026-10-01.
