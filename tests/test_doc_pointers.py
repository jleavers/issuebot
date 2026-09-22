"""A cross-document pointer resolves against the document it names (#210, #218).

The repository's prose navigates itself: `README.md` sends a reader to `docs/operations.md`
for a procedure, `CLAUDE.md` cites a section of it, and the `docs/` files cite each other. A
pointer carries an *anchor* -- `[Safety](docs/operations.md#safety)` -- and an anchor is
GitHub's slug of a heading, so renaming that heading breaks the link with no sign that
anything is wrong: the link still renders, the click still loads the file, and the browser
lands at the top instead of at the section. Nothing in a markdown renderer checks it, and the
prose is edited far more often than it is clicked through.

So the sweep below is the check: every markdown link in `SWEPT_FILES` that names a path is
resolved against the tree, and every one that carries an anchor is resolved against the
headings of the file it lands in. A rename that moves a section out from under a pointer now
fails a test rather than a reader.

Two halves, and the second is where the work is. `_headings` parses the ATX headings of a
markdown file, and `github_slug` is GitHub's slug rule -- lower-cased, punctuation other than
hyphens and underscores dropped, spaces to hyphens -- with `_anchors` adding the numeric
suffix a repeated heading gets. That rule is somebody else's, ported rather than invented, so
it is pinned by a truth table of its own (`SLUG_CASES`, `DUPLICATE_CASES`) instead of only by
whatever headings the tree happens to carry today.

Where the slug rule is *wrong* it fails loudly rather than quietly: an unhandled inline
construct in a heading gives a slug no pointer matches, so the sweep reports a pointer that
is in fact fine. That is the safe direction for a guard whose subject is other people's
markdown, and it is why the inline handling below covers what this tree's headings use --
code spans -- plus the neighbours a heading could grow, rather than all of CommonMark.

`docs/superpowers/` is outside the sweep. Those are dated design records: a pointer in one is
a statement about where the content was when the document was written, and holding it to
today's headings would make a correct record fail.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The documents a reader navigates between, and so the files whose pointers have to land. The
# root set is the operator-facing prose plus the two instruction files; `docs/*.md` is a glob
# rather than a list so a document added tomorrow is swept the day it lands, and it is
# deliberately not recursive -- that is what keeps `docs/superpowers/` outside the sweep by
# construction rather than by an exclusion somebody has to remember.
#
# Out, each for its own reason: `docs/superpowers/` (dated records, above);
# `tests/fixtures/workflows/` (fixtures, some deliberately invalid, whose contents are their
# tests' subject); `.claude/` (session configuration this repository does not load as its own,
# per `claude.setting_sources`, rather than documentation).
SWEPT_FILES: tuple[str, ...] = (
    "README.md",
    "CLAUDE.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    # A workflow file is markdown with YAML front matter, and its body is the prompt template.
    # It carries no pointer today; it is swept so that the first one it grows is checked.
    "configs/WORKFLOW.md",
    # Directory-local documentation. Neither carries a pointer today either, and a list that
    # omitted them for that reason would go stale the moment one did.
    "tools/screenshots/README.md",
    "src/issuebot/web/static/vendor/README.md",
    *sorted(str(path.relative_to(ROOT)) for path in (ROOT / "docs").glob("*.md")),
)

# ``---`` on the first line opens YAML front matter, closed by the next such line. This is not
# decoration: `configs/WORKFLOW.md` *is* that shape -- `issuebot.config` reads the front matter
# and renders the body -- and its YAML comments start with ``#`` in column 0, so a parser that
# did not skip the front matter would read a dozen of them as headings.
_FRONT_MATTER = re.compile(r"^-{3,}\s*$")

# A fenced block, CommonMark's rule: three or more backticks or tildes, indented at most three
# spaces. The closing fence must use the same character, be at least as long, and carry no info
# string -- which is what lets a ````` ````md ````` block hold ``` ``` ``` fences inside it, as
# the workpad template in `configs/WORKFLOW.md` does.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*(.*)$")

# An ATX heading: up to three spaces of indent (four would be an indented code block), one to
# six ``#``, at least one space, and an optional closing run of ``#`` that is not part of the
# text. Setext headings (a line underlined with ``=`` or ``-``) are not used anywhere in this
# tree and are not parsed; one would simply not be found, and a pointer at it would fail.
_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")

# An inline link's target: ``](...)``. Reference-style links, angle-bracket targets and link
# titles are all absent from this tree, so the target is everything up to the closing bracket
# with no whitespace in it.
_LINK = re.compile(r"\]\(\s*([^)\s]+?)\s*\)")

# A link that goes off this repository is nobody here's to check.
_EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)")

# What a heading's *text content* is, which is what GitHub slugs -- the heading is rendered to
# HTML first, so the markup around the words is not in the anchor. An image contributes no text
# at all (its alt lands in an attribute), a link contributes its text, a code span its content,
# and emphasis its own. Images before links, since ``![alt](src)`` contains ``[alt](src)``.
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_INLINE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
# ``*`` and ``**`` only. ``_`` is a slug character -- ``snake_case`` survives into the anchor
# whole -- so stripping it would corrupt every identifier a heading names, and underscore
# emphasis appears in no heading here.
_EMPHASIS = re.compile(r"\*{1,2}")

# GitHub's slug rule, the half that removes: everything that is not a word character, a hyphen
# or a space goes, and what is left keeps its underscores. ``\w`` is Unicode here, so an
# accented letter survives as itself, which is what GitHub does too.
_NOT_SLUG = re.compile(r"[^\w\- ]", re.UNICODE)


def _body_lines(text: str) -> list[tuple[int, str]]:
    """Every line of a markdown document that is prose, numbered from one.

    Front matter and fenced blocks are dropped, so neither a YAML comment nor a shell comment
    can be read as a heading and no link inside an example is read as a pointer. Shared by
    `_headings` and `_links`, because the two shapes have to agree about what is a code block:
    a heading the sweep cannot see is a pointer that cannot land.
    """
    lines = text.splitlines()
    start = 0
    if lines and _FRONT_MATTER.match(lines[0]):
        for index, line in enumerate(lines[1:], 1):
            if _FRONT_MATTER.match(line):
                start = index + 1
                break
    opener: str | None = None
    body: list[tuple[int, str]] = []
    for number, line in enumerate(lines[start:], start + 1):
        fence = _FENCE.match(line)
        if fence:
            run, info = fence.group(1), fence.group(2)
            if opener is None:
                opener = run
            elif run[0] == opener[0] and len(run) >= len(opener) and not info.strip():
                opener = None
            continue
        if opener is None:
            body.append((number, line))
    return body


def _headings(text: str) -> list[str]:
    """The ATX headings of a markdown document, in order, as their source text."""
    return [
        heading.group(2)
        for _, line in _body_lines(text)
        if (heading := _ATX.match(line)) is not None
    ]


def _heading_text(heading: str) -> str:
    """A heading's rendered text content: the words, without the markup around them."""
    text = _IMAGE.sub("", heading)
    text = _INLINE_LINK.sub(r"\1", text)
    text = _CODE_SPAN.sub(r"\2", text)
    return _EMPHASIS.sub("", text)


