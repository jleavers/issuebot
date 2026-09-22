"""A cross-document pointer resolves against the document it names (#210, #218).

Prose in this repository has one home each, and the homes have moved (#196, #199). A pointer
between those homes goes stale silently, which is what makes it worth a test: the reader is
the only thing that fails, and only once they have gone looking.

Two shapes, checked here against one parse of the headings and one list of files.

**A quoted title beside a document's name** (#210), as
``(docs/operations.md, "Rotating the database password")``. The files an operator reads
outside the documentation -- ``compose.yaml`` and ``.env.example`` above all -- point this
way, and a split that moves the section and not the pointer leaves an operator opening a file
and searching for a heading that is not there, which is what #206 found in ``docs/`` and #210
found in those two files. Nothing here judges prose that names a document without quoting a
section -- "the README's step 2" is a reference to a numbered step, not to a heading -- so the
rule binds exactly the form that can go stale silently.

**A markdown link carrying an anchor** (#218), as ``[Safety](docs/operations.md#safety)``. An
anchor is GitHub's slug of a heading, so renaming that heading breaks the link with no sign
that anything is wrong: the link still renders, the click still loads the file, and the
browser lands at the top instead of at the section. ``README.md`` alone carries twenty-six.
Every such link is resolved against the tree, and every anchor against the slugged headings of
the file it lands in; a link with no anchor is checked for the path existing and nothing more,
since a link may name a directory or a file that is not markdown. A link with no *path* is
resolved against the document it is written in -- ``README.md``'s own table of contents is
built of eight of those, and a rename breaks them exactly as silently. A link inside a code
span is excluded, the way one inside a fenced block is: it is a sentence *about* a pointer.

``github_slug`` is somebody else's rule, ported rather than invented, so it is pinned by a
truth table of its own (``SLUG_CASES``, ``DUPLICATE_CASES``) instead of only by whatever
headings the tree happens to carry today. Where it is *wrong* it fails loudly rather than
quietly: an unhandled construct in a heading gives a slug no pointer matches, so the sweep
reports a pointer that is in fact fine. That is the safe direction for a guard whose subject
is other people's markdown.

The files swept are the repository's own, from ``git ls-files``, which is the idiom
``tests/test_compose_credentials.py`` and ``tests/test_web_vendor.py`` already use. That is
load-bearing rather than tidy: a walk of the directory sweeps whatever is *sitting* in it,
and this repository's own parallel-session workflow nests full checkouts under
``.claude/worktrees/`` (see ``.gitignore``), so a walk would judge another commit's files
as if they were this branch's -- and their ``docs/superpowers/``, being one directory
deeper, would miss the exclusion below and be complained about. The tracked list cannot see
a nested worktree, a ``build/``, a ``.venv`` or an operator's ``configs/*.local.md``,
because none of them is the repository's own file. The anchor sweep reads the markdown among
them (``_swept_markdown``), anchors meaning nothing in a file with no headings.

What is excluded, and why each is:

* ``docs/superpowers/`` holds dated design records, and a pointer in one is a statement
  about where the content was when the document was written, not a claim about today.
* this module itself, where a deliberately stale pointer belongs -- its truth tables quote
  ``(README, "Rotating the database password")`` and write links at documents that do not
  exist, to prove the two regexes see them. The file and not ``tests/``: no other test
  carries a pointer, and their docstrings are as prose-heavy as ``invocation.py``'s, so
  excluding the directory would blank seventy-six files to protect one.

Everything else tracked is read, minus the suffixes that are not prose at all, so the rule
is "the repository, less what is named" rather than a list of documents to keep complete.
That includes bytes nobody here wrote and nobody here may edit -- the vendored dashboard
libraries, the recorded ``tests/fixtures/`` payloads and turn files -- swept because none
of them carries either shape, and not because an artefact could be edited to satisfy the
rule. If one ever does, the answer is another name in ``EXCLUDED``, never a changed fixture.
Keeping such a list was the first draft's mistake: it named nine operator-facing files and
so never looked at ``src/issuebot/invocation.py``, which carries a pointer of exactly the
quoted shape in its module docstring.
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Not prose: binary assets and lock files, where this shape could only match by accident.
# Compared case-folded, since a suffix is the author's to capitalise.
NOT_PROSE = frozenset(
    {
        ".gif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".jsonl",
        ".lock",
        ".otf",
        ".pdf",
        ".png",
        ".svg",
        ".ttf",
        ".webp",
        ".whl",
        ".woff",
        ".woff2",
        ".zip",
    }
)
# The two exclusions the module docstring explains, as paths relative to the root.
EXCLUDED = ("docs/superpowers", "tests/test_doc_pointers.py")

# ``<document>, "<section title>"``: the document named, then the title in quotes beside it.
# The backtick is optional because a markdown file spells the name as code
# (``[`docs/operations.md`, "Rotating the database password"](...)``) while a YAML or env
# comment spells it bare. ``README`` is admitted without its extension, which is how every
# one of these pointers has always written it.
POINTER = re.compile(
    r"(?P<document>README(?:\.md)?|[\w.-]*(?:/[\w.-]+)*\.md)`?,\s+\"(?P<title>[^\"\n]+)\""
)
# The leading indent and comment marker a line of prose may carry, in any of the files
# swept: ``#`` for YAML, env, Python and a Dockerfile, and the same character for a
# markdown heading, whose text is prose once the hashes are off. It matches the empty
# string, so every line has a start.
MARKER = re.compile(r"^[ \t]*#*[ \t]*")


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

# An inline code span: backticks, and what they wrap. Literal text, so a link written inside
# one is text rather than a pointer and is dropped before `_links` looks.
#
# ``[\s\S]`` rather than ``.``, because a code span wraps: this repository's prose opens one at
# the end of a line and closes it on the next constantly, and pairing line by line would take a
# stray closing backtick for an *opening* one and blank from there to the next backtick on that
# line -- swallowing any link between them, silently, which is this module's own failure mode.
# The pairing is bounded to a paragraph by `_blank_code_spans` rather than by the pattern, a
# code span being unable to hold a blank line.
_CODE_SPAN = re.compile(r"(`+)([\s\S]+?)\1")

# What a heading's *text content* is, which is what GitHub slugs -- the heading is rendered to
# HTML first, so the markup around the words is not in the anchor. An image contributes no text
# at all (its alt lands in an attribute) and a link contributes its text. Images before links,
# since ``![alt](src)`` contains ``[alt](src)``.
#
# Code spans and emphasis need no pass of their own: a backtick and an asterisk are dropped by
# the slug rule below as punctuation, and what they wrap is kept, which is the whole of why
# `` `.issuebot/env` `` becomes ``issuebotenv``. Underscore emphasis could not have one
# anyway -- ``_`` is a slug character, so ``snake_case`` survives into the anchor whole and
# stripping it would corrupt every identifier a heading names.
#
# Two constructs are deliberately not handled: raw HTML in a heading (``<br>`` slugs as ``br``
# where GitHub drops it) and a link written inside a code span (`` `[a](b)` ``, which GitHub
# renders as the literal ``[a](b)``). Neither appears in this tree, and both fail in the safe
# direction -- a slug no pointer matches is a reported pointer that is in fact fine.
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_INLINE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")

# GitHub's slug rule, the half that removes: everything that is not a word character, a hyphen
# or a space goes, and what is left keeps its underscores. ``\w`` is Unicode here, so an
# accented letter survives as itself, which is what GitHub does too.
_NOT_SLUG = re.compile(r"[^\w\- ]", re.UNICODE)


@functools.cache
def _tracked_files() -> tuple[str, ...]:
    """Every file the repository tracks, the idiom ``test_compose_credentials.py`` uses.

    De-duplicated, because an unmerged index lists a conflicted path once per stage and
    every complaint in it would otherwise be made three times. The skip carries git's own
    stderr: exit 128 is most often ``detected dubious ownership``, on a repository
    bind-mounted from another uid, and "returned non-zero exit status 128" names nothing.
    """
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as error:  # pragma: no cover - needs no git
        detail = getattr(error, "stderr", "") or ""
        pytest.skip(
            f"not a git checkout, or no git: {error}{': ' + detail.strip() if detail else ''}"
        )
    return tuple(dict.fromkeys(name for name in listed.stdout.split("\0") if name))


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


@functools.cache
def _text(path: Path) -> str | None:
    """The file's characters, or ``None`` for a tracked name this checkout cannot read.

    Undecodable bytes are replaced rather than raised on, and an unreadable name costs one
    complaint rather than the suite: a documentation guard that aborted collection would
    take the database and orchestrator tests down with it, and a pointer is ASCII either
    way -- U+FFFD is not a word character, so a replacement can neither make a document
    name nor extend one. ``utf-8-sig`` because a byte order mark is not whitespace, so a
    file carrying one would hide its own first heading from ``_ATX``.
    """
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


@functools.cache
def _headings_of(path: Path) -> tuple[str, ...]:
    """A markdown file's headings, in order, as their source text.

    The one parse both shapes rest on, and cached because one document answers for every
    pointer in the tree that names it. ``_section_titles`` normalises these for the quoted
    shape and ``_anchors_of`` slugs them for the anchor shape; the order matters to the
    second, a repeated heading taking its suffix from how many came before it.
    """
    return tuple(_headings(_text(path) or ""))


def _normalise(title: str) -> str:
    """A title as the other spelling of it would write it.

    A heading may carry code spans or emphasis that a pointer quoting it drops, and the
    wrapping a pointer survives leaves its own whitespace, so both sides are compared with
    the markup removed, the whitespace collapsed and the case folded.
    """
    return " ".join(title.replace("`", "").replace("*", "").replace("_", "").split()).casefold()


@functools.cache
def _section_titles(path: Path) -> frozenset[str]:
    """Every section title in a markdown file, normalised, for the quoted shape."""
    return frozenset(_normalise(heading) for heading in _headings_of(path))


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
    still has to find, so one inside a fence is judged like any other -- in the other tests
    now swept as much as in the documentation. The one place a stale example belongs is this
    module, which is excluded by name.
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
    """Every file in the repository these rules bind, as paths relative to the root."""
    return [
        relative
        for relative in _tracked_files()
        if Path(relative).suffix.lower() not in NOT_PROSE
        and not any(relative == e or relative.startswith(f"{e}/") for e in EXCLUDED)
    ]


def _swept_markdown() -> list[str]:
    """The markdown among them, which is where a link and an anchor can live.

    The same list, narrowed rather than declared again: an anchor is a heading's slug and a
    file with no headings has none to offer, so the quoted shape's sweep of every prose file
    is the right breadth for it and the wrong one here. A document added tomorrow is swept
    the day it is tracked either way.
    """
    return [relative for relative in _swept() if Path(relative).suffix.lower() == ".md"]


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
    """Every pointer in the file that names no heading of the document it names.

    A file this checkout cannot read is one complaint of its own rather than silence: its
    pointers are unchecked either way, and a sweep that says nothing about them is a sweep
    whose green is worth less than it looks.
    """
    if _text(ROOT / relative) is None:
        return [f"{relative}: unreadable, so its pointers were not checked"]
    complaints = []
    for number, document, title in _pointers(relative):
        target = _resolve(relative, document)
        if not target.is_file():
            complaints.append(f"{relative}:{number}: {document} is not a file in the tree")
        elif _normalise(title) not in _section_titles(target):
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
        assert excluded not in swept, excluded
        assert not any(relative.startswith(f"{excluded}/") for relative in swept), excluded


def test_the_sweep_reaches_past_the_documentation() -> None:
    """The gap the first draft's allow-list left: this shape in a module docstring.

    ``src/issuebot/invocation.py`` names a README section the way ``compose.yaml`` does,
    and a list of operator-facing documents would never have looked at it. Sweeping what is
    tracked is what makes "the next split cannot leave the same residue" a statement about
    the repository rather than about the files somebody remembered.
    """
    swept = set(_swept())
    expected = (
        "compose.yaml",
        ".env.example",
        "CLAUDE.md",
        "src/issuebot/invocation.py",
        # The two the exclusions used to cover wholesale, for one file and for authorship:
        # every other test is swept, and so are the bytes this repository only carries.
        "tests/conftest.py",
        "src/issuebot/web/static/vendor/htmx.min.js",
    )
    for relative in expected:
        assert relative in swept, relative
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


def test_the_quoted_sweep_reports_a_pointer_that_does_not_land(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_stale`'s two verdicts, which nothing exercised: the document is not in the tree, and
    the document is there but carries no such section. Both branches survived being replaced by
    ``False`` with the suite green, on this branch and on `main` before it -- the same vacuity
    `test_the_sweep_reports_a_pointer_that_does_not_land` guards for the anchor shape, and the
    merged module is where the older half can have it too."""
    document = (
        "# One\n\n"
        'See (docs/operations.md, "Rotating the database password") and\n'
        '(docs/operations.md, "A section nobody wrote") and\n'
        '(docs/nowhere.md, "Anything at all").\n'
    )
    # One file's text and no other's: the target document has to stay the real
    # `docs/operations.md`, since the first pointer landing is half of what this proves.
    real = _text
    monkeypatch.setattr(
        sys.modules[__name__],
        "_text",
        lambda path: document if path == ROOT / "README.md" else real(path),
    )
    _pointers.cache_clear()
    try:
        assert _stale("README.md") == [
            'README.md:4: docs/operations.md has no section "A section nobody wrote"',
            "README.md:5: docs/nowhere.md is not a file in the tree",
        ]
    finally:
        _pointers.cache_clear()


