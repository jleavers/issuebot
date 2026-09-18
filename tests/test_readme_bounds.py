"""Every instruction that widens the agent's reach says, where the reader acts on it, which
boundary it removes (#74).

The three grants below cannot be defaulted shut -- a token scope and a CI permission are the
operator's to choose, and the host route is how the suite itself runs -- so the note beside
each one *is* the enforcement, and it has to travel with the capability rather than live in
the Safety bullet the reader reaches later or not at all. That is a property of the README's
structure, which is why it is pinned here: the incentive to take each route is concrete and
correct, and a later edit that tightens the prose must not leave the incentive behind with
its consequence deleted.

Matching is over whitespace-collapsed text, so rewrapping a paragraph -- which happens often
-- never fails this; only dropping the words does.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_RAW = (ROOT / "README.md").read_text(encoding="utf-8")
README = " ".join(_RAW.split())
# Blank-line-delimited blocks, whitespace-collapsed. A numbered prerequisite is one block, so
# "the same block" is "the thing the reader is currently reading".
BLOCKS = [" ".join(block.split()) for block in _RAW.split("\n\n")]

# Seven wrapped lines or so: far enough to let the note be a sentence rather than a clause,
# close enough that a reader who has just read the incentive has not moved on. The largest
# real distance today is 600 characters, in the host-route case.
POINT_OF_USE = 700


def _consequence_travels_with(incentive: str, *consequences: str) -> None:
    """The consequence is stated within `POINT_OF_USE` characters *after* the incentive."""
    start = README.find(incentive)
    assert start != -1, f"README no longer says {incentive!r}; re-anchor this test"
    window = README[start : start + POINT_OF_USE]
    for consequence in consequences:
        assert consequence in window, (
            f"{incentive!r} is stated without {consequence!r} beside it: "
            "a widening instruction must name the boundary it removes at the point of use (#74)"
        )


def _qualified_in_the_same_block(incentive: str, *qualifiers: str) -> None:
    """A qualifier on the consequence shares the incentive's block, but is not distance-bound.

    It is the tail of the note rather than the note, so holding it to `POINT_OF_USE` would mean
    raising that constant for every case and blunting the guard the primary phrases need.
    Sharing the block is the claim that actually matters: the qualifier must not drift off into
    a section of its own, where a reader weighing the grant would never meet it.
    """
    blocks = [block for block in BLOCKS if incentive in block]
    assert len(blocks) == 1, f"{incentive!r} matches {len(blocks)} blocks; re-anchor this test"
    for qualifier in qualifiers:
        assert qualifier in blocks[0], (
            f"{incentive!r} no longer carries {qualifier!r} in its own block (#74)"
        )


def test_workflows_write_names_the_review_gate_it_removes() -> None:
    """copycat-6. Workflows write is offered because a push touching `.github/workflows/` is
    rejected without it. What it removes is human review over what CI *is* -- a job definition
    the session wrote, its triggers and the secrets it names, runs as written on a
    same-repository ref, where a fork's would be withheld them.

    The correction is pinned too, because the obvious way to write this note overclaims: with
    Contents write alone the session already pushes a branch, so wherever a workflow runs
    repository code (this repository's own `uv run pytest`, say) that code is the session's and
    reaches those secrets already. Leaving Workflows off narrows the blast radius; it does not
    close it, and an operator must not read the note as saying otherwise."""
    _consequence_travels_with(
        "also grant Workflows",
        "removes is human review over what CI",
        "before anyone has read the diff",
        "Nothing in issuebot replaces that gate",
    )
    _qualified_in_the_same_block(
        "also grant Workflows",
        "Leaving it off narrows that blast radius rather than closing it",
        "already runs with those secrets",
    )


def test_classic_token_names_the_repository_scoping_it_removes() -> None:
    """copycat-5. The classic `repo` token is offered because it reads check runs where a
    fine-grained token cannot. What it removes is the scoping to one repository that the
    Safety note names as the control on a credential the session itself holds."""
    _consequence_travels_with(
        "A classic token with the `repo` scope",
        "reach is the whole account's, not one repository's",
    )


def test_host_route_names_the_container_it_removes() -> None:
    """copycat-3. The host route is offered because it needs no Docker. What it removes is the
    container, which is the sandbox -- the session runs at the operator's own uid, with their
    `$HOME` and no egress allow-list, on an issue body anyone can write. Not the permission
    prompts: `--permission-prompts none` is unconditional on every route, so the note says so
    rather than crediting the container with a control it does not supply."""
    _consequence_travels_with(
        "Running the CLI outside a container",
        "calls the sandbox",
        "your own uid",
        "no allow-list between it and the network",
    )
