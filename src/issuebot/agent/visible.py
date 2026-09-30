"""The issue text as its reader saw it: GitHub's render, back to text (GHSA-f3fm-r55f-2vgm).

A human applying ``issuebot/todo`` approves the issue page GitHub rendered; a session used to
be handed the raw Markdown. An HTML comment, a link definition nothing uses, a zero-width
character: none is on the page, and all reached the prompt. The first cut of this module
removed those from the Markdown with fence-aware regexes, and review found single-line
whole-body bypasses and a backtracking hang -- every fix another approximation of GitHub's
own renderer. So this module does not render Markdown. It takes ``bodyHTML``, the sanitised
HTML the page was built from, and turns it back into text: text nodes only, so attribute
text (``alt``, ``title``) is not text; ``<pre>`` back to a fence (long enough that no line
inside can close it) with the language from GitHub's ``highlight-source-*`` class; ``<code>``
to backticks; ``<del>``, ``<s>`` and ``<strike>`` to ``~~``;
``<a>`` to ``[text](href)``; block elements to line breaks
and table cells to `` | ``; the content of ``<script>``, ``<style>``, ``<template>``, ``<rp>``
and ``sr-only`` elements dropped, as is text nested three or more levels deep in the elements
whose font size compounds when nested (``sub``, ``sup``, ``small``, ``code``, ``tt``), which is
below a pixel on the page; ``<img>`` rendered as nothing. Then
the characters that print as nothing go: Unicode format characters, variation selectors,
fillers, C0 and C1 controls other than tab and newline.

``href`` is the one attribute whose text reaches the output: a session needs URLs for
legitimate steps, and the workflow's pinned-reference rule governs what it may follow. It is
shown in the text whenever it differs from the link text, so a reader sees where it goes.
``lang=`` is not carried over: GitHub emits it only for an info string it did not recognise, so
it is a channel the page never shows; only the languages of GitHub's own renderers (``mermaid``,
``math``, ``geojson``, ``topojson``, ``stl``) are kept, because they change what the reader sees.

Residual classes, kept knowingly: the content of a collapsed ``<details>`` block (its summary
shows and a reader can expand it); text hidden inside the special renderers beyond the best
effort here (``%%`` lines of a mermaid block and ``\\phantom`` of a math block are dropped);
and visual against logical order under ``dir="rtl"``. ``bodyText`` was not used because it
flattens the fences the steps live in.
"""

import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import quote

_BLOCK = frozenset(
    {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "tr", "table"}
    | {"blockquote", "details", "summary", "hr", "section", "dt", "dd", "caption", "figcaption"}
)
_DROPPED = frozenset({"script", "style", "template", "rp"})
_STRIKE = frozenset({"del", "s", "strike"})
_CELL = frozenset({"td", "th"})
_SMALL = frozenset({"sub", "sup", "small", "code", "tt"})
_SMALL_LIMIT = 3
"""Elements whose font size compounds when nested: ``sub`` and ``sup`` (75% each), ``small``
(90%) and ``code`` and ``tt`` (85%). Text nested this deep is below a pixel on the page: one
level is ordinary markup (``<pre><code>`` is one), two is rare, three has no honest use."""
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param"}
    | {"source", "track", "wbr"}
)
_SPECIAL_LANGUAGES = frozenset({"mermaid", "math", "geojson", "topojson", "stl"})
_LANGUAGE = re.compile(r"highlight-(?:source|text)-([A-Za-z0-9_+-]+)")
_PHANTOM = re.compile(r"\\[hv]?phantom\s*\{[^{}]*\}")
_VARIATION = frozenset(range(0xFE00, 0xFE10)) | frozenset(range(0xE0100, 0xE01F0))
_BLANKS = frozenset({0x34F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x17B4, 0x17B5})
_BLANKS |= frozenset(range(0x180B, 0x1810))
_MATH_SPAN = re.compile(r"(\$\$?)([^$]+)\1")
_MERMAID_ACC = re.compile(r"\s*acc(?:Title|Descr)\s*([:{])")
_HOLE = re.compile(r"\x00(\d+)\x00")


def strip_invisible(text: str) -> str:
    """``text`` without the characters that print as nothing."""
    return "".join(
        c
        for c in text
        if unicodedata.category(c) != "Cf"
        and ord(c) not in _VARIATION
        and ord(c) not in _BLANKS
        and not (ord(c) < 0x20 and c not in "\t\n")
        and not (0x7F <= ord(c) <= 0x9F)
    )


def _unphantom(text: str) -> str:
    """``text`` without ``\\phantom{...}``; a math span it leaves empty goes whole."""

    def span(m: re.Match[str]) -> str:
        cleaned = _PHANTOM.sub("", m.group(2))
        if cleaned != m.group(2) and not cleaned.strip():
            return ""  # `$$` left behind would read as a display-math delimiter
        return f"{m.group(1)}{cleaned}{m.group(1)}"

    return _PHANTOM.sub("", _MATH_SPAN.sub(span, text))