def test_an_unreadable_file_costs_one_complaint_and_not_the_suite(tmp_path: Path) -> None:
    """Every file is read, so one bad byte must not be able to abort collection."""
    undecodable = tmp_path / "undecodable.md"
    undecodable.write_bytes(b'x (README, "Prerequisites") \xff\xfe y')
    assert "Prerequisites" in (_text(undecodable) or "")
    assert _text(tmp_path / "absent.md") is None
    assert _stale("no/such/file.md") == [
        "no/such/file.md: unreadable, so its pointers were not checked"
    ]
    # Both shapes, and the same answer: reading an unreadable file as an empty document would
    # have the anchor sweep report nothing about it, which is the one thing it must not do.
    assert _unresolved_anchors("no/such/file.md") == [
        "no/such/file.md: unreadable, so its links were not checked"
    ]


def test_a_byte_order_mark_does_not_hide_the_first_heading(tmp_path: Path) -> None:
    """U+FEFF is not whitespace, so ``_ATX`` would miss a BOM'd file's own first section."""
    document = tmp_path / "bom.md"
    document.write_bytes(b"\xef\xbb\xbf# First Section\n\ntext\n")
    assert "first section" in _section_titles(document)


def test_a_conflicted_path_is_listed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unmerged index lists a path once per stage.

    ``git ls-files`` emits the three stages of a conflicted file, so a merge in progress
    would have every complaint in it made three times. A clean index cannot show that, so
    the listing is driven directly here.
    """

    def listing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        stages = "a.md\0conflicted.md\0conflicted.md\0conflicted.md\0b.md\0"
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stages, stderr="")

    _tracked_files.cache_clear()
    monkeypatch.setattr(subprocess, "run", listing)
    try:
        assert _tracked_files() == ("a.md", "conflicted.md", "b.md")
    finally:
        _tracked_files.cache_clear()


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


def test_section_titles_skip_a_fenced_block() -> None:
    """``docs/operations.md``'s rotation recipe is a shell script full of ``#`` comments."""
    headings = _section_titles(ROOT / "docs" / "operations.md")
    assert "rotating the database password" in headings
    assert not any(heading.startswith("1. put the new value") for heading in headings)


