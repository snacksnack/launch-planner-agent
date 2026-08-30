"""Text normalization shared by plan validation and the spec gate (RC1-286).

These began life in `planner_core.validation` and were promoted here when the
spec gate became their second caller. `validation.py` re-imports them, so every
existing import path (`planner_core.validation.normalize_for_quote_match`,
the package-root export) still works — there is exactly one implementation, and
the RC1-257 false-positive rules baked into `_DECORATION` apply to both callers.
"""

from __future__ import annotations

import re


def normalize_whitespace(text: str) -> str:
    """Collapse whitespace so quote/section matching ignores PRD line wrapping."""
    return " ".join(text.split())


#: Markdown emphasis and quote punctuation, removed before a quote is matched.
#:
#: RC1-257 found this by running the work-breakdown agent against the shipped
#: PRDs: three faithful quotes were reported as unverifiable because the PRD
#: wraps them in `**`, and the agent quotes what a reader *sees* rather than the
#: markup — which is the right reading of "copied verbatim from the PRD".
#:
#: Double quotes are stripped for a related reason: the agent transliterates a
#: closing `**` as `"`, so `...the public launch** and Apple's...` came back as
#: `...the public launch" and Apple's...`. RC1-326 added em/en dashes: the same
#: closing `**` also comes back as ` — `. Apostrophes and hyphens are
#: deliberately *not* stripped — they are part of words, and removing them
#: would let a quote match text it does not actually contain.
_DECORATION = re.compile(r"[*`\"“”—–]")


def normalize_for_quote_match(text: str) -> str:
    """Whitespace-, markup- and case-insensitive form used for provenance matching.

    Deliberately *not* used for anything but quote comparison. It is a matcher,
    not a canonical form — stripping decoration from text that is then displayed
    would quietly rewrite the PRD.

    Case-folded since RC1-326: the agent lowercases a leading capital when it
    quotes mid-sentence (`"the v1.0 feature set…"` for the PRD's `"The v1.0
    feature set…"`). The check exists to catch work proposed from nothing; a
    letter's case carries no such signal, and every word must still appear in
    order for a quote to match.
    """
    return normalize_whitespace(_DECORATION.sub("", text)).casefold()


def is_verbatim(quote: str, source_text: str) -> bool:
    """Whether `quote` appears verbatim in `source_text`, up to wrapping/markup.

    The one matching rule shared by plan validation (`flag_unverifiable_quotes`)
    and the spec gate's quote verification (RC1-290). Callers checking many
    quotes against one source should pre-normalize with
    `normalize_for_quote_match(source_text)` and use `in` directly.
    """
    return normalize_for_quote_match(quote) in normalize_for_quote_match(source_text)