def _newlines(text: str) -> str:
    """HTML5 preprocessing of line ends, plus the two Unicode separators."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\u2028", "\n").replace("\u2029", "\n")


def _clean(text: str) -> str:
    return strip_invisible(_newlines(text))


class _ToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.fences: list[str] = []  # finished fences, held out of the blank-line collapse
        self._pre = 0
        self._pre_buf: list[str] = []
        self._pre_lang = ""
        self._renderer = False  # the fence language came from lang=, not from the class
        self._table = 0
        self._cell = 0
        self._dropped = 0
        self._hidden: tuple[str, int] | None = None  # (tag, depth) of an sr-only element
        self._math = 0
        self._small = 0  # open depth of the elements in _SMALL
        self._first_cell = True
        self._language: str | None = None
        self._link: list[tuple[str | None, int]] = []  # (href, index into parts)

    def _out(self, text: str) -> None:
        (self._pre_buf if self._pre else self.parts).append(text)

    def _end_fence(self) -> None:
        content = "".join(self._pre_buf)
        if self._renderer and self._pre_lang == "mermaid":
            # Drawn as a diagram: comments and accessibility text are not on the picture.
            kept: list[str] = []
            in_block = False
            for line in content.split("\n"):
                if in_block:  # an accDescr { ... } body, through the line that opens with }
                    in_block = not line.lstrip().startswith("}")
                    continue
                if line.lstrip().startswith("%%"):
                    continue
                acc = _MERMAID_ACC.match(line)
                if acc:
                    in_block = acc.group(1) == "{" and "}" not in line[acc.end() :]
                    continue
                kept.append(line)
            content = "\n".join(kept)
        elif self._renderer and self._pre_lang == "math":
            content = _unphantom(content)
        if content and not content.endswith("\n"):
            content += "\n"
        longest = max((len(run) for run in re.findall(r"`+", content)), default=0)
        ticks = "`" * max(3, longest + 1)
        self.fences.append(f"{ticks}{self._pre_lang}\n{content}{ticks}")
        self.parts.append(f"\n\x00{len(self.fences) - 1}\x00\n")
        self._pre_buf = []

    def close(self) -> None:
        super().close()
        if self._pre:  # a <pre> never closed: what was inside it was still in the page
            self._pre = 0
            self._end_fence()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if self._hidden:
            if tag == self._hidden[0]:
                self._hidden = (tag, self._hidden[1] + 1)
            return
        if tag in _DROPPED:
            self._dropped += 1
            return
        if self._dropped:
            return
        classes = (a.get("class") or "").split()
        if "sr-only" in classes and tag not in _VOID:
            self._hidden = (tag, 1)
            return
        if tag in _SMALL:
            self._small += 1
        if tag == "div":
            match = _LANGUAGE.search(a.get("class") or "")
            if match:
                self._language = match.group(1)
        if tag == "pre":
            if not self._pre:
                lang = a.get("lang")
                self._renderer = lang in _SPECIAL_LANGUAGES
                self._pre_lang = lang if self._renderer else (self._language or "")
            self._pre += 1
            return
        if self._pre:
            if tag == "br" or tag in _BLOCK or tag in _CELL:
                self._pre_buf.append("\n")
            return
        if tag == "math-renderer":
            self._math += 1
        elif tag == "code" or tag in _STRIKE:
            self.parts.append("`" if tag == "code" else "~~")
        elif tag == "a":
            self._link.append((a.get("href"), len(self.parts)))
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "input" and (a.get("type") or "").lower() == "checkbox":
            self.parts.append("[x]" if "checked" in a else "[ ]")
        elif tag == "li":
            self.parts.append("- ")
        elif tag == "tr":
            self._first_cell = True
            self.parts.append("\n")
        elif tag == "table":
            self._table += 1
            self.parts.append("\n")
        elif tag in _CELL:
            self._cell += 1
            if not self._first_cell:
                self.parts.append(" | ")
            self._first_cell = False
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._hidden:
            if tag == self._hidden[0]:
                depth = self._hidden[1] - 1
                self._hidden = (tag, depth) if depth else None
            return
        if tag in _DROPPED:
            self._dropped = max(0, self._dropped - 1)
            return
        if self._dropped:
            return
        if tag in _SMALL:
            self._small = max(0, self._small - 1)
        if tag == "pre":
            if self._pre:
                self._pre -= 1
                if not self._pre:
                    self._end_fence()
            return
        if self._pre:
            return
        if tag == "math-renderer":
            self._math = max(0, self._math - 1)
        elif tag == "code" or tag in _STRIKE:
            self.parts.append("`" if tag == "code" else "~~")
        elif tag == "a" and self._link:
            href, start = self._link.pop()
            href = re.sub(r"\s", lambda m: quote(m.group()), strip_invisible(href or ""))
            text = "".join(self.parts[start:])
            if href and text.strip() and href != text.strip():
                del self.parts[start:]
                self.parts.append(f"[{text}]({href})")
        elif tag == "table":
            self._table = max(0, self._table - 1)
            self.parts.append("\n")
        elif tag in _CELL:
            self._cell = max(0, self._cell - 1)
        elif tag == "div":
            self._language = None
            self.parts.append("\n")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # GitHub re-serialises bodyHTML with explicit end tags, so <x/> never arrives from it;
        # HTML5 itself ignores the slash on a non-void element. Were one to arrive it would
        # open a drop or a fence nothing closes, so only the true voids are honoured.
        if tag in _VOID:
            self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._dropped or self._hidden or self._small >= _SMALL_LIMIT:
            return
        # Stripped here, so no C0 control (the NUL of a fence's hole) can arrive in the text.
        data = _clean(data)
        if self._table and not self._cell and not self._pre and not data.strip():
            return  # the newlines cmark puts between cells and rows
        if self._math and not self._pre:
            data = _unphantom(data)
        self._out(data)

    def handle_comment(self, data: str) -> None:
        return  # not on the page


def visible_text(html: str) -> str:
    """``bodyHTML`` as readable text, minus everything the page did not show."""
    parser = _ToText()
    parser.feed(_newlines(html))
    parser.close()
    lines = [line.rstrip() for line in "".join(parser.parts).split("\n")]
    # Blank lines outside a fence are dropped: the page's spacing is not information. A fence is
    # one hole in these lines, so its own blank lines and spacing are never touched.
    collapsed = [line for line in lines if line]
    text = "\n".join(collapsed) + ("\n" if collapsed else "")
    return _HOLE.sub(lambda m: parser.fences[int(m.group(1))], text)
