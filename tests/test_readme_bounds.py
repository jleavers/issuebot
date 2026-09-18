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

ROOT = Path(__file__).resolve().parent.parent
README = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())

# Seven wrapped lines or so: far enough to let the note be a sentence rather than a clause,
# close enough that a reader who has just read the incentive has not moved on. The largest
# real distance today is ~520 characters.
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


def test_workflows_write_names_the_review_gate_it_removes() -> None:
    """copycat-6. Workflows write is offered because a push touching `.github/workflows/` is
    rejected without it. What it removes is human review as the gate on what CI runs: the
    session pushes to a branch of the target repository itself, and GitHub withholds secrets
    from a fork's ref but not from a same-repository one, so the workflow the session wrote
    runs with that repository's Actions secrets before the diff is read."""
    _consequence_travels_with(
        "also grant Workflows",
        "human review as the gate on what CI runs",
        "Actions secrets",
        "before anyone has read the diff",
        "Nothing in issuebot replaces that gate",
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
    `$HOME`, no permission prompts and no egress allow-list, on an issue body anyone can
    write."""
    _consequence_travels_with(
        "Running the CLI outside a container",
        "calls the sandbox",
        "your own uid",
        "no permission prompts and no allow-list",
    )
