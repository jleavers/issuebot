"""`docs/package-layout.md` can be read one module at a time (#221).

The file is the reference half of `CLAUDE.md`, split out by #211 because at 120 KB it took
that file past the instruction cap. `CLAUDE.md` now tells every session to read it from the
working tree -- but #211 moved the prose *verbatim*, which is what made the move checkable
line by line, and verbatim meant one flat `# Package layout` over a single bullet list of
fourteen entries. There was no way to read *part* of it: no headings, so no anchors, so the
pointer could name none, and a session that needed `issuebot.egress` had to open ~134 KB
(roughly 33k tokens) to reach 5 KB of it, or grep and hope the entry's continuation lines came
with it. The same failure #211 was about -- reference a session cannot reach -- one level
down, costing budget where #211 cost truncation.

So the structure is pinned here rather than left to hold by habit, and it is pinned as the
property itself: every module of the package has a section of its own, and the pointer names
the anchor form so a session knows the sections are there. This is the step before the split
`LAYOUT_BUDGET` prescribes when it fails -- one document per module under
`docs/package-layout/` behind an index -- and it is what makes that split mechanical when the
day comes.

Structure, not size: `tests/test_instruction_bounds.py` beside it holds the budget, and says
in as many words that it pins a size and not this.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYOUT = ROOT / "docs" / "package-layout.md"
PACKAGE = ROOT / "src" / "issuebot"

# GitHub's anchor for a heading: lower-case the text, drop everything that is not a word
# character, a hyphen or a space, then turn each space into a hyphen. `issuebot.egress` loses
# its backticks and its dot and comes out `issuebotegress`.
_NOT_SLUG = re.compile(r"[^\w\- ]")
_CODE_SPAN = re.compile(r"`([^`]+)`")


def _slug(heading: str) -> str:
    return _NOT_SLUG.sub("", heading.lower()).replace(" ", "-")


def _headings() -> list[str]:
    """The file's `##` headings, as their source text, in the order it gives them."""
    return [
        line.removeprefix("## ").strip()
        for line in LAYOUT.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]


def _sections() -> list[str]:
    """The module each `##` heading names: `` `issuebot.egress` `` is `egress`."""
    return [heading.strip("`").removeprefix("issuebot.") for heading in _headings()]


def _modules() -> set[str]:
    """Every top-level module of the package, as `issuebot.X` spells X."""
    return {
        entry.stem if entry.is_file() else entry.name
        for entry in PACKAGE.iterdir()
        if (entry.is_dir() and (entry / "__init__.py").is_file())
        or (entry.is_file() and entry.suffix == ".py")
        if not entry.name.startswith("_")
    }


def _pointer() -> str:
    """`CLAUDE.md`'s `## Package layout` section, which is the only route to the file."""
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    start = text.index("## Package layout")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_every_module_has_a_section_of_its_own() -> None:
    """A session that needs one module's design reads one module's section (#221)."""
    sections = _sections()
    assert len(sections) == len(set(sections)), f"a module is sectioned twice: {sections}"
    assert set(sections) == _modules(), (
        "docs/package-layout.md's sections and the package's modules have drifted: "
        f"undocumented {sorted(_modules() - set(sections))}, "
        f"documented but gone {sorted(set(sections) - _modules())}. The file opens by "
        "promising every module of issuebot, and a session reads one section rather than the "
        "whole file, so a module with no `## `issuebot.X`` heading has no entry it can reach "
        "(#221)"
    )


def test_the_sections_run_in_the_order_the_pointer_lists() -> None:
    """The pointer is read first and the file second; they must not disagree about order."""
    opening = _pointer().split("\n\n")[1]  # [0] is the heading line itself
    spans = _CODE_SPAN.findall(opening)
    assert "issuebot.config" in spans, (
        "CLAUDE.md's package-layout pointer no longer opens its list with `issuebot.config`; "
        "re-anchor this test"
    )
    listed = [s.removeprefix("issuebot.") for s in spans[spans.index("issuebot.config") :]]
    assert listed == _sections(), (
        f"CLAUDE.md lists the modules as {listed}, docs/package-layout.md sections them as "
        f"{_sections()} (#221)"
    )


def test_the_pointer_names_the_anchor_form() -> None:
    """Sections a session is never told about are sections it reads the whole file to find."""
    anchors = set(re.findall(r"package-layout\.md#([\w-]+)", _pointer()))
    assert anchors, (
        "CLAUDE.md's `## Package layout` names no `docs/package-layout.md#<anchor>`, so "
        "nothing tells a session it can read one module rather than the file (#221)"
    )
    offered = {_slug(heading) for heading in _headings()}
    assert anchors <= offered, (
        f"CLAUDE.md points at {sorted(anchors - offered)}, which docs/package-layout.md does "
        f"not offer; its anchors are {sorted(offered)} (#221)"
    )
