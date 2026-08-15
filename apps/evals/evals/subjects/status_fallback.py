"""The deterministic twin of `status-narrative`.

Same cases, same characteristics, rule-written producer. Registered as its own
subject rather than a flag because RC1-252 requires the two scored as separate
*subject versions* — and the run record is where that becomes real: this one
records `model: None` and a cost of zero.

A module rather than a class, because a subject is four names (`NAME`, `CASES`,
`version()`, `run()`) and nothing here needs more than that.
"""

from __future__ import annotations

from evals.subjects.status_narrative import (
    CASES as CASES,
)
from evals.subjects.status_narrative import (
    FALLBACK_NAME as NAME,
)
from evals.subjects.status_narrative import (
    fallback_version as version,
)
from evals.subjects.status_narrative import (
    run_fallback as run,
)

__all__ = ["CASES", "NAME", "run", "version"]