def github_slug(heading: str) -> str:
    """GitHub's anchor for a heading, before any suffix for a repeated one.

    Lower-case the text content, drop everything that is not a word character, a hyphen or a
    space, then turn each remaining space into a hyphen -- in that order, so two spaces give
    two hyphens and a dropped comma does not join the words either side of it.
    """
    return _NOT_SLUG.sub("", _heading_text(heading).strip().lower()).replace(" ", "-")


def _anchors(headings: list[str]) -> list[str]:
    """The anchors GitHub gives a document's headings, repeats included.

    A heading whose slug is already taken gets ``-1``, then ``-2``, and so on -- and the
    candidate is re-checked each time round, so a document carrying both ``Safety`` twice and a
    literal ``Safety 1`` hands out three distinct anchors rather than two and a collision. That
    is `github-slugger`'s own loop, which is what GitHub renders with.
    """
    occurrences: dict[str, int] = {}
    anchors: list[str] = []
    for heading in headings:
        base = github_slug(heading)
        anchor = base
        while anchor in occurrences:
            occurrences[base] += 1
            anchor = f"{base}-{occurrences[base]}"
        occurrences[anchor] = 0
        anchors.append(anchor)
    return anchors


@cache
def _anchors_of(path: Path) -> tuple[str, ...]:
    """Every anchor `path` offers. Cached: `docs/operations.md` is pointed at a dozen times."""
    return tuple(_anchors(_headings(path.read_text(encoding="utf-8"))))


