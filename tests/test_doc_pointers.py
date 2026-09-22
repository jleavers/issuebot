"""A pointer names the file the section it quotes actually lives in (#210).

Prose in this repository has one home each, and the homes have moved (#196, #199). The
files an operator reads outside the documentation -- ``compose.yaml`` and ``.env.example``
above all -- point at a section by naming its document and quoting its title, as
``(docs/operations.md, "Rotating the database password")``. A split that moves the section
and not the pointer leaves an operator opening a file and searching for a heading that is
not there, which is what #206 found in ``docs/`` and this issue found in those two files.

The shape is checkable, so it is checked rather than re-swept by hand: a pointer is a
quoted title beside a document's name, and every document in the tree declares its own
headings. Nothing here judges prose that names a document without quoting a section --
"the README's step 2" is a reference to a numbered step, not to a heading -- so the rule
binds exactly the form that can go stale silently.

``docs/superpowers/`` is deliberately outside the sweep: those are dated design records,
and a pointer in one is a statement about where the content was when it was written.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The files that carry a pointer of this shape, and the ones a stale pointer costs most:
# ``OPERATOR_FACING`` in ``tests/test_compose_credentials.py`` is the nearest precedent and
# already names the first two. A new document joins the list when it is written, which is
# the one manual step; a section *moving* is what this catches on its own.
POINTER_FILES = [
    "compose.yaml",
    ".env.example",
    "README.md",
    "CONTRIBUTING.md",
    "CLAUDE.md",
    "docs/operations.md",
    "docs/security-model.md",
    "docs/toolchains.md",
    "docs/dashboard.md",
]

# ``<document>, "<section title>"``: the document named, then the title in quotes beside it.
# The backtick is optional because a markdown file spells the name as code
# (``[`docs/operations.md`, "Rotating the database password"](...)``) while a YAML or env
# comment spells it bare. ``README`` is admitted without its extension, which is how every
# one of these pointers has always written it.
POINTER = re.compile(
    r"(?P<document>README(?:\.md)?|[\w.-]*(?:/[\w.-]+)*\.md)`?,\s+\"(?P<title>[^\"\n]+)\""
)
# An ATX heading, with the optional closing run of hashes ``#`` allows.
HEADING = re.compile(r"^\s{0,3}(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*#*$")
FENCE = re.compile(r"^\s*(?:```|~~~)")
# A line of an ordinary ``#`` comment, in the files that are not markdown.
COMMENT = re.compile(r"^\s*#\s?")


def _normalise(title: str) -> str:
    """A title as the other spelling of it would write it.

    A heading may carry code spans or emphasis that a pointer quoting it drops, and the
    wrapping a pointer survives leaves its own whitespace, so both sides are compared with
    the markup removed, the whitespace collapsed and the case folded.
    """
    return " ".join(title.replace("`", "").replace("*", "").replace("_", "").split()).casefold()


def _headings(path: Path) -> set[str]:
    """Every section title in a markdown file, normalised.

    Fenced blocks are skipped: ``docs/operations.md``'s rotation recipe is a shell script
    whose comments start with ``#``, and reading those as headings would have the sweep
    accept a title no reader can find.
    """
    found: set[str] = set()
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = HEADING.match(line)
        if heading:
            found.add(_normalise(heading.group("title")))
    return found


def _runs(relative: str, text: str) -> list[tuple[int, str]]:
    """The file's prose as ``(first line number, unwrapped text)`` runs.

    A pointer is one phrase that was wrapped where the line ran out, so
    ``(docs/toolchains.md, "A`` / ``PowerShell server ...")`` is one pointer and not two
    fragments; joining the run is what lets a single regex see it. A run ends at a blank
    line or at a line that is not prose -- for markdown every non-blank line, and
    elsewhere a ``#`` comment -- so no pointer is ever assembled across a gap.
    """
    markdown = relative.endswith(".md")
    runs: list[tuple[int, str]] = []
    current: list[str] = []
    start = 0
    for number, line in enumerate(text.splitlines(), start=1):
        if markdown:
            prose = line.strip() or None
        else:
            comment = COMMENT.match(line)
            prose = line[comment.end() :].strip() if comment else None
            prose = prose or None
        if prose is None:
            if current:
                runs.append((start, " ".join(current)))
                current = []
            continue
        if not current:
            start = number
        current.append(prose)
    if current:
        runs.append((start, " ".join(current)))
    return runs


def _resolve(relative: str, document: str) -> Path:
    """The file a pointer names, read from the referring file's own directory then the root.

    ``README`` without its extension is the one abbreviation the pointers use.
    """
    name = "README.md" if document == "README" else document
    beside = (ROOT / relative).parent / name
    return beside if beside.is_file() else ROOT / name


def _stale(relative: str) -> list[str]:
    """Every pointer in the file that names no heading of the document it names."""
    text = (ROOT / relative).read_text(encoding="utf-8")
    complaints = []
    for number, run in _runs(relative, text):
        for pointer in POINTER.finditer(run):
            document, title = pointer.group("document"), pointer.group("title")
            target = _resolve(relative, document)
            if not target.is_file():
                complaints.append(f"{relative}:{number}: {document} is not a file in the tree")
            elif _normalise(title) not in _headings(target):
                complaints.append(f'{relative}:{number}: {document} has no section "{title}"')
    return complaints


@pytest.mark.parametrize("relative", POINTER_FILES)
def test_every_pointer_names_the_document_the_section_lives_in(relative: str) -> None:
    assert _stale(relative) == [], "\n".join(_stale(relative))


def test_the_sweep_reads_the_pointers_it_is_there_for() -> None:
    """The files this issue repointed carry pointers, so a regex that stopped matching fails.

    Every assertion above passes over a file with no pointers in it at all, which is
    exactly what a broken ``POINTER`` would produce. Fifteen is what #210 moved out of the
    README; the count is a floor, not a pin, so adding a pointer does not fail the suite.
    """
    for relative, expected in (("compose.yaml", 7), (".env.example", 8)):
        text = (ROOT / relative).read_text(encoding="utf-8")
        found = [
            pointer.group("document", "title")
            for _, run in _runs(relative, text)
            for pointer in POINTER.finditer(run)
        ]
        assert len(found) >= expected, f"{relative}: {found}"
        assert all(document.startswith("docs/") for document, _ in found), found


def test_a_reference_that_quotes_no_section_is_not_a_pointer() -> None:
    """The four references #210 left alone name a step or a requirement, not a heading.

    They are the reason the rule is "a title in quotes beside a document" and not "the word
    README": the README does still carry the setting-up steps and the prerequisites, and a
    sweep that flagged those would be asking for them to be broken.
    """
    for phrase in (
        "the README's step 2, before anything is up",
        "newer, which the README's requirements name",
        "created once per host (README, step 1), external",
        "a CI run failed). See the README's prerequisites for what",
    ):
        assert POINTER.search(phrase) is None, phrase


@pytest.mark.parametrize(
    ("phrase", "document", "title"),
    [
        (
            '(docs/operations.md, "Rotating the database password")',
            "docs/operations.md",
            "Rotating the database password",
        ),
        (
            'See docs/security-model.md, "What a session may reach".',
            "docs/security-model.md",
            "What a session may reach",
        ),
        (
            '(see docs/operations.md, "More than one repository")',
            "docs/operations.md",
            "More than one repository",
        ),
        (
            '[`docs/operations.md`, "Blocked"](docs/operations.md#blocked)',
            "docs/operations.md",
            "Blocked",
        ),
        ('(README, "Rotating the database password")', "README", "Rotating the database password"),
    ],
)
def test_pointer_shape(phrase: str, document: str, title: str) -> None:
    match = POINTER.search(phrase)
    assert match is not None, phrase
    assert match.group("document") == document
    assert match.group("title") == title


def test_a_wrapped_pointer_is_read_as_one_pointer() -> None:
    """The form this issue had to fix by hand: a pointer split across two comment lines."""
    text = (
        "        # need a real server, and `docker compose build worker` to pick it up\n"
        "        # (docs/toolchains.md, \"A PostgreSQL server for the target repository's\n"
        '        # tests"). The web service builds from the same context without it.\n'
    )
    runs = _runs("compose.yaml", text)
    assert len(runs) == 1 and runs[0][0] == 1
    match = POINTER.search(runs[0][1])
    assert match is not None
    assert match.group("title") == "A PostgreSQL server for the target repository's tests"


def test_headings_skip_a_fenced_block() -> None:
    """``docs/operations.md``'s rotation recipe is a shell script full of ``#`` comments."""
    headings = _headings(ROOT / "docs" / "operations.md")
    assert "rotating the database password" in headings
    assert not any(heading.startswith("1. put the new value") for heading in headings)
