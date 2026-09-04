"""Tests for event types."""

import io
import json
from datetime import UTC, datetime

import pytest

from issuebot.events import (
    EVENT_KINDS,
    Blocked,
    Event,
    EventBus,
    IssueCancelled,
    IssueCompleted,
    LogSink,
    NotificationSent,
    PrOpened,
    RunEnded,
    RunStarted,
    StateChanged,
)
from issuebot.log import configure_logging

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


def test_run_ended_log_dir_defaults_to_none_and_serialises() -> None:
    ended = ALL_EVENTS[2]
    assert isinstance(ended, RunEnded)
    assert ended.log_dir is None
    assert ended.to_dict()["log_dir"] is None
    with_dir = RunEnded(
        issue_number=1,
        issue_identifier="repo-1",
        run_id="run-1",
        outcome="failed",
        error="x",
        turns=0,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        duration_s=0.1,
        log_dir="/workspaces/repo-1/.issuebot/runs/run-1",
    )
    assert json.loads(json.dumps(with_dir.to_dict()))["log_dir"] == (
        "/workspaces/repo-1/.issuebot/runs/run-1"
    )


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


# --- bus and sinks -------------------------------------------------------------


class _Recorder:
    def __init__(self, name: str) -> None:
        self.name = name
        self.seen: list[Event] = []

    def handle(self, event: Event) -> None:
        self.seen.append(event)


class _Exploder:
    name = "exploder"

    def handle(self, event: Event) -> None:
        raise RuntimeError("boom")


def _blocked() -> Blocked:
    return Blocked(issue_number=1, issue_identifier="repo-1", reason="x")


def test_publish_fans_out_in_registration_order() -> None:
    first, second = _Recorder("first"), _Recorder("second")
    bus = EventBus([first])
    bus.add_sink(second)
    event = _blocked()
    bus.publish(event)
    assert first.seen == [event]
    assert second.seen == [event]
    assert [s.name for s in bus.sinks] == ["first", "second"]


def test_raising_sink_is_isolated_counted_and_logged() -> None:
    stream = io.StringIO()
    configure_logging(fmt="json", stream=stream)  # type: ignore[arg-type]
    after = _Recorder("after")
    bus = EventBus([_Exploder(), after])
    bus.publish(_blocked())
    bus.publish(_blocked())
    assert len(after.seen) == 2
    assert bus.failures == {"exploder": 2}
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0]["event"] == "event_sink_failed"
    assert lines[0]["sink"] == "exploder"
    assert lines[0]["event_kind"] == "blocked"
    assert lines[0]["level"] == "error"


def test_duplicate_sink_name_is_rejected() -> None:
    bus = EventBus([_Recorder("dup")])
    with pytest.raises(ValueError, match="dup"):
        bus.add_sink(_Recorder("dup"))


def test_remove_sink_stops_delivery() -> None:
    sink = _Recorder("gone")
    bus = EventBus([sink])
    bus.remove_sink("gone")
    bus.publish(_blocked())
    assert sink.seen == []
    assert bus.sinks == ()


def test_log_sink_logs_kind_and_fields() -> None:
    stream = io.StringIO()
    configure_logging(fmt="json", stream=stream)  # type: ignore[arg-type]
    bus = EventBus([LogSink()])
    bus.publish(
        StateChanged(
            issue_number=3,
            issue_identifier="repo-3",
            from_label="issuebot/todo",
            to_label="issuebot/in-progress",
            actor="issuebot",
        )
    )
    (record,) = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert record["event"] == "state_changed"
    assert record["issue_number"] == 3
    assert record["to_label"] == "issuebot/in-progress"
    assert record["logger"] == "issuebot.events"
    assert record["level"] == "info"
    assert LogSink.name == "log"
