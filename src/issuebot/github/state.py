"""The label state machine as data, plus pure helpers over it. No I/O."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from issuebot.github.models import Issue, StateLabel


class Actor(StrEnum):
    HUMAN = "human"
    ISSUEBOT = "issuebot"
    AGENT = "agent"


ClosedOutcome = Literal["complete", "cancelled"]

ACTIVE_STATES: frozenset[StateLabel] = frozenset(
    {StateLabel.TODO, StateLabel.REWORK, StateLabel.IN_PROGRESS}
)
TERMINAL_STATES: frozenset[StateLabel] = frozenset({StateLabel.COMPLETE})

TRANSITIONS: frozenset[tuple[StateLabel | None, StateLabel, Actor]] = frozenset(
    {
        (None, StateLabel.TODO, Actor.HUMAN),
        (StateLabel.TODO, StateLabel.IN_PROGRESS, Actor.ISSUEBOT),
        (StateLabel.REWORK, StateLabel.IN_PROGRESS, Actor.ISSUEBOT),
        (StateLabel.IN_PROGRESS, StateLabel.REVIEW, Actor.AGENT),
        (StateLabel.IN_PROGRESS, StateLabel.REVIEW, Actor.ISSUEBOT),
        (StateLabel.REVIEW, StateLabel.REWORK, Actor.HUMAN),
        (StateLabel.REVIEW, StateLabel.TODO, Actor.HUMAN),
        (StateLabel.IN_PROGRESS, StateLabel.TODO, Actor.HUMAN),
        (StateLabel.REVIEW, StateLabel.COMPLETE, Actor.ISSUEBOT),
        (StateLabel.IN_PROGRESS, StateLabel.COMPLETE, Actor.ISSUEBOT),
    }
)


@dataclass(frozen=True, slots=True)
class LabelStyle:
    color: str
    description: str


LABEL_STYLES: dict[StateLabel, LabelStyle] = {
    StateLabel.TODO: LabelStyle("0E8A16", "Queued for issuebot; a human sets this"),
    StateLabel.IN_PROGRESS: LabelStyle("FBCA04", "An issuebot agent is working on it"),
    StateLabel.REVIEW: LabelStyle("1D76DB", "PR opened; waiting for human review"),
    StateLabel.REWORK: LabelStyle("D93F0B", "Reviewer wants changes; issuebot will pick it up"),
    StateLabel.COMPLETE: LabelStyle("5319E7", "Closed by a merged issuebot PR"),
}


def is_allowed(current: StateLabel | None, target: StateLabel, actor: Actor) -> bool:
    return (current, target, actor) in TRANSITIONS


def is_active(state: StateLabel | None) -> bool:
    return state in ACTIVE_STATES


def is_terminal(state: StateLabel | None) -> bool:
    return state in TERMINAL_STATES


def next_state_for(issue: Issue) -> StateLabel | None:
    """The label the orchestrator sets when it dispatches this issue, or None if it must not."""
    if issue.state in ACTIVE_STATES:
        return StateLabel.IN_PROGRESS
    return None


def classify_closed(issue: Issue) -> ClosedOutcome:
    """A closed issue is complete only when a linked pull request was merged."""
    pr = issue.linked_pr
    return "complete" if pr is not None and pr.state == "merged" else "cancelled"
