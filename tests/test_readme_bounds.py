"""Every instruction that widens the agent's reach says, where the reader acts on it, which
boundary it removes (#74).

The four choice points below cannot be defaulted shut -- a token scope and a CI permission are the
operator's to choose, and the host route is how the suite itself runs -- so the note beside
each one *is* the enforcement, and it has to travel with the capability rather than live in
the Safety bullet the reader reaches later or not at all. That is a property of the README's
structure, which is why it is pinned here: the incentive to take each route is concrete and
correct, and a later edit that tightens the prose must not leave the incentive behind with
its consequence deleted.

Matching is over whitespace-collapsed text, so rewrapping a paragraph -- which happens often
-- never fails this; only dropping the words does.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_RAW = (ROOT / "README.md").read_text(encoding="utf-8")
README = " ".join(_RAW.split())
# The units a reader takes in as one thing, whitespace-collapsed. Splitting on blank lines
# alone is not enough: a markdown numbered list has none between its items, so the whole of
# Prerequisites collapses into a single ~5.8k-character run and a "same block" check over it
# would pass for a note parked in a different prerequisite entirely. Each numbered item starts
# a block of its own as well.
BLOCKS = [
    " ".join(part.split())
    for block in _RAW.split("\n\n")
    for part in re.split(r"\n(?=\d+\. )", block)
]

# Seven wrapped lines or so: far enough to let the note be a sentence rather than a clause,
# close enough that a reader who has just read the incentive has not moved on. The largest
# real distance today is 640 characters (a consequence must *fit* in the window, not merely
# start in it), in the host-route case.
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
    Sharing the block is the claim that actually matters: the qualifier must not drift off to
    where a reader weighing the grant would never meet it. See `BLOCKS` for why a block is not
    simply what sits between two blank lines.
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
        "already runs with whatever secrets that job is given",
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
    rather than crediting the container with a control it does not supply.

    The uid split of #75 goes with the container too -- raised on the issue after it was filed,
    since #74 predates #75 landing -- and is pinned as a qualifier: it is the elaboration on
    "your own uid" rather than a consequence of its own."""
    _consequence_travels_with(
        "Running the CLI outside a container",
        "calls the sandbox",
        "your own uid",
        "no allow-list between it and the network",
    )
    _qualified_in_the_same_block(
        "Running the CLI outside a container",
        "the split #75 rests on is gone with the container",
    )


def test_claude_credential_names_the_scoping_it_has_none_of() -> None:
    """The fourth choice point, raised on the issue after it was filed. Every other credential
    in the getting-started guide is scoped down on purpose -- a dedicated bot account, a
    repository-scoped token, a generated database password -- and the Claude one is the
    operator's own subscription, minted by `claude setup-token`, held by the session directly
    and refreshing itself rather than expiring. There is no scope to narrow it with, so what
    the note has to name instead is the pair of spend ceilings that do bound it and the
    dedicated account or cappable API key that bounds it outside issuebot.

    The recipe the comment cited (logging in interactively *as the session's account*, with the
    credential in a mounted `claude-home` volume) no longer exists -- #142 made a session
    account one nobody logs into, taking its credential from the environment -- so this pins
    what is still live rather than that recipe.
    """
    _consequence_travels_with(
        "long-lived OAuth token minted from a",
        "the session holds this one *directly*",
        "carries your subscription's whole reach",
        "a subscription reports no per-token cost",
    )
    _qualified_in_the_same_block(
        "long-lived OAuth token minted from a",
        "`agent.max_issue_cost_usd` never fires",
        "an account dedicated to the bot rather than the login you use yourself",
    )
