"""Spec ingestion — markdown to offset-preserving sections (RC1-286).

The parser tiles the document: every character of the source belongs to exactly
one section, each section records its offsets, and slicing the source by those
offsets reproduces the section's text. Nothing is normalized away at ingest
time — quote matching normalizes at *match* time (`spec_gate.text`), because a
lossy ingest would make every verbatim quote a near-miss.

Fence awareness: a `# comment` inside a fenced code block is not a heading and
does not start a section. The shipped fixture PRDs contain no fenced headings,
so `validation.markdown_sections` (which delegates here) is behavior-identical
on every existing corpus — guarded by a regression test against the old regex.

Remote sources: `SpecSource` is the seam. `MarkdownFile` is the only
implementation for now; Confluence/Notion adapters are deferred until the
rubric works (see the RC1-286 ticket), and anything that lands here must keep
`origin` stably retrievable — downstream commands re-read the spec by path
(see the RC1-293 note on `source_document`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_LINE = re.compile(r"^\s*```")


@dataclass(frozen=True)
class SpecSection:
    """One tile of the document. `text` and `body` slice the shared source."""

    heading: str | None  # stripped heading text; None for preamble or a headingless doc
    level: int  # heading level 1-6; 0 when heading is None
    start: int  # offset of the section's first character (the heading line)
    body_start: int  # offset where the body begins (past the heading line)
    end: int  # offset one past the section's last character
    source: str = field(repr=False)  # the full document, shared by reference

    @property
    def text(self) -> str:
        return self.source[self.start : self.end]

    @property
    def body(self) -> str:
        return self.source[self.body_start : self.end]

    @property
    def prose_body(self) -> str:
        """The body with fenced code blocks removed — what the rubric may scan.

        Fenced content is never scanned for prose findings: a TODO in a code
        sample is not an unresolved scope marker.
        """
        out: list[str] = []
        in_fence = False
        for line in self.body.splitlines(keepends=True):
            if _FENCE_LINE.match(line):
                in_fence = not in_fence
                continue
            if not in_fence:
                out.append(line)
        return "".join(out)


def parse_sections(source: str) -> list[SpecSection]:
    """Split a markdown document into sections that tile it exactly.

    A document with no headings (or an empty one) yields a single section with
    ``heading=None`` — a flat spec is bad, but it is not unparseable.
    """
    boundaries: list[tuple[int, int, str, int]] = []  # (start, body_start, heading, level)
    in_fence = False
    offset = 0
    for line in source.splitlines(keepends=True):
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            match = _HEADING_LINE.match(line.rstrip("\n"))
            if match:
                boundaries.append(
                    (offset, offset + len(line), match.group(2), len(match.group(1)))
                )
        offset += len(line)

    if not boundaries:
        return [
            SpecSection(
                heading=None, level=0, start=0, body_start=0, end=len(source), source=source
            )
        ]

    sections: list[SpecSection] = []
    first_start = boundaries[0][0]
    if first_start > 0:  # preamble before the first heading
        sections.append(
            SpecSection(
                heading=None, level=0, start=0, body_start=0, end=first_start, source=source
            )
        )
    for i, (start, body_start, heading, level) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(source)
        sections.append(
            SpecSection(
                heading=heading,
                level=level,
                start=start,
                body_start=body_start,
                end=end,
                source=source,
            )
        )
    return sections


@dataclass(frozen=True)
class LoadedSpec:
    """A spec's text plus where it came from. `origin` must stay re-readable."""

    text: str
    origin: str


class SpecSource(Protocol):
    """Where a spec comes from. Confluence/Notion adapters implement this later."""

    def load(self) -> LoadedSpec: ...


@dataclass(frozen=True)
class MarkdownFile:
    """The one implemented source: a markdown file on disk."""

    path: Path

    def load(self) -> LoadedSpec:
        return LoadedSpec(text=self.path.read_text(), origin=str(self.path))