def _links(text: str) -> list[tuple[int, str]]:
    """Every inline link target in the prose, with the line it sits on."""
    return [
        (number, match.group(1))
        for number, line in _body_lines(text)
        for match in _LINK.finditer(line)
    ]


def _pointer_complaints(name: str, text: str) -> list[str]:
    """Why each pointer in `text` fails to resolve, if any does.

    `name` is the document's path relative to `ROOT`: a relative target is resolved against the
    directory it is written in, not against the root, which is how `docs/operations.md` reaches
    `security-model.md` beside it and `../README.md` above it. The text is passed in rather than
    read here so the sweep's verdict can be exercised on a document that is not in the tree.
    """
    source = ROOT / name
    complaints: list[str] = []
    for number, target in _links(text):
        if _EXTERNAL.match(target):
            continue
        path, _, anchor = target.partition("#")
        anchor = unquote(anchor)
        # A link with no path is a pointer into the document it is written in, which is the
        # same drift one file nearer home: `README.md`'s own table of contents is built of
        # them, and a rename breaks those exactly as silently.
        resolved = (source.parent / path).resolve() if path else source.resolve()
        where = f"{name}:{number}"
        if ROOT not in resolved.parents and resolved != ROOT:
            complaints.append(f"{where}: [...]({target}) leaves the repository")
            continue
        if not resolved.exists():
            complaints.append(f"{where}: [...]({target}) names no such path")
            continue
        if not anchor:
            # The path existing is the whole of the check: a link may name a directory
            # (`docs/superpowers/specs/`) or a file that is not markdown (`LICENSE`).
            continue
        if not resolved.is_file() or resolved.suffix != ".md":
            complaints.append(f"{where}: [...]({target}) puts an anchor on a non-markdown path")
            continue
        anchors = _anchors_of(resolved)
        if anchor not in anchors:
            relative = resolved.relative_to(ROOT)
            complaints.append(
                f"{where}: [...]({target}) -- {relative} has no heading slugging to "
                f"{anchor!r}; it offers {', '.join(repr(one) for one in anchors)}"
            )
    return complaints


@pytest.mark.parametrize("name", SWEPT_FILES)
def test_every_pointer_resolves(name: str) -> None:
    """Every markdown link in a swept file lands: the path exists, and where the link carries
    an anchor, some heading of the file it names slugs to it."""
    text = (ROOT / name).read_text(encoding="utf-8")
    complaints = _pointer_complaints(name, text)
    assert not complaints, "cross-document pointers no longer resolve:\n" + "\n".join(complaints)


def test_the_swept_files_all_exist() -> None:
    """A file renamed out from under this list would leave its pointers unswept in silence."""
    missing = [name for name in SWEPT_FILES if not (ROOT / name).is_file()]
    assert not missing, f"SWEPT_FILES names files that are not in the tree: {missing}"


def test_superpowers_is_outside_the_sweep() -> None:
    """A design record is dated. Its pointers describe where the content was when it was
    written, so holding them to today's headings would make a correct record fail."""
    assert not [name for name in SWEPT_FILES if name.startswith("docs/superpowers/")]
    assert (ROOT / "docs" / "superpowers").is_dir(), "re-anchor this test: the directory moved"


# The slug rule is GitHub's, so it is pinned by a table rather than by the headings this tree
# happens to carry: a case here says what the rule *is*, where a heading only says that one
# pointer matches one heading today.
SLUG_CASES = [
    ("Safety", "safety"),
    ("More than one repository", "more-than-one-repository"),
    # A code span contributes its content: this is the heading `docs/toolchains.md` carries,
    # and the pointer at it in `README.md` only lands because the backticks and the `.` and
    # the `/` all come out.
    ("`.issuebot/env`: what a hook hands the agent", "issuebotenv-what-a-hook-hands-the-agent"),
    ("`.issuebot/env`", "issuebotenv"),
    # Punctuation: an apostrophe is dropped without joining or splitting anything, which is
    # what makes `docs/toolchains.md`'s section anchors read as they do.
    (
        "A PostgreSQL server for the target repository's tests",
        "a-postgresql-server-for-the-target-repositorys-tests",
    ),
    ("Blocked: what now?", "blocked-what-now"),
    ("Rotating the database password (ALTER ROLE)", "rotating-the-database-password-alter-role"),
    # Hyphens and underscores survive; nothing else does.
    ("Fine-grained tokens", "fine-grained-tokens"),
    ("0001_initial and 0002_run_turns", "0001_initial-and-0002_run_turns"),
    # Spaces map one for one, so a double space is two hyphens rather than one.
    ("two  spaces", "two--spaces"),
    ("  padded  ", "padded"),
    # Markup around the words is not in the anchor.
    ("**Never** push to `main`", "never-push-to-main"),
    ("[Safety](#safety) and scope", "safety-and-scope"),
    ("![shield](docs/images/x.png) Licence", "licence"),
    # `\w` is Unicode, so a letter that is not ASCII is a letter.
    ("Café", "café"),
    # A hyphen is a slug character, so a heading of them is its own anchor; a heading of
    # anything else leaves nothing, which is a slug no pointer can match -- and so a loud
    # failure rather than an accidental collision with the next such heading.
    ("---", "---"),
    ("?!", ""),
]