# ---------------------------------------------------------------------------
# The anchor shape (#218): a markdown link carrying GitHub's slug of a heading.
# ---------------------------------------------------------------------------


def _heading_text(heading: str) -> str:
    """A heading's rendered text content: the words, without the markup around them."""
    return _INLINE_LINK.sub(r"\1", _IMAGE.sub("", heading))


def github_slug(heading: str) -> str:
    """GitHub's anchor for a heading, before any suffix for a repeated one.

    Lower-case the text content, drop everything that is not a word character, a hyphen or a
    space, then turn each remaining space into a hyphen -- in that order, so two spaces give
    two hyphens and a dropped comma does not join the words either side of it.

    The trim is of the *source*, which `_ATX` has already done for a real heading, and not of
    the text content: GitHub slugs what the rendered ``<h2>`` contains, and an image contributes
    no text while the space beside it survives. So a heading opening with one -- a badge, say --
    has an anchor opening with a hyphen, as a heading opening with an emoji does.
    """
    return _NOT_SLUG.sub("", _heading_text(heading.strip()).lower()).replace(" ", "-")


def _anchors(headings: list[str]) -> list[str]:
    """The anchors GitHub gives a document's headings, repeats included.

    A heading whose slug is already taken gets ``-1``, then ``-2``, and so on -- and the
    candidate is re-checked each time round, so a document carrying both ``Safety`` twice and a
    literal ``Safety 1`` hands out three distinct anchors rather than two and a collision. That
    re-check is `github-slugger`'s own loop, ported from it; the repeat itself is the part every
    renderer of GitHub markdown agrees on, and no document in this tree has one.
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


@functools.cache
def _anchors_of(path: Path) -> tuple[str, ...]:
    """Every anchor `path` offers. Cached: `docs/operations.md` is pointed at a dozen times."""
    return tuple(_anchors(list(_headings_of(path))))


def _blank_code_spans(body: list[tuple[int, str]]) -> list[str]:
    """`body`'s lines with every code span replaced by spaces, one character for one.

    Paragraph by paragraph, because a code span is bounded by the block that holds it. That is
    what pairs a span across the line break it wraps at -- the house style here -- and equally
    what keeps a single unpaired backtick, prose *about* backticks as several lines of this
    module are, from blanking the rest of a document.

    A paragraph ends at a blank line and also wherever `body`'s line numbers stop being
    consecutive, which is where `_body_lines` took a fenced block out: a fence is a block
    boundary as much as a blank line is, and without the second rule a stray backtick before
    a code block would pair with one after it, across everything between.

    The replacement keeps each line's length and the line breaks inside a span, so a line
    number is still a line number afterwards.
    """
    blanked: list[str] = []
    paragraph: list[str] = []
    previous: int | None = None

    def flush() -> None:
        if paragraph:
            joined = "\n".join(paragraph)
            blanked.extend(
                _CODE_SPAN.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), joined).split("\n")
            )
            paragraph.clear()

    for number, line in body:
        if previous is not None and number != previous + 1:
            flush()
        previous = number
        if line.strip():
            paragraph.append(line)
        else:
            flush()
            blanked.append(line)
    flush()
    return blanked


def _links(text: str) -> list[tuple[int, str]]:
    """Every inline link target in the prose, with the line it sits on.

    Code spans go the way fenced blocks do, and for the same reason: a link written inside one
    is a sentence *about* a pointer rather than a pointer, and GitHub renders it as the literal
    characters. This repository's own prose contains that sentence.
    """
    body = _body_lines(text)
    return [
        (number, match.group(1))
        # `strict`: `_blank_code_spans` preserves the line breaks inside a span, so losing one
        # would shift every line number after it. Better an error than a wrong citation.
        for (number, _), line in zip(body, _blank_code_spans(body), strict=True)
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
        # ``[x](<path>)`` is CommonMark's destination-in-angle-brackets, which resolves like
        # any other; without this it would be reported as naming no such path, which is loud
        # but wrong. The spelling that exists *because* of the brackets -- a destination with
        # a space in it -- carries whitespace, so `_LINK` never matches it and the
        # completeness guard below reports it as a shape the sweep does not parse.
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        path, _, anchor = target.partition("#")
        # Both halves may be percent-encoded: a space in a file name, and the form GitHub puts
        # in the address bar for an anchor whose heading is not ASCII.
        path, anchor = unquote(path), unquote(anchor)
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
        if not resolved.is_file() or resolved.suffix.lower() != ".md":
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


def _unresolved_anchors(relative: str) -> list[str]:
    """Every link in a swept markdown file that does not land, the file itself included.

    A file this checkout cannot read is one complaint of its own rather than silence, the rule
    `_stale` already has for the other shape: its links are unchecked either way, and a sweep
    that says nothing about them is a sweep whose green is worth less than it looks. Reading it
    as an empty document would have said nothing -- no links, and so no complaints.
    """
    text = _text(ROOT / relative)
    if text is None:
        return [f"{relative}: unreadable, so its links were not checked"]
    return _pointer_complaints(relative, text)


def test_every_anchor_resolves_against_the_headings_it_names() -> None:
    """Every markdown link in a swept file lands: the path exists, and where the link carries
    an anchor, some heading of the file it names slugs to it."""
    complaints = [
        complaint for relative in _swept_markdown() for complaint in _unresolved_anchors(relative)
    ]
    assert complaints == [], "cross-document pointers no longer resolve:\n" + "\n".join(complaints)


# The documents the anchor sweep exists for, named so that narrowing `_swept_markdown` cannot
# quietly drop one. The floors below catch a collapse and not a subtraction: dropping
# `CLAUDE.md` and `SECURITY.md` takes six anchored pointers out of the sweep and leaves both
# of them well above their thresholds, so "it still sees plenty" is not the same claim as "it
# still sees this file".
ANCHORED_DOCUMENTS = (
    "README.md",
    "CLAUDE.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "configs/WORKFLOW.md",
    "docs/operations.md",
    "docs/security-model.md",
    "docs/toolchains.md",
    "docs/dashboard.md",
    "docs/BLUEPRINT.md",
    "docs/package-layout.md",
    "tools/screenshots/README.md",
    "src/issuebot/web/static/vendor/README.md",
)


def test_the_anchor_sweep_reads_the_documents_it_is_there_for() -> None:
    """A list to keep complete, deliberately, and the only one left in this module: every other
    file is swept because the repository tracks it. These are the documents whose pointers this
    issue was filed about, so a narrowing of `_swept_markdown` that still passed the floors
    would otherwise take one out in silence."""
    swept = set(_swept_markdown())
    missing = [relative for relative in ANCHORED_DOCUMENTS if relative not in swept]
    assert missing == [], f"the anchor sweep no longer reads {missing}"


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
    # An image contributes no text, but the space beside it does, so the anchor opens with a
    # hyphen -- which is also what a heading opening with an emoji gets, the character being
    # dropped where the space it left is not.
    ("![shield](docs/images/x.png) Licence", "-licence"),
    ("🚀 Getting started", "-getting-started"),
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


def test_links_pairs_a_code_span_across_the_break_it_wraps_at() -> None:
    """The property `_blank_code_spans` exists for. Paired line by line, the stray closing
    backtick opening line 2 would pair with the opening backtick of ``more`` and blank the link
    between them -- dropping a live pointer from the sweep with nothing to say so."""
    document = "A wrapped `code\nspan` and [link](docs/operations.md) with `more` here.\n"
    assert _links(document) == [(2, "docs/operations.md")]


def test_links_does_not_pair_a_code_span_past_its_own_block() -> None:
    """And no further than that, which is the other half: an unpaired backtick is prose about a
    backtick, and must not reach past the blank line or the fenced block after it. Both bounds,
    since `_body_lines` has already taken the fence out and left the lines either side of it
    adjacent."""
    across_a_blank_line = "Prose about a lone ` backtick.\n\n[a link](docs/operations.md) `x`\n"
    assert _links(across_a_blank_line) == [(3, "docs/operations.md")]
    across_a_fence = (
        "Prose about a lone ` backtick.\n"
        "```bash\n"
        "echo hello\n"
        "```\n"
        "[a link](docs/operations.md) and `a span`\n"
    )
    assert _links(across_a_fence) == [(5, "docs/operations.md")]


def test_links_skips_inline_code_spans() -> None:
    """A link inside a code span is a sentence about a pointer, not a pointer -- and this
    repository's prose contains that sentence, `CLAUDE.md` naming the very anchor below to
    explain the drift this module exists to catch."""
    document = "`[an example](docs/nowhere.md#gone)` beside [a pointer](docs/operations.md)\n"
    assert _links(document) == [(1, "docs/operations.md")]


# Today's tree, so a parser regression cannot make the sweep pass by seeing nothing. The floor
# is well under the real figure (99 links, 83 of them inside the repository, 49 carrying an
# anchor) because the prose is edited constantly; what it catches is an order-of-magnitude
# collapse -- an unbalanced fence swallowing the tail of a document, a tightened `_LINK` -- not
# a paragraph rewritten.
MINIMUM_LINKS = 60
MINIMUM_ANCHORED = 35

# Every link this repository's prose writes inside a code span, and so quotes rather than
# points with. Declared rather than counted, because "a link the sweep does not check" is the
# one thing it cannot be allowed to acquire silently: a new one has to be written down here,
# where the next reader can ask whether it was meant.
CODE_QUOTED_POINTERS: tuple[tuple[str, str], ...] = (
    # The paragraph explaining this very drift has to spell a pointer to explain it.
    ("CLAUDE.md", "`[Safety](docs/operations.md#safety)`"),
)

# A reference-style link and its definition. Neither contains ``](`` at all, so neither is
# caught by counting; both would be swept by nothing at all if the prose grew one.
_REFERENCE_LINK = re.compile(r"\]\[")
_REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s")


def test_the_anchor_sweep_still_sees_the_trees_links() -> None:
    """The guard against a vacuous sweep. Most swept markdown carries no link at all, so every
    complaint list above is empty for a document the parser has stopped reading, and the one
    failure this module exists to catch is a silent one."""
    targets = [
        target
        for relative in _swept_markdown()
        for _, target in _links(_text(ROOT / relative) or "")
        if not _EXTERNAL.match(target)
    ]
    anchored = [target for target in targets if "#" in target]
    assert len(targets) >= MINIMUM_LINKS, f"the sweep now sees only {len(targets)} links"
    assert len(anchored) >= MINIMUM_ANCHORED, f"only {len(anchored)} of them carry an anchor"


def _unparsed_links(name: str, text: str, quoted: int) -> list[str]:
    """Every link in `text` the sweep does not parse, and so does not check at all.

    `_LINK` reads one link shape: an inline destination, no title. A link written any other way
    is not *reported* by the sweep, it is invisible to it -- so what is pinned is the count
    rather than the syntax, and the raw line is what it is counted against.

    Raw, not blanked, because the blanking is itself something that can go wrong: a code span
    mispaired across a line break would take a live link out of both sides of a comparison made
    on blanked text, and the check would pass by having stopped looking. Blanking can only
    replace characters with spaces, so it can never *create* a ``](``; the resolved count is
    therefore bounded by the written one however exotic the mispairing, and any link the
    blanking eats shows up here. `quoted` is how many of the written ones this document means
    as quotations rather than as pointers.

    A destination carrying a title and a wrapped one both fail on the count. A reference link
    and its definition carry no ``](`` at all, so counting could never see one and they are
    matched for separately.
    """
    body = _body_lines(text)
    unparsed: list[str] = []
    parsed = 0
    for (number, raw), bare in zip(body, _blank_code_spans(body), strict=True):
        parsed += len(_LINK.findall(bare))
        if _REFERENCE_LINK.search(bare) or _REFERENCE_DEFINITION.match(bare):
            unparsed.append(f"{name}:{number}: reference-style link: {raw.strip()[:80]}")
    written = sum(raw.count("](") for _, raw in body)
    if written != parsed + quoted:
        unparsed.append(
            f"{name}: the prose writes {written} inline links and the sweep resolved "
            f"{parsed}, with {quoted} declared in CODE_QUOTED_POINTERS"
        )
    return unparsed


def _quoted_in(name: str) -> int:
    """How many links `name` is declared to write inside a code span rather than point with."""
    return sum(1 for file, _ in CODE_QUOTED_POINTERS if file == name)


def test_every_link_in_the_prose_is_a_link_the_sweep_parses() -> None:
    """Over the tree: every ``](`` a swept document writes is either a target the sweep
    resolved or one of the links this repository quotes on purpose."""
    unparsed = [
        complaint
        for relative in _swept_markdown()
        for complaint in _unparsed_links(
            relative, _text(ROOT / relative) or "", _quoted_in(relative)
        )
    ]
    assert unparsed == [], (
        "a markdown link the sweep does not parse, so its pointer is unchecked:\n"
        + "\n".join(unparsed)
    )


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ('[titled](docs/operations.md "A title")', "the prose writes 1 inline links"),
        ("[wrapped](docs/operations.md\n#safety)", "the prose writes 1 inline links"),
        ("[a reference][ref]", "reference-style link"),
        ("[ref]: docs/operations.md#safety", "reference-style link"),
        ("`[quoted](docs/operations.md)` with none declared", "the prose writes 1 inline links"),
    ],
)
def test_the_completeness_guard_reports_a_link_it_cannot_parse(
    document: str, expected: str
) -> None:
    """The negative proof for the guard, which over the tree only ever passes. Each of these is
    a live pointer the sweep would otherwise not check at all -- silently, which is the one
    thing a guard against silence must not do."""
    complaints = _unparsed_links("README.md", document + "\n", 0)
    assert complaints, f"{document!r} went unreported"
    assert expected in complaints[0], complaints


@pytest.mark.parametrize(
    ("target", "lands"),
    [
        # CommonMark's destination in angle brackets, which resolves like any other. Without
        # the unwrap it is reported as naming no such path -- loud, but wrong.
        ("[x](<docs/operations.md#safety>)", True),
        ("[x](<docs/operations.md#no-such-heading>)", False),
        # Both halves of a target may be percent-encoded: a space in a file name, and the form
        # GitHub puts in the address bar for an anchor whose heading is not ASCII. The decode
        # runs after the split, so a `%23` in a name is not mistaken for the separator.
        ("[x](docs/operations.md#rotating%2Dthe%2Ddatabase%2Dpassword)", True),
        ("[x](docs%2Foperations.md#safety)", True),
    ],
)
def test_a_target_is_unwrapped_and_percent_decoded(target: str, lands: bool) -> None:
    complaints = _pointer_complaints("README.md", target + "\n")
    assert (complaints == []) is lands, complaints


def test_headings_close_a_fence_only_on_its_own_terms() -> None:
    """`_FENCE`'s two rules, which `configs/WORKFLOW.md`'s workpad template needs and which
    nothing else exercised: a closing fence uses the same character as its opener and carries
    no info string. Without either, the block below closes early and the lines inside it are
    read as headings."""
    document = "````md\n### inside\n~~~\n### still inside\n````info\n### also inside\n````\n# Out\n"
    assert _headings(document) == ["Out"]


def test_the_completeness_guard_passes_an_ordinary_document() -> None:
    """And the other way, so the test above is not passing on everything."""
    assert not _unparsed_links("README.md", "[a pointer](docs/operations.md#safety)\n", 0)
    assert not _unparsed_links("README.md", "`[quoted](docs/operations.md)`\n", 1)


def test_the_code_quoted_pointers_are_still_written_that_way() -> None:
    """The other half of `CODE_QUOTED_POINTERS`: an allowance nothing checks is an allowance
    that outlives what it was for, and the count above would then hide a real link.

    Matched against the prose rather than the raw file, so an example moved into a fenced block
    -- where the sweep would not have read it as a link in the first place -- is reported here,
    naming the allowance, rather than only as arithmetic that has stopped adding up."""
    missing = [
        f"{name}: its prose no longer carries {quoted}"
        for name, quoted in CODE_QUOTED_POINTERS
        if quoted
        not in "\n".join(line for _, line in _body_lines((ROOT / name).read_text(encoding="utf-8")))
    ]
    assert not missing, "CODE_QUOTED_POINTERS is stale:\n" + "\n".join(missing)


def test_the_sweep_reports_a_pointer_that_does_not_land() -> None:
    """The negative proof: a document of this test's own, carrying one pointer of each kind
    that fails and one that still lands. Without this the sweep could be vacuous -- every
    pointer in the tree resolves today, so a check that reported nothing whatever it was given
    would pass the file above just as well.

    The one that lands names a real heading, which is what makes it a proof rather than a
    tautology; renaming `docs/operations.md`'s "Safety" therefore fails this test alongside
    the two files that point at it."""
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
