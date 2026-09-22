"""The instruction files this repository hands its own sessions fit in the prompt (#211).

issuebot reads ``CLAUDE.md`` and ``AGENTS.md`` out of a clone and carries them to the session
as enveloped data, cut at ``INSTRUCTION_FILE_LIMIT`` (128 KiB). A cut is deliberately not a
failure -- a target repository's instruction file is that repository's business, and refusing
to run because it is long would be issuebot dictating someone else's prose -- so nothing
anywhere raises, and the only party told is the session, in an envelope saying the text it is
reading stops early. The file renders whole on GitHub and in an editor. That is exactly how
this repository's own ``CLAUDE.md`` came to sit 12 KB over the cap without anyone noticing,
losing the whole of "The files beside this one" -- the doc map added so a session editing
prose picks the right file -- along with the operational rules and the PR convention.

So this repository buys itself the report its sessions cannot give it. The bound is checked
here, in CI, where a maintainer adding a paragraph meets it, and it is checked with
``HEADROOM`` to spare so the failure arrives *before* the cut rather than at it: at the cap a
test and a truncated prompt say the same thing on the same commit, which is a warning that
comes too late to be one.

This pins a *size*, not a structure, unlike ``tests/test_readme_bounds.py`` beside it. The
remedy when it fails is never to raise the cap -- that number bounds what a clone supplies to
a prompt for every deployment, and under ``agent.run_as`` the clone is the session's own to
write -- but to move reference detail into a file beside ``CLAUDE.md`` and leave a pointer,
which is what ``docs/package-layout.md`` is.

That file is bounded here too, at ``LAYOUT_BUDGET``, and for the other half of the same
lesson. No cap applies to it -- a session reads it from the working tree, so there is nothing
to truncate and nothing to be silent about -- but the growth that took ``CLAUDE.md`` over
moved into it with the prose, and leaving the destination of an overflow unmeasured is how
the overflow went unnoticed to begin with.
"""

from pathlib import Path

import pytest

from issuebot.agent.instructions import INSTRUCTION_FILE_LIMIT, REPOSITORY_INSTRUCTION_FILES

ROOT = Path(__file__).resolve().parents[1]

# Room for a while's ordinary growth, so the failure is a prompt to move something rather than
# a report that something has already been lost. Measured rather than guessed, over the eleven
# merges that touched `CLAUDE.md` before this change: 119,111 bytes to the 143,136 it overran
# at, a mean of ~2.2 KB a merged pull request. So 16 KiB is on the order of seven of them at
# the *old* rate -- and that rate was mostly the layout section, which has now moved out, so
# for the navigational half left here it should buy a good deal more. Long enough either way
# to be acted on in a normal piece of work, and short enough that the slack is not itself a
# budget somebody spends. If this starts failing every few pull requests, the rate has not
# fallen the way the split assumed, and the answer is another move, not a bigger number.
HEADROOM = 16 * 1024

# `docs/package-layout.md` is read from the working tree, not carried in a prompt, so
# `INSTRUCTION_FILE_LIMIT` does not apply to it and this number is not that one. It exists
# because the half that moved took the *growth* with it, and the whole lesson of #211 is that
# unwatched growth in a file nobody measures is exactly what goes unnoticed: the two
# `origin/main` merges on this change's own branch added ~16 KB to this prose in a day
# (121,097 -> 129,471 -> 136,911), replaying into it the layout edits #173, #190 and #225 made
# on main while the section was moving out. That pair is a burst rather than the rate -- it is
# what a split costs while it is in flight, and it stops once this lands -- but it is the
# honest figure to size against, and it leaves ~26 KB here: a dozen merges at the ~2.2 KB mean
# measured for `HEADROOM` below, three at the burst. The remedy when it fails is not to raise
# it either -- it is to give the file the structure it has so far done without, one document
# per module under `docs/package-layout/` behind an index.
LAYOUT_BUDGET = 160 * 1024
LAYOUT_DOC = "docs/package-layout.md"


