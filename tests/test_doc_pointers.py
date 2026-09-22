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

The files swept are *derived* rather than listed. An allow-list is a list somebody has to
keep complete, and the point of this module is that the next split leaves no residue: a
list would have covered the two files this issue fixed and missed the pointer in
``src/issuebot/invocation.py``, which is the same shape in a module docstring. So the
sweep walks the tree, takes every text file it understands, and names its exclusions
instead, each for a reason:

* ``docs/superpowers/`` holds dated design records, and a pointer in one is a statement
  about where the content was when the document was written, not a claim about today.
* ``tests/`` is where a deliberately stale pointer belongs -- this module's own truth
  table quotes ``(README, "Rotating the database password")`` to prove the regex sees it.
"""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The text files a pointer can be written in. Everything else in the tree is data, an image
# or a lock file, where this shape is not prose and a match would be a coincidence.
POINTER_SUFFIXES = frozenset({".md", ".yaml", ".yml", ".py", ".toml", ".sql", ".cfg"})
POINTER_NAMES = frozenset({".env.example", "Dockerfile"})
# Directories with nothing of the repository's own in them.
NOT_THE_TREE = frozenset(
    {
        ".git",
        ".issuebot",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "htmlcov",
        "node_modules",
        "site-packages",
    }
)
# The two exclusions the module docstring explains, as paths relative to the root.
EXCLUDED = ("docs/superpowers", "tests")

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
# The leading indent and comment marker a line of prose may carry, in any of the files
# swept: ``#`` for YAML, env, Python and a Dockerfile, and the same character for a
# markdown heading, whose text is prose once the hashes are off.
MARKER = re.compile(r"^[ \t]*#*[ \t]*")


def _normalise(title: str) -> str:
    """A title as the other spelling of it would write it.

    A heading may carry code spans or emphasis that a pointer quoting it drops, and the
    wrapping a pointer survives leaves its own whitespace, so both sides are compared with
    the markup removed, the whitespace collapsed and the case folded.
    """
    return " ".join(title.replace("`", "").replace("*", "").replace("_", "").split()).casefold()


@functools.cache
def _headings(path: Path) -> frozenset[str]:
    """Every section title in a markdown file, normalised.

    Fenced blocks are skipped: ``docs/operations.md``'s rotation recipe is a shell script
    whose comments start with ``#``, and reading those as headings would have the sweep
    accept a title no reader can find. Cached, since one document answers for every
    pointer in the tree that names it.
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
    return frozenset(found)


def _runs(text: str) -> list[tuple[str, list[tuple[int, int]]]]:
    """The file's prose as unwrapped runs, each with the line every offset came from.

    A pointer is one phrase that was wrapped where the line ran out, so
    ``(docs/toolchains.md, "A`` / ``PowerShell server ...")`` is one pointer and not two
    fragments; joining the run is what lets a single regex see it. A run ends at a blank
    line, so no pointer is ever assembled across a gap, and each run carries the offset
    at which every source line starts, so a match is reported against the line it is
    written on rather than against the line the run began at.

    The marker is stripped rather than required, which is what lets one function serve a
    YAML comment, an env comment, a markdown paragraph and a Python docstring alike. What
    a line *is* does not matter here: a pointer is judged by the regex, and a line that
    holds none costs a join and nothing else.
    """
    runs: list[tuple[str, list[tuple[int, int]]]] = []
    current: list[tuple[int, str]] = []

    def flush() -> None:
        if not current:
            return
        parts: list[str] = []
        offsets: list[tuple[int, int]] = []
        index = 0
        for number, prose in current:
            offsets.append((index, number))
            parts.append(prose)
            index += len(prose) + 1
        runs.append((" ".join(parts), offsets))

    for number, line in enumerate(text.splitlines(), start=1):
        prose = line[MARKER.match(line).end() :].strip()  # type: ignore[union-attr]
        if not prose:
            flush()
            current = []
            continue
        current.append((number, prose))
    flush()
    return runs


def _line_of(offsets: list[tuple[int, int]], position: int) -> int:
    """The source line the character at ``position`` in a joined run was written on."""
    line = offsets[0][1]
    for start, number in offsets:
        if start > position:
            break
        line = number
    return line


def _resolve(relative: str, document: str) -> Path:
    """The file a pointer names, read from the referring file's own directory then the root.

    ``README`` without its extension is the one abbreviation the pointers use.
    """
    name = "README.md" if document == "README" else document
    beside = (ROOT / relative).parent / name
    return beside if beside.is_file() else ROOT / name


def _swept() -> list[str]:
    """Every file in the tree this rule binds, as paths relative to the root."""
    found: list[str] = []
    for directory, subdirectories, names in os.walk(ROOT):
        subdirectories[:] = sorted(d for d in subdirectories if d not in NOT_THE_TREE)
        here = Path(directory)
        for name in sorted(names):
            path = here / name
            if path.suffix not in POINTER_SUFFIXES and name not in POINTER_NAMES:
                continue
            relative = path.relative_to(ROOT).as_posix()
            if any(relative == e or relative.startswith(f"{e}/") for e in EXCLUDED):
                continue
            found.append(relative)
    return found


