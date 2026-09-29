"""The issue text as its reader saw it rendered (GHSA-f3fm-r55f-2vgm).

A human applying ``issuebot/todo`` approves the issue page GitHub rendered; the session is
handed the raw Markdown. Three things GitHub renders as nothing therefore reach a session
past an honest approval: an HTML comment, a link-reference definition nothing in the text
uses, and a Unicode format character (category ``Cf``: zero-width joiners and spaces,
direction marks, the byte-order mark). This module removes them, and only them.

It is fence-aware because the same bytes *are* rendered inside a fenced code block or an
inline code span, and that is where a description's steps live. What it leaves alone, on
purpose: a collapsed ``<details>`` block, whose summary line is visible and which a reader
can expand; and length, since a long body was on the page. GraphQL's ``bodyText`` is the
rendered text and was not used because it flattens code fences.

Pure, standard library only, and deliberately not a Markdown parser: the aim is to remove
the classes GitHub hides, not to render.
"""

import re
import unicodedata

_FENCE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")
# A comment, or an inline code span (which a comment must not be removed from). Spans use one
# or more backticks and close on the same run; the tempered pattern stops a shorter run
# inside a longer span from closing it.
_COMMENT_OR_SPAN = re.compile(
    r"(?P<span>(?P<ticks>`+)(?:(?!(?P=ticks))[\s\S])+?(?P=ticks))"
    r"|(?P<comment><!--[\s\S]*?-->)"
)
_LINK_DEFINITION = re.compile(r"^ {0,3}\[(?P<label>[^\]]+)\]:[ \t]*\S")
_REFERENCE = re.compile(r"\[(?P<label>[^\]]+)\]")


def strip_format_characters(text: str) -> str:
    """``text`` without the characters Unicode prints as nothing."""
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


def _split_fences(markdown: str) -> list[tuple[bool, str]]:
    """``(fenced, chunk)`` pairs, each chunk a run of whole lines; fences belong to their block."""
    chunks: list[tuple[bool, str]] = []
    current: list[str] = []
    fenced = False
    closing: str | None = None
    for line in markdown.splitlines(keepends=True):
        match = _FENCE.match(line)
        if not fenced and match:
            if current:
                chunks.append((False, "".join(current)))
                current = []
            fenced, closing = True, match.group("fence")
            current.append(line)
            continue
        if (
            fenced
            and match
            and closing is not None
            and match.group("fence")[0] == closing[0]
            and len(match.group("fence")) >= len(closing)
        ):
            current.append(line)
            chunks.append((True, "".join(current)))
            current, fenced, closing = [], False, None
            continue
        current.append(line)
    if current:
        chunks.append((fenced, "".join(current)))
    return chunks


def _without_comments(chunk: str) -> str:
    return _COMMENT_OR_SPAN.sub(lambda m: m.group("span") or "", chunk)


def visible_text(markdown: str) -> str:
    """``markdown`` as GitHub renders it, minus what it renders as nothing."""
    chunks = _split_fences(markdown)
    outside = _without_comments("".join(chunk for fenced, chunk in chunks if not fenced))
    # A definition's own `[label]` is not a use of it, so the labels in use are read off the
    # text with the definition lines left out.
    prose = "".join(
        line for line in outside.splitlines(keepends=True) if not _LINK_DEFINITION.match(line)
    )
    labels_used = {m.group("label").lower() for m in _REFERENCE.finditer(prose)}
    lines: list[str] = []
    for fenced, chunk in chunks:
        if fenced:
            lines.append(chunk)
            continue
        text = _without_comments(chunk)
        kept = []
        for line in text.splitlines(keepends=True):
            definition = _LINK_DEFINITION.match(line)
            if definition and definition.group("label").lower() not in labels_used:
                continue
            kept.append(line)
        lines.append("".join(kept))
    return strip_format_characters("".join(lines))