def _size(name: str) -> int:
    """The file's size, or a failure that names the file rather than a `stat` traceback."""
    path = ROOT / name
    if not path.is_file():
        pytest.fail(f"{name} is not in the tree; this bound was written for it (#211)")
    return path.stat().st_size


@pytest.mark.parametrize("name", REPOSITORY_INSTRUCTION_FILES)
def test_instruction_file_is_carried_whole(name: str) -> None:
    """Every byte of it reaches a session: the cut never falls inside this repository's file."""
    size = _size(name)
    assert size <= INSTRUCTION_FILE_LIMIT, (
        f"{name} is {size} bytes, {size - INSTRUCTION_FILE_LIMIT} over the "
        f"{INSTRUCTION_FILE_LIMIT}-byte instruction cap: every session's copy is cut there and "
        "the end of the file reaches nobody. Move reference detail into a file beside it and "
        "leave a pointer (docs/package-layout.md), rather than raising the cap (#211)"
    )


@pytest.mark.parametrize("name", REPOSITORY_INSTRUCTION_FILES)
def test_instruction_file_keeps_headroom(name: str) -> None:
    """And with room left, so the warning lands before the first byte is lost."""
    size = _size(name)
    budget = INSTRUCTION_FILE_LIMIT - HEADROOM
    assert size <= budget, (
        f"{name} is {size} bytes, within {INSTRUCTION_FILE_LIMIT - size} of the "
        f"{INSTRUCTION_FILE_LIMIT}-byte instruction cap. Nothing is cut yet; this is the "
        "warning that it soon will be, and that a cut is silent when it comes. Move reference "
        "detail out into a file beside it and leave a pointer (#211)"
    )


def test_the_doc_map_reaches_a_session() -> None:
    """#211's acceptance criterion, stated as itself rather than inferred from a size.

    The doc map is the section a cut takes first, being near the end, and it is the one whose
    loss is self-concealing: a session that cannot read which file a change belongs in writes
    a second copy of a section that has already moved, and nothing says why.
    """
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    carried = text.encode("utf-8")[:INSTRUCTION_FILE_LIMIT].decode("utf-8", errors="ignore")
    start = text.index("## The files beside this one")
    doc_map = text[start:]
    assert doc_map in carried, (
        "the doc map, or part of it, falls past the instruction cap: a session is handed a "
        "CLAUDE.md that stops before it (#211)"
    )
    for heading in [line for line in text.splitlines() if line.startswith("## ")]:
        assert heading in carried, f"{heading!r} falls past the instruction cap (#211)"


def test_the_layout_pointer_names_a_file_that_is_there() -> None:
    """What keeps the split honest: the pointer is the only route to what was moved."""
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "docs/package-layout.md" in text, (
        "CLAUDE.md no longer points at docs/package-layout.md; the package layout moved out "
        "of it for the cap's sake (#211) and the pointer is how a session finds it"
    )
    assert (ROOT / "docs" / "package-layout.md").is_file()


def test_the_file_the_split_created_is_bounded_too() -> None:
    """The remedy for #211 must not become #211 (one level down) for want of being measured.

    Nothing cuts this file: a session reads it from the working tree, so there is no cap and
    no silent truncation to catch. What there is, is the growth that took `CLAUDE.md` over --
    it moved here with the prose, and here nothing was watching it at all, which is the half
    of #211 that was never about a number. So the file carries a budget of its own, checked
    in the same place and for the same reason, and the remedy when it fails is the one that
    worked: split it, one document per module under `docs/package-layout/` behind an index,
    rather than raise the budget.
    """
    size = _size(LAYOUT_DOC)
    assert size <= LAYOUT_BUDGET, (
        f"{LAYOUT_DOC} is {size} bytes, past its {LAYOUT_BUDGET}-byte budget. Nothing truncates "
        "it -- a session reads it from the tree -- but it is the file #211's overflow was moved "
        "into, and an unmeasured file is how that overflow went unnoticed in the first place. "
        "Split it per module under docs/package-layout/ behind an index, rather than raising "
        "this number (#211)"
    )