def _pointers(relative: str) -> list[tuple[int, str, str]]:
    """Every pointer in the file, as ``(line, document, title)``."""
    text = (ROOT / relative).read_text(encoding="utf-8")
    return [
        (_line_of(offsets, match.start()), match.group("document"), match.group("title"))
        for run, offsets in _runs(text)
        for match in POINTER.finditer(run)
    ]


def _stale(relative: str) -> list[str]:
    """Every pointer in the file that names no heading of the document it names."""
    complaints = []
    for number, document, title in _pointers(relative):
        target = _resolve(relative, document)
        if not target.is_file():
            complaints.append(f"{relative}:{number}: {document} is not a file in the tree")
        elif _normalise(title) not in _headings(target):
            complaints.append(f'{relative}:{number}: {document} has no section "{title}"')
    return complaints


# The files that carry a pointer today, so that no case of the sweep passes vacuously. The
# sweep itself is over the whole tree; this is only how the cases are named.
CARRIES_A_POINTER = sorted(relative for relative in _swept() if _pointers(relative))


@pytest.mark.parametrize("relative", CARRIES_A_POINTER)
def test_every_pointer_names_the_document_the_section_lives_in(relative: str) -> None:
    complaints = _stale(relative)
    assert complaints == [], "\n".join(complaints)


def test_the_sweep_reaches_every_file_in_the_tree_and_not_only_the_documentation() -> None:
    """The gap an allow-list left: a pointer of this shape in a module docstring.

    ``src/issuebot/invocation.py`` names a README section the way ``compose.yaml`` does,
    and a list of operator-facing documents would never have looked at it. Deriving the
    swept set is what makes "the next split cannot leave the same residue" a statement
    about the tree rather than about nine files somebody remembered.
    """
    swept = _swept()
    for expected in ("compose.yaml", ".env.example", "CLAUDE.md", "src/issuebot/invocation.py"):
        assert expected in swept, expected
    assert not any(relative.startswith("docs/superpowers/") for relative in swept)
    assert not any(relative.startswith("tests/") for relative in swept)


def test_the_sweep_reads_the_pointers_it_is_there_for() -> None:
    """The files this issue repointed carry pointers, so a regex that stopped matching fails.

    Every assertion above passes over a file with no pointers in it at all, which is
    exactly what a broken ``POINTER`` would produce. Seven and eight are what #210 moved
    out of the README; the counts are floors, not pins, so adding a pointer does not fail
    the suite.
    """
    for relative, expected in (("compose.yaml", 7), (".env.example", 8)):
        found = _pointers(relative)
        assert len(found) >= expected, f"{relative}: {found}"


def test_a_reference_that_quotes_no_section_is_not_a_pointer() -> None:
    """The four references #210 left alone name a step or a requirement, not a heading.

    They are the reason the rule is "a title in quotes beside a document" and not "the word
    README": the README does still carry the setting-up steps and the prerequisites, and a
    sweep that flagged those would be asking for them to be broken. Nothing here says a
    pointer *must* name a file under ``docs/`` -- one naming a README section that is still
    in the README is correct, and is checked like any other.
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
    """The form this issue had to fix by hand: a pointer split across two comment lines.

    The line reported is the one the pointer *starts* on, not the one the comment block
    started on, which is what an editor jumps to.
    """
    text = (
        "        # need a real server, and `docker compose build worker` to pick it up\n"
        "        # (docs/toolchains.md, \"A PostgreSQL server for the target repository's\n"
        '        # tests"). The web service builds from the same context without it.\n'
    )
    runs = _runs(text)
    assert len(runs) == 1
    run, offsets = runs[0]
    match = POINTER.search(run)
    assert match is not None
    assert match.group("title") == "A PostgreSQL server for the target repository's tests"
    assert _line_of(offsets, match.start()) == 2


def test_a_pointer_is_reported_against_its_own_line() -> None:
    """A run is many lines long, so the run's first line is the wrong thing to report."""
    text = '# one\n# two\n# three (docs/operations.md, "Blocked")\n'
    run, offsets = _runs(text)[0]
    match = POINTER.search(run)
    assert match is not None
    assert _line_of(offsets, match.start()) == 3


def test_a_run_ends_at_a_blank_line() -> None:
    """Two paragraphs are two runs, so no pointer is ever assembled across the gap."""
    assert len(_runs('# docs/operations.md,\n\n# "Blocked"\n')) == 2


def test_headings_skip_a_fenced_block() -> None:
    """``docs/operations.md``'s rotation recipe is a shell script full of ``#`` comments."""
    headings = _headings(ROOT / "docs" / "operations.md")
    assert "rotating the database password" in headings
    assert not any(heading.startswith("1. put the new value") for heading in headings)