@pytest.mark.parametrize(("heading", "expected"), SLUG_CASES)
def test_github_slug(heading: str, expected: str) -> None:
    assert github_slug(heading) == expected


DUPLICATE_CASES = [
    # The plain repeat.
    (["Safety", "Safety", "Safety"], ["safety", "safety-1", "safety-2"]),
    # Two headings that are not the same text but slug the same way, which is the case an eye
    # skimming a table of contents would never catch.
    (["The `db` service", "The db service"], ["the-db-service", "the-db-service-1"]),
    (["Cost", "cost", "COST"], ["cost", "cost-1", "cost-2"]),
    # A repeat whose suffix is already spoken for by a literal heading: the candidate is
    # re-checked, so the third heading skips past `safety-1` rather than colliding with it.
    (["Safety", "Safety 1", "Safety"], ["safety", "safety-1", "safety-2"]),
    ([], []),
]


@pytest.mark.parametrize(("headings", "expected"), DUPLICATE_CASES)
def test_anchors_suffix_repeated_headings(headings: list[str], expected: list[str]) -> None:
    assert _anchors(headings) == expected


def test_headings_skips_fenced_blocks_and_front_matter() -> None:
    """The two things in this tree's markdown that look like headings and are not: a shell
    comment inside a fence (`CLAUDE.md`'s command block is full of them) and a YAML comment in
    a workflow file's front matter. A longer fence holds shorter ones, which is how the workpad
    template in `configs/WORKFLOW.md` nests."""
    document = """---
github:
  # not a heading: YAML front matter
  repo: jleavers/issuebot
---
# Real

```bash
# not a heading: a shell comment
```

## Also real

````md
```
### not a heading: a fence inside a fence
```
````

### Third ###
"""
    assert _headings(document) == ["Real", "Also real", "Third"]


def test_links_skips_fenced_blocks() -> None:
    """A link inside an example is an example, not a pointer at anything."""
    document = """[live](docs/operations.md#safety)

```md
[an example](docs/nowhere.md#gone)
```

[also live](#top)
"""
    assert _links(document) == [(1, "docs/operations.md#safety"), (7, "#top")]


def test_the_sweep_reports_a_pointer_that_does_not_land() -> None:
    """The negative proof: a document of this test's own, carrying one pointer of each kind
    that fails and one that still lands. Without this the sweep could be vacuous -- every
    pointer in the tree resolves today, so a check that reported nothing whatever it was given
    would pass the file above just as well."""
    document = "\n".join(
        (
            "[lands](docs/operations.md#safety)",
            "[renamed](docs/operations.md#safety-and-scope)",
            "[gone](docs/nowhere.md)",
            "[not markdown](LICENSE#safety)",
            "[escapes](../elsewhere.md)",
            "[own file](#no-such-heading-here)",
        )
    )
    complaints = _pointer_complaints("README.md", document)
    assert [complaint.split(": ", 1)[0] for complaint in complaints] == [
        "README.md:2",
        "README.md:3",
        "README.md:4",
        "README.md:5",
        "README.md:6",
    ]
    assert "has no heading slugging to 'safety-and-scope'" in complaints[0]
    assert "names no such path" in complaints[1]
    assert "puts an anchor on a non-markdown path" in complaints[2]
    assert "leaves the repository" in complaints[3]
    assert "has no heading slugging to 'no-such-heading-here'" in complaints[4]
