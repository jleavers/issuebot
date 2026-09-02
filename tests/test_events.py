"""Tests for event types."""

import json
from datetime import UTC, datetime

from issuebot.events import (
    EVENT_KINDS,
    Blocked,
    Event,
    IssueCancelled,
    IssueCompleted,
    NotificationSent,
    PrOpened,
    RunEnded,
    RunStarted,
    StateChanged,
)

ALL_EVENTS: list[Event] = [
    StateChanged(
        issue_number=1,
        issue_identifier="repo-1",
        from_label="issuebot/todo",
        to_label="issuebot/in-progress",
        actor="issuebot",
    ),
    RunStarted(
        issue_number=1,
        issue_identifier="repo-1",
        run_id="run-1",
        attempt=1,
        session_id=None,
        workspace_path="/workspaces/repo-1",
    ),
    RunEnded(
        issue_number=1,
        issue_identifier="repo-1",
        run_id="run-1",
        outcome="succeeded",
        error=None,
        turns=2,
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.01,
        duration_s=12.5,
    ),
    PrOpened(issue_number=1, issue_identifier="repo-1", pr_number=9, pr_url="https://x/pull/9"),
    Blocked(issue_number=1, issue_identifier="repo-1", reason="turn budget exhausted"),
    IssueCompleted(issue_number=1, issue_identifier="repo-1", pr_url="https://x/pull/9"),
    IssueCancelled(issue_number=1, issue_identifier="repo-1", reason="closed without merge"),
    NotificationSent(
        issue_number=1, issue_identifier="repo-1", channel="slack", about_kind="state_changed"
    ),
]


def test_every_event_serialises_to_json() -> None:
    for event in ALL_EVENTS:
        data = event.to_dict()
        assert data["kind"] == event.kind
        assert data["issue_number"] == 1
        assert data["issue_identifier"] == "repo-1"
        datetime.fromisoformat(data["at"])
        json.dumps(data)


def test_event_kinds_registry_matches_classes() -> None:
    assert {
        "state_changed",
        "run_started",
        "run_ended",
        "pr_opened",
        "blocked",
        "issue_completed",
        "issue_cancelled",
        "notification_sent",
    } == EVENT_KINDS
    assert {event.kind for event in ALL_EVENTS} == EVENT_KINDS


def test_at_defaults_to_aware_utc_now() -> None:
    before = datetime.now(UTC)
    event = Blocked(issue_number=1, issue_identifier="repo-1", reason="x")
    assert event.at.tzinfo is UTC
    assert before <= event.at <= datetime.now(UTC)


def test_state_changed_pr_url_defaults_to_none() -> None:
    event = StateChanged(
        issue_number=1, issue_identifier="repo-1", from_label=None, to_label="x", actor="human"
    )
    assert event.to_dict()["pr_url"] is None


def test_events_are_immutable() -> None:
    event = Blocked(issue_number=1, issue_identifier="repo-1", reason="x")
    try:
        event.reason = "y"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("event was mutable")
