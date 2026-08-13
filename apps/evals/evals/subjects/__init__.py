"""Subjects under test.

A subject is a module exposing three names: `NAME`, `version()`, and `run(case)`,
plus a `CASES` tuple. That is the whole contract, and it is duck-typed on
purpose — no `Subject` protocol, no ABC, no plugin discovery. There is exactly
one subject today; an interface generalised from one implementation is a guess,
and RC1-252 is where the second consumer makes the real seams visible. See
ADR-0030.

`SUBJECTS` is a dict rather than an entry-point registry for the same reason: a
CLI needs to turn `health` into a module, and a dict does that in one line
without anything to configure or discover.
"""

from __future__ import annotations

from evals.subjects import health

SUBJECTS = {
    health.NAME: health,
}

__all__ = ["SUBJECTS", "health"]
