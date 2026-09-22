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

The files swept are the repository's own, from ``git ls-files``, which is the idiom
``tests/test_compose_credentials.py`` and ``tests/test_web_vendor.py`` already use. That is
load-bearing rather than tidy: a walk of the directory sweeps whatever is *sitting* in it,
and this repository's own parallel-session workflow nests full checkouts under
``.claude/worktrees/`` (see ``.gitignore``), so a walk would judge another commit's files
as if they were this branch's -- and their ``docs/superpowers/``, being one directory
deeper, would miss the exclusion below and be complained about. The tracked list cannot see
a nested worktree, a ``build/``, a ``.venv`` or an operator's ``configs/*.local.md``,
because none of them is the repository's own file.

What is excluded, and why each is:

* ``docs/superpowers/`` holds dated design records, and a pointer in one is a statement
  about where the content was when the document was written, not a claim about today.
* ``tests/`` is where a deliberately stale pointer belongs -- this module's own truth
  table quotes ``(README, "Rotating the database password")`` to prove the regex sees it.
* the vendored dashboard material is somebody else's bytes, kept here byte for byte.

Everything else tracked is read, minus the suffixes that are not prose at all, so the rule
is "the repository, less what is named" rather than a list of documents to keep complete.
Keeping such a list was the first draft's mistake: it named nine operator-facing files and
so never looked at ``src/issuebot/invocation.py``, which carries a pointer of exactly this
shape in its module docstring.
"""

from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Not prose: binary assets and lock files, where this shape could only match by accident.
NOT_PROSE = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2", ".lock", ".jsonl"}
)
# The three exclusions the module docstring explains, as paths relative to the root.
EXCLUDED = ("docs/superpowers", "tests", "src/issuebot/web/static/vendor")

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
# markdown heading, whose text is prose once the hashes are off. It matches the empty
# string, so every line has a start.
MARKER = re.compile(r"^[ \t]*#*[ \t]*")


def _tracked_files() -> list[str]:
    """Every file the repository tracks, the idiom ``test_compose_credentials.py`` uses."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as error:  # pragma: no cover - needs no git
        pytest.skip(f"not a git checkout, or no git: {error}")
    return [name for name in listed.stdout.split("\0") if name]


def _normalise(title: str) -> str:
    """A title as the other spelling of it would write it.

    A heading may carry code spans or emphasis that a pointer quoting it drops, and the
    wrapping a pointer survives leaves its own whitespace, so both sides are compared with
    the markup removed, the whitespace collapsed and the case folded.
    """
    return " ".join(title.replace("`", "").replace("*", "").replace("_", "").split()).casefold()


def _text(path: Path) -> str | None:
    """The file's characters, or ``None`` for a tracked name this checkout cannot read.

    Undecodable bytes are replaced rather than raised on, and an unreadable name costs its
    own file and nothing else: a documentation guard that aborted collection would take
    the whole suite down with it, and a pointer is ASCII either way.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


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
    for line in (_text(path) or "").splitlines():
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
    holds none costs a join and nothing else. A fenced block is prose to this function,
    unlike to ``_headings``, and deliberately: an example pointer names a section a reader
    still has to find, and the one place a stale one belongs is ``tests/``, excluded above.
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
        prose = line[MARKER.match(line).end() :].strip()
        if not prose:
            flush()
            current.clear()
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
    """Every file in the repository this rule binds, as paths relative to the root."""
    return [
        relative
        for relative in _tracked_files()
        if Path(relative).suffix not in NOT_PROSE
        and not any(relative == e or relative.startswith(f"{e}/") for e in EXCLUDED)
    ]


@functools.cache
def _pointers(relative: str) -> tuple[tuple[int, str, str], ...]:
    """Every pointer in the file, as ``(line, document, title)``."""
    text = _text(ROOT / relative)
    if text is None:
        return ()
    return tuple(
        (_line_of(offsets, match.start()), match.group("document"), match.group("title"))
        for run, offsets in _runs(text)
        for match in POINTER.finditer(run)
    )


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


def test_every_pointer_names_the_document_the_section_lives_in() -> None:
    """The rule itself, over the whole repository rather than over a list of documents."""
    complaints = [complaint for relative in _swept() for complaint in _stale(relative)]
    assert complaints == [], "\n".join(complaints)


def test_the_sweep_reads_the_repository_and_not_the_directory_it_sits_in() -> None:
    """A nested checkout is the failure this guards: `.claude/worktrees/` holds full clones.

    Every swept path is one the repository tracks, so a worktree, a ``build/``, a ``.venv``
    or an operator's ``configs/*.local.md`` is invisible however deep it sits -- which also
    keeps the ``docs/superpowers/`` exclusion true, since a nested checkout's copy of it is
    one directory further down and would match no root-anchored prefix.
    """
    swept = set(_swept())
    assert swept <= set(_tracked_files())
    assert not any(relative.startswith(".claude/worktrees/") for relative in swept)
    for excluded in EXCLUDED:
        assert not any(relative.startswith(f"{excluded}/") for relative in swept), excluded


def test_the_sweep_reaches_past_the_documentation() -> None:
    """The gap the first draft's allow-list left: this shape in a module docstring.

    ``src/issuebot/invocation.py`` names a README section the way ``compose.yaml`` does,
    and a list of operator-facing documents would never have looked at it. Sweeping what is
    tracked is what makes "the next split cannot leave the same residue" a statement about
    the repository rather than about the files somebody remembered.
    """
    swept = set(_swept())
    for expected in ("compose.yaml", ".env.example", "src/issuebot/invocation.py"):
        assert expected in swept, expected
    assert _pointers("src/issuebot/invocation.py"), "the pointer the allow-list missed"


def test_the_sweep_reads_the_pointers_it_is_there_for() -> None:
    """The files this issue repointed carry pointers, so a regex that stopped matching fails.

    The assertion above passes over a repository with no pointers in it at all, which is
    exactly what a broken ``POINTER`` would produce. Seven and eight are what #210 moved
    out of the README; the counts are floors, not pins, so adding a pointer does not fail
    the suite.
    """
    for relative, expected in (("compose.yaml", 7), (".env.example", 8)):
        found = _pointers(relative)
        assert len(found) >= expected, f"{relative}: {found}"


def test_an_unreadable_file_costs_its_own_file_and_not_the_suite(tmp_path: Path) -> None:
    """Every file is read, so one bad byte must not be able to abort collection."""
    undecodable = tmp_path / "undecodable.md"
    undecodable.write_bytes(b'x (README, "Prerequisites") \xff\xfe y')
    assert "Prerequisites" in (_text(undecodable) or "")
    assert _text(tmp_path / "absent.md") is None


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
