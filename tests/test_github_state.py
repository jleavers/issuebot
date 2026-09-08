"""Tests for the label state machine."""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from issuebot.config import GitHubLabels
from issuebot.github.models import Issue, LinkedPr, StateLabel
from issuebot.github.state import (
    ACTIVE_STATES,
    LABEL_STYLES,
    NO_FAULT_LABEL_STYLE,
    TERMINAL_STATES,
    TRANSITIONS,
    Actor,
    carries_no_fault,
    classify_closed,
    is_active,
    is_allowed,
    is_terminal,
    marker_label_styles,
    next_state_for,
)

LABELS = GitHubLabels()

MERGED = LinkedPr(
    number=51,
    url="https://github.com/example/repo/pull/51",
    state="merged",
    merged_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
)
OPEN = LinkedPr(
    number=52, url="https://github.com/example/repo/pull/52", state="open", merged_at=None
)
CLOSED = LinkedPr(
    number=50, url="https://github.com/example/repo/pull/50", state="closed", merged_at=None
)


def test_state_label_values_match_settings_field_names() -> None:
    assert [s.value for s in StateLabel] == ["todo", "in_progress", "review", "rework", "complete"]


@pytest.mark.parametrize(
    ("current", "target", "actor"),
    [
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
    ],
)
def test_allowed_transitions(current: StateLabel | None, target: StateLabel, actor: Actor) -> None:
    assert is_allowed(current, target, actor)
    assert (current, target, actor) in TRANSITIONS


@pytest.mark.parametrize(
    ("current", "target", "actor"),
    [
        (StateLabel.TODO, StateLabel.IN_PROGRESS, Actor.HUMAN),
        (StateLabel.TODO, StateLabel.REVIEW, Actor.AGENT),
        (StateLabel.COMPLETE, StateLabel.TODO, Actor.ISSUEBOT),
        (StateLabel.REVIEW, StateLabel.COMPLETE, Actor.AGENT),
        (None, StateLabel.IN_PROGRESS, Actor.ISSUEBOT),
    ],
)
def test_disallowed_transitions(
    current: StateLabel | None, target: StateLabel, actor: Actor
) -> None:
    assert not is_allowed(current, target, actor)


def test_transition_table_size() -> None:
    assert len(TRANSITIONS) == 10


def test_active_and_terminal_partition() -> None:
    assert {StateLabel.TODO, StateLabel.REWORK, StateLabel.IN_PROGRESS} == ACTIVE_STATES
    assert {StateLabel.COMPLETE} == TERMINAL_STATES
    assert is_active(StateLabel.TODO) and is_active(StateLabel.REWORK)
    assert is_active(StateLabel.IN_PROGRESS)
    assert not is_active(StateLabel.REVIEW) and not is_active(StateLabel.COMPLETE)
    assert not is_active(None)
    assert is_terminal(StateLabel.COMPLETE)
    assert not is_terminal(StateLabel.REVIEW) and not is_terminal(None)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (StateLabel.TODO, StateLabel.IN_PROGRESS),
        (StateLabel.REWORK, StateLabel.IN_PROGRESS),
        (StateLabel.IN_PROGRESS, StateLabel.IN_PROGRESS),
        (StateLabel.REVIEW, None),
        (StateLabel.COMPLETE, None),
        (None, None),
    ],
)
def test_next_state_for(
    make_issue: Callable[..., Issue], state: StateLabel | None, expected: StateLabel | None
) -> None:
    assert next_state_for(make_issue(state=state)) == expected


@pytest.mark.parametrize(
    ("pr", "expected"),
    [(MERGED, "complete"), (OPEN, "cancelled"), (CLOSED, "cancelled"), (None, "cancelled")],
)
def test_classify_closed(
    make_issue: Callable[..., Issue], pr: LinkedPr | None, expected: str
) -> None:
    issue = make_issue(github_state="closed", linked_pr=pr, dispatchable=False)
    assert classify_closed(issue, LABELS) == expected


@pytest.mark.parametrize(
    ("pr", "expected"),
    [(MERGED, "complete"), (OPEN, "cancelled"), (CLOSED, "cancelled"), (None, "no_change")],
)
def test_classify_closed_reads_the_no_fault_marker(
    make_issue: Callable[..., Issue], pr: LinkedPr | None, expected: str
) -> None:
    """The marker turns an abandonment into a no-change close, but only while there is no PR.

    Nothing removes the label, so a session that later opened a pull request leaves it stale;
    an unmerged PR on such an issue is still the abandonment it looks like.
    """
    issue = make_issue(
        github_state="closed",
        linked_pr=pr,
        dispatchable=False,
        labels=("issuebot/review", "issuebot/no-fault"),
    )
    assert classify_closed(issue, LABELS) == expected


def test_the_no_fault_marker_is_read_under_the_configured_name(
    make_issue: Callable[..., Issue],
) -> None:
    """`Issue.labels` is lowercased by the normaliser, so the comparison must be too."""
    labels = GitHubLabels(no_fault="Team/No-Fault")
    issue = make_issue(github_state="closed", dispatchable=False, labels=("team/no-fault",))
    assert carries_no_fault(issue, labels)
    assert not carries_no_fault(issue, LABELS)
    assert classify_closed(issue, labels) == "no_change"


def test_marker_labels_are_named_and_styled_but_are_not_states() -> None:
    """A marker must never reach `as_tuple()`: `clear_state` strips exactly those names."""
    assert marker_label_styles(LABELS) == {"issuebot/no-fault": NO_FAULT_LABEL_STYLE}
    assert set(marker_label_styles(LABELS)).isdisjoint(LABELS.as_tuple())
    assert len(NO_FAULT_LABEL_STYLE.color) == 6
    int(NO_FAULT_LABEL_STYLE.color, 16)
    assert NO_FAULT_LABEL_STYLE.description


def test_label_styles_cover_every_role() -> None:
    assert set(LABEL_STYLES) == set(StateLabel)
    for style in LABEL_STYLES.values():
        assert len(style.color) == 6
        int(style.color, 16)
        assert style.description
