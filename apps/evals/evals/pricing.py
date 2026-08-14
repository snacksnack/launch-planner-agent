"""Model prices, so a run record can carry a cost rather than a token count.

Prices are per million tokens, in USD, as published for the first-party
Anthropic API. They are a **local snapshot**, not a live lookup: an eval run has
to be reproducible months later, and a cost that silently changes when a price
list changes would make two runs incomparable for the wrong reason. `as_of` is
part of the table for that reason — it says when a reader last checked.

An unknown model raises rather than defaulting to zero. A silent $0.00 would let
a subject look free forever after a model rename, which is exactly the finding
RC1-254's budgets exist to surface.
"""

from __future__ import annotations

from decimal import Decimal

#: When these prices were last verified against the published price list.
AS_OF = "2026-08-13"

_MILLION = Decimal("1000000")


class UnknownModelPrice(Exception):
    """No price on file for a model.

    Raised rather than assumed: a run record claiming zero cost for a model we
    simply do not have a price for is worse than a failed run, because it looks
    like a finding.
    """


class ModelPrice:
    """Input and output price per million tokens."""

    __slots__ = ("input_per_mtok", "output_per_mtok", "note")

    def __init__(self, input_per_mtok: str, output_per_mtok: str, note: str = "") -> None:
        self.input_per_mtok = Decimal(input_per_mtok)
        self.output_per_mtok = Decimal(output_per_mtok)
        self.note = note


#: Standard list prices. Promotional rates are deliberately NOT used — a run
#: costed at an introductory rate would understate the steady-state cost that
#: RC1-254's budgets are supposed to inform.
PRICES: dict[str, ModelPrice] = {
    "claude-opus-5": ModelPrice("5.00", "25.00"),
    "claude-sonnet-5": ModelPrice(
        "3.00", "15.00", note="introductory 2.00/10.00 through 2026-08-31; standard used here"
    ),
    "claude-haiku-4-5": ModelPrice("1.00", "5.00"),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Cost of one call, exact to the token."""
    try:
        price = PRICES[model]
    except KeyError as exc:
        known = ", ".join(sorted(PRICES)) or "(none)"
        raise UnknownModelPrice(
            f"no price on file for {model!r} (known: {known}); add it to evals.pricing "
            f"rather than letting the run record claim zero cost"
        ) from exc
    return (
        Decimal(input_tokens) * price.input_per_mtok
        + Decimal(output_tokens) * price.output_per_mtok
    ) / _MILLION
