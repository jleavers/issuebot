"""Tests for the orchestrator's pure records and rules."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from issuebot.agent import RunResult
from issuebot.agent.runner import RateLimits, RateLimitWindow
from issuebot.config import GitHubLabels
from issuebot.events import PrOpened, StateChanged
from issuebot.github import Issue, LinkedPr, StateLabel
from issuebot.orchestrator.state import (
    BlockedContext,
    ClaudeTotals,
    Counters,
    RetryEntry,
    RetryRow,
    RunningEntry,
    RunningRow,
    RuntimeSnapshot,
    backoff_ms,
    claimed_snapshot,
    conflict_candidate,
    observe_transition,
    rate_limits_from_dict,
    sort_candidates,
    state_label_name,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def make_result(**overrides: object) -> RunResult:
    fields: dict[str, object] = {
        "run_id": "run-1",
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "attempt": 1,
        "session_id": "sess",
        "outcome": "succeeded",
        "stop_reason": "issue_moved",
        "error_category": None,
        "error": None,
        "turns": 2,
        "input_tokens": 100,
        "output_tokens": 10,
        "cost_usd": 0.5,
        "duration_s": 12.5,
        "final_state": StateLabel.REVIEW,
        "final_issue": None,
        "workspace_path": Path("/workspaces/repo-42"),
        "log_dir": Path("/workspaces/repo-42/.issuebot/runs/run-1"),
    }
    fields.update(overrides)
    return RunResult(**fields)  # type: ignore[arg-type]


# --- rules ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "cap", "expected"),
    [(1, 300_000, 10_000), (2, 300_000, 20_000), (3, 300_000, 40_000), (6, 300_000, 300_000)],
)
def test_backoff_doubles_from_ten_seconds_and_caps(attempt: int, cap: int, expected: int) -> None:
    assert backoff_ms(attempt, cap) == expected


def test_backoff_cap_is_the_configured_maximum() -> None:
    assert backoff_ms(2, 15_000) == 15_000
    assert backoff_ms(0, 300_000) == 10_000


def test_sort_candidates_ranks_orphans_then_rework_then_todo(
    make_issue: Callable[..., Issue],
) -> None:
    old = datetime(2026, 9, 1, tzinfo=UTC)
    new = datetime(2026, 9, 2, tzinfo=UTC)
    todo_old = make_issue(id="1", number=1, state=StateLabel.TODO, created_at=old)
    todo_new = make_issue(id="2", number=2, state=StateLabel.TODO, created_at=new)
    rework = make_issue(id="3", number=3, state=StateLabel.REWORK, created_at=new)
    orphan = make_issue(id="4", number=4, state=StateLabel.IN_PROGRESS, created_at=new)
    tie = make_issue(id="5", number=5, state=StateLabel.TODO, created_at=old)
    ordered = sort_candidates([tie, todo_new, rework, todo_old, orphan])
    assert [issue.number for issue in ordered] == [4, 3, 1, 5, 2]


def test_state_label_name_is_the_raw_label_or_none(make_issue: Callable[..., Issue]) -> None:
    assert state_label_name(make_issue(state_labels=("Issuebot/Todo",))) == "Issuebot/Todo"
    assert state_label_name(make_issue(state=None, state_labels=())) is None


def _pr(state: str = "open", mergeable: str = "conflicting") -> LinkedPr:
    return LinkedPr(
        number=51,
        url="https://github.com/example/repo/pull/51",
        state=state,  # type: ignore[arg-type]
        merged_at=None,
        mergeable=mergeable,  # type: ignore[arg-type]
    )


def test_conflict_candidate_is_an_open_conflicting_pr_on_a_review_issue(
    make_issue: Callable[..., Issue],
) -> None:
    review = {"state": StateLabel.REVIEW, "state_labels": ("issuebot/review",)}
    assert conflict_candidate(make_issue(**review, linked_pr=_pr()))
    assert not conflict_candidate(make_issue(**review, linked_pr=None))
    assert not conflict_candidate(make_issue(**review, linked_pr=_pr(mergeable="mergeable")))
    assert not conflict_candidate(make_issue(**review, linked_pr=_pr(mergeable="unknown")))
    assert not conflict_candidate(make_issue(**review, linked_pr=_pr(state="merged")))
    assert not conflict_candidate(make_issue(**review, linked_pr=_pr(state="closed")))
    assert not conflict_candidate(make_issue(**review, linked_pr=_pr(), dispatchable=False))
    assert not conflict_candidate(
        make_issue(state=StateLabel.REWORK, state_labels=("issuebot/rework",), linked_pr=_pr())
    )
    assert not conflict_candidate(
        make_issue(
            state=StateLabel.IN_PROGRESS, state_labels=("issuebot/in-progress",), linked_pr=_pr()
        )
    )


def test_claimed_snapshot_replaces_only_the_state_labels(
    make_issue: Callable[..., Issue],
) -> None:
    issue = make_issue(labels=("bug", "issuebot/todo"), state_labels=("issuebot/todo",))
    claimed = claimed_snapshot(issue, GitHubLabels())
    assert claimed.state is StateLabel.IN_PROGRESS
    assert claimed.state_labels == ("issuebot/in-progress",)
    assert claimed.labels == ("bug", "issuebot/in-progress")
    assert claimed.dispatchable is True
    assert (claimed.number, claimed.title, claimed.url) == (issue.number, issue.title, issue.url)


def test_claimed_snapshot_uses_configured_names(make_issue: Callable[..., Issue]) -> None:
    labels = GitHubLabels(in_progress="Bot/Working", todo="bot/queue")
    issue = make_issue(labels=("bot/queue",), state_labels=("bot/queue",))
    assert claimed_snapshot(issue, labels).labels == ("bot/working",)


def test_observe_transition_agent_review(make_issue: Callable[..., Issue]) -> None:
    url = "https://github.com/example/repo/pull/7"
    pr = LinkedPr(number=7, url=url, state="open", merged_at=None)
    before = make_issue(state=StateLabel.IN_PROGRESS, state_labels=("issuebot/in-progress",))
    after = make_issue(state=StateLabel.REVIEW, state_labels=("issuebot/review",), linked_pr=pr)
    events = observe_transition(before, after)
    assert [type(event) for event in events] == [StateChanged, PrOpened]
    changed = events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label, changed.actor) == (
        "issuebot/in-progress",
        "issuebot/review",
        "agent",
    )
    assert changed.pr_url == pr.url
    opened = events[1]
    assert isinstance(opened, PrOpened)
    assert (opened.pr_number, opened.pr_url) == (7, pr.url)


@pytest.mark.parametrize(
    ("before_state", "before_label", "after_state", "after_label"),
    [
        (StateLabel.IN_PROGRESS, "issuebot/in-progress", StateLabel.TODO, "issuebot/todo"),
        (StateLabel.REVIEW, "issuebot/review", StateLabel.REWORK, "issuebot/rework"),
        (StateLabel.IN_PROGRESS, "issuebot/in-progress", None, None),
    ],
)
def test_observe_transition_other_moves_are_human(
    make_issue: Callable[..., Issue],
    before_state: StateLabel,
    before_label: str,
    after_state: StateLabel | None,
    after_label: str | None,
) -> None:
    before = make_issue(state=before_state, state_labels=(before_label,))
    after = make_issue(
        state=after_state, state_labels=(after_label,) if after_label else (), dispatchable=False
    )
    events = observe_transition(before, after)
    assert len(events) == 1
    changed = events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label, changed.actor) == (
        before_label,
        after_label,
        "human",
    )


def test_observe_transition_no_change_is_empty(make_issue: Callable[..., Issue]) -> None:
    issue = make_issue()
    assert observe_transition(issue, issue) == []


# --- records --------------------------------------------------------------------------


def test_running_entry_stop_keeps_the_first_cause(make_issue: Callable[..., Issue]) -> None:
    entry = RunningEntry(
        issue=make_issue(),
        attempt=1,
        rework=False,
        resumed=False,
        run_id="run-1",
        started_mono=0.0,
        started_at=NOW,
        cancel=asyncio.Event(),
    )
    assert (entry.issue_id, entry.identifier) == ("42", "repo-42")
    entry.stop("stalled", "no activity for 301 s")
    entry.stop("shutdown", "worker stopping")
    assert (entry.stop_cause, entry.stop_detail) == ("stalled", "no activity for 301 s")
    assert entry.cancel.is_set()


def test_totals_add_and_counters_bump() -> None:
    totals = ClaudeTotals().add(make_result()).add(make_result(input_tokens=1, cost_usd=0.25))
    assert (totals.input_tokens, totals.output_tokens) == (101, 20)
    assert totals.total_tokens == 121
    assert totals.cost_usd == 0.75
    assert totals.seconds_running == 25.0
    counters = Counters().bump(runs_started=1).bump(runs_started=1, blocked=1)
    assert (counters.runs_started, counters.blocked, counters.runs_ended) == (2, 1, 0)


def test_snapshot_rows_and_to_dict(make_issue: Callable[..., Issue]) -> None:
    entry = RunningEntry(
        issue=make_issue(state=StateLabel.IN_PROGRESS),
        attempt=2,
        rework=True,
        resumed=False,
        run_id="run-1",
        started_mono=10.0,
        started_at=NOW,
        cancel=asyncio.Event(),
    )
    entry.session_id = "sess"
    entry.last_event = "turn_activity:Read"
    entry.turns = 1
    retry = RetryEntry(
        issue_id="7",
        identifier="repo-7",
        issue_number=7,
        issue_url="https://github.com/example/repo/issues/7",
        title="Add a power function",
        attempt=2,
        kind="failure",
        due_mono=30.0,
        due_at=NOW,
        error="process_exit: boom",
        escape=BlockedContext(reason="r", run_id="run-0", attempt=1, turns=1, log_dir=None),
    )
    snapshot = RuntimeSnapshot(
        at=NOW,
        workflow_path="/configs/WORKFLOW.md",
        workflow_mtime_ns=5,
        config_valid=True,
        config_error=None,
        dispatch_hold=None,
        poll_interval_ms=30_000,
        max_concurrent_agents=2,
        tick_count=3,
        last_tick_at=NOW,
        running=(RunningRow.from_entry(entry),),
        retrying=(RetryRow.from_entry(retry),),
        totals=ClaudeTotals(input_tokens=5, output_tokens=6, cost_usd=0.1, seconds_running=2.0),
        counters=Counters(runs_started=1),
    )
    data = snapshot.to_dict()
    assert json.dumps(data)
    # The overlay rides the snapshot like the other worker facts; None without one, and a
    # snapshot built without naming it (every earlier caller) says so.
    assert data["workflow_overlay_path"] is None
    row = data["running"][0]
    assert row["state"] == "in_progress"
    assert (row["attempt"], row["rework"], row["session_id"], row["turns"]) == (2, True, "sess", 1)
    assert row["started_at"] == NOW.isoformat()
    assert row["last_activity_at"] is None
    assert data["retrying"][0]["kind"] == "failure"
    assert data["retrying"][0]["due_at"] == NOW.isoformat()
    # The title rides the snapshot so the Retrying table has something to say about the
    # issue; the identifier is the number again and says nothing (#42).
    assert data["retrying"][0]["title"] == "Add a power function"
    assert data["totals"] == {
        "input_tokens": 5,
        "output_tokens": 6,
        "cost_usd": 0.1,
        "seconds_running": 2.0,
        "total_tokens": 11,
    }
    assert data["counters"]["runs_started"] == 1
    assert data["at"] == NOW.isoformat()


def _snapshot_with(limits: RateLimits | None) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        at=NOW,
        workflow_path="/configs/WORKFLOW.md",
        workflow_mtime_ns=5,
        config_valid=True,
        config_error=None,
        dispatch_hold=None,
        poll_interval_ms=30_000,
        max_concurrent_agents=2,
        tick_count=3,
        last_tick_at=NOW,
        running=(),
        retrying=(),
        totals=ClaudeTotals(),
        counters=Counters(),
        credential="subscription",
        rate_limits=limits,
    )


def test_rate_limits_round_trip_through_the_snapshot() -> None:
    """A worker restart reads its last reading back out of the stored snapshot."""
    limits = RateLimits(
        five_hour=RateLimitWindow(utilization=0.42, resets_at=NOW),
        seven_day=RateLimitWindow(utilization=0.32, resets_at=NOW),
        observed_at=NOW,
    )
    data = _snapshot_with(limits).to_dict()
    assert json.dumps(data)
    assert rate_limits_from_dict(data["rate_limits"]) == limits


def test_rate_limits_from_dict_keeps_a_half_reading() -> None:
    data = _snapshot_with(
        RateLimits(
            five_hour=RateLimitWindow(utilization=0.42, resets_at=NOW),
            seven_day=None,
            observed_at=NOW,
        )
    ).to_dict()
    restored = rate_limits_from_dict(data["rate_limits"])
    assert restored is not None
    assert restored.five_hour is not None and restored.seven_day is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "nonsense",
        5,
        [],
        {},
        {"five_hour": {"utilization": 0.4, "resets_at": NOW.isoformat()}},  # no observed_at
        {"observed_at": NOW.isoformat()},  # no window
        {"observed_at": "not a time", "five_hour": {"utilization": 0.4, "resets_at": "x"}},
    ],
)
def test_rate_limits_from_dict_refuses_what_it_cannot_read(value: object) -> None:
    """The column is JSON written by some older version of this code; it may be anything."""
    assert rate_limits_from_dict(value) is None
