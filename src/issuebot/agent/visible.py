"""The issue text as its reader saw it: GitHub's render, back to text (GHSA-f3fm-r55f-2vgm).

A human applying ``issuebot/todo`` approves the issue page GitHub rendered; a session used to
be handed the raw Markdown. An HTML comment, a link definition nothing uses, a zero-width
character: none is on the page, and all reached the prompt. The first cut of this module
removed those from the Markdown with fence-aware regexes, and review found single-line
whole-body bypasses and a backtracking hang -- every fix another approximation of GitHub's
own renderer. So this module does not render Markdown. It takes ``bodyHTML``, the sanitised
HTML the page was built from, and turns it back into text: text nodes only, so attribute
text (``alt``, ``title``) is not text; ``<pre>`` back to a fence with its language; ``<code>``
to backticks; ``<a>`` to ``[text](href)``, since a session needs URLs for legitimate steps
and the workflow's pinned-reference rule governs what it may follow; block elements to line
breaks; the content of ``<script>``, ``<style>`` and ``<template>`` dropped; ``<img>``
rendered as nothing. Then the characters that print as nothing go: Unicode format
characters, variation selectors, C0 controls other than tab and newline.

What is deliberately kept: a collapsed ``<details>`` block, whose summary line is visible
and which a reader can expand; and length. ``bodyText`` was not used because it flattens the
fences the steps live in.
"""

import re
import unicodedata
from html.parser import HTMLParser

_BLOCK = frozenset(
    {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "tr", "table"}
    | {"blockquote", "details", "summary", "hr", "section"}
)
_DROPPED = frozenset({"script", "style", "template"})
_LANGUAGE = re.compile(r"highlight-(?:source|text)-([A-Za-z0-9_+-]+)")
_WORD = re.compile(r"[A-Za-z0-9_+-]+")
_VARIATION = frozenset(range(0xFE00, 0xFE10)) | frozenset(range(0xE0100, 0xE01F0))
_HOLE = re.compile(r"\x00(\d+)\x00")


def strip_invisible(text: str) -> str:
    """``text`` without the characters that print as nothing."""
    return "".join(
        c
        for c in text
        if unicodedata.category(c) != "Cf"
        and ord(c) not in _VARIATION
        and not (ord(c) < 0x20 and c not in "\t\n")
        and ord(c) != 0x7F
    )


class _ToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.fences: list[str] = []  # finished fences, held out of the blank-line collapse
        self._pre = 0
        self._pre_buf: list[str] = []
        self._pre_lang = ""
        self._dropped = 0
        self._language: str | None = None
        self._link: list[tuple[str | None, int]] = []  # (href, index into parts)

    def _end_fence(self) -> None:
        content = "".join(self._pre_buf)
        if content and not content.endswith("\n"):
            content += "\n"
        self.fences.append(f"```{self._pre_lang}\n{content}```")
        self.parts.append(f"\n\x00{len(self.fences) - 1}\x00\n")
        self._pre_buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in _DROPPED:
            self._dropped += 1
            return
        if self._dropped:
            return
        if tag == "div" and a.get("class"):
            match = _LANGUAGE.search(a["class"] or "")
            if match:
                self._language = match.group(1)
        if tag == "pre":
            if not self._pre:
                lang = _WORD.match(a.get("lang") or "")
                self._pre_lang = lang.group(0) if lang else (self._language or "")
            self._pre += 1
            return
        if self._pre:
            return
        if tag == "code":
            self.parts.append("`")
        elif tag == "a":
            self._link.append((a.get("href"), len(self.parts)))
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "input" and (a.get("type") or "").lower() == "checkbox":
            self.parts.append("[x]" if "checked" in a else "[ ]")
        elif tag == "li":
            self.parts.append("- ")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED:
            self._dropped = max(0, self._dropped - 1)
            return
        if self._dropped:
            return
        if tag == "pre":
            if self._pre:
                self._pre -= 1
                if not self._pre:
                    self._end_fence()
            return
        if self._pre:
            return
        if tag == "code":
            self.parts.append("`")
        elif tag == "a" and self._link:
            href, start = self._link.pop()
            href = "".join(strip_invisible(href or "").split())
            text = "".join(self.parts[start:])
            if href and text.strip() and href != text.strip():
                del self.parts[start:]
                self.parts.append(f"[{text}]({href})")
        elif tag == "div":
            self._language = None
            self.parts.append("\n")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._dropped:
            return
        # Stripped here, so no C0 control (the NUL of a fence's hole) can arrive in the text.
        (self._pre_buf if self._pre else self.parts).append(strip_invisible(data))

    def handle_comment(self, data: str) -> None:
        return  # not on the page


def visible_text(html: str) -> str:
    """``bodyHTML`` as readable text, minus everything the page did not show."""
    parser = _ToText()
    parser.feed(html)
    parser.close()
    if parser._pre:  # a <pre> never closed: what was inside it was still in the page
        parser._end_fence()
    lines = [line.rstrip() for line in "".join(parser.parts).split("\n")]
    # Blank lines outside a fence are dropped: the page's spacing is not information. A fence is
    # one hole in these lines, so its own blank lines and spacing are never touched.
    collapsed = [line for line in lines if line]
    text = "\n".join(collapsed) + ("\n" if collapsed else "")
    return _HOLE.sub(lambda m: parser.fences[int(m.group(1))], text)
