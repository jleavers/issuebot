"""The label state machine as data, plus pure helpers over it. No I/O."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from issuebot.config import GitHubLabels
from issuebot.github.models import Issue, StateLabel


class Actor(StrEnum):
    HUMAN = "human"
    ISSUEBOT = "issuebot"
    AGENT = "agent"


ClosedOutcome = Literal["complete", "no_change", "cancelled"]

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
    StateLabel.COMPLETE: LabelStyle(
        "5319E7", "Closed by a merged issuebot PR, or after issuebot found no fault"
    ),
}

NO_FAULT_LABEL_STYLE = LabelStyle(
    "C5DEF5", "issuebot investigated and found nothing to fix; no PR was opened"
)


def marker_label_styles(labels: GitHubLabels) -> dict[str, LabelStyle]:
    """The non-state labels issuebot owns, keyed by their configured names."""
    return {labels.no_fault: NO_FAULT_LABEL_STYLE}


MODEL_LABEL_COLOR = "BFD4F2"


def model_label_style(model: str) -> LabelStyle:
    """The style of a label that picks a claude model for one issue."""
    return LabelStyle(MODEL_LABEL_COLOR, f"Run this issue with the {model} model")


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


def carries_no_fault(issue: Issue, labels: GitHubLabels) -> bool:
    """True when the session's no-fault marker is on the issue (``Issue.labels`` is lowered)."""
    return labels.no_fault.strip().lower() in issue.labels


def classify_closed(issue: Issue, labels: GitHubLabels) -> ClosedOutcome:
    """How a closed issue was resolved: a merged PR, an investigation, or neither.

    A merged linked pull request is ``complete`` however the issue is labelled. Failing that,
    the no-fault marker means a session's investigation is the delivered value, so the close is
    ``no_change`` rather than an abandonment. Everything else is ``cancelled``, as before.

    The marker is trusted whatever pull requests the issue has linked, because ``claim`` clears
    it: it is always the verdict of the *last* session, and a session that opened a pull request
    does not add it. So an unmerged pull request under the marker is an earlier attempt that the
    investigation superseded, not evidence of an abandonment.
    """
    pr = issue.linked_pr
    if pr is not None and pr.state == "merged":
        return "complete"
    return "no_change" if carries_no_fault(issue, labels) else "cancelled"
