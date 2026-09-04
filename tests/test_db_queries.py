"""Tests for the query module against a seeded database (skipped without DATABASE_URL)."""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from issuebot.config import GitHubLabels
from issuebot.db import StoreError, connect, migrate
from issuebot.db.database import Database
from issuebot.db.queries import COMPLETE_LIMIT, DailyPoint, EventRow, IssueRow, RunRow
from issuebot.db.store import IssueSnapshot, PostgresStore
from issuebot.events import Blocked, RunEnded, RunStarted
from issuebot.github import Issue, StateLabel

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


@pytest.fixture
async def seeded(db_url: str, make_issue: Callable[..., Issue]) -> AsyncIterator[Database]:
    """Issues closed 1 h, 3 d and 10 d ago; a cancelled one; open issues; runs; events."""
    await migrate(db_url)
    store = PostgresStore(db_url, labels=GitHubLabels())
    await store.connect()

    def issue(number: int, state: StateLabel | None, **overrides: Any) -> Issue:
        fields: dict[str, Any] = {
            "number": number,
            "identifier": f"repo-{number}",
            "title": f"Issue {number}",
            "state": state,
            "state_labels": (f"issuebot/{state.value}",) if state else (),
            "labels": (f"issuebot/{state.value}",) if state else (),
            "updated_at": NOW - number * HOUR,
        }
        fields.update(overrides)
        return make_issue(**fields)

    def closed(
        number: int, ago: timedelta, state: StateLabel | None = StateLabel.COMPLETE
    ) -> Issue:
        return issue(number, state, github_state="closed", closed_at=NOW - ago)

    issues = [
        issue(1, StateLabel.TODO),
        issue(2, StateLabel.IN_PROGRESS),
        issue(3, StateLabel.REVIEW),
        issue(4, StateLabel.TODO),
        issue(5, None),  # unlabelled: on no board column
        closed(10, HOUR),
        closed(11, 3 * DAY),
        closed(12, 10 * DAY),
        closed(13, HOUR, state=StateLabel.REVIEW),  # closed, awaiting the sweep: not counted
        closed(14, HOUR, state=None),  # cancelled: not counted
    ]
    await store.upsert_issues([IssueSnapshot(issue=i, seen_at=NOW) for i in issues])

    def started(run_id: str, number: int, ago: timedelta) -> RunStarted:
        return RunStarted(
            issue_number=number,
            issue_identifier=f"repo-{number}",
            run_id=run_id,
            attempt=1,
            session_id="s",
            workspace_path="/w",
            at=NOW - ago,
        )

    await store.apply_event(started("r1", 2, HOUR))
    await store.apply_event(started("r2", 2, 2 * DAY))
    await store.apply_event(started("r3", 10, 10 * DAY))
    await store.apply_event(
        RunEnded(
            issue_number=2,
            issue_identifier="repo-2",
            run_id="r2",
            outcome="failed",
            error="turn_failed: x",
            turns=1,
            input_tokens=10,
            output_tokens=1,
            cost_usd=0.1,
            duration_s=30.0,
            at=NOW - 2 * DAY + timedelta(seconds=30),
        )
    )
    await store.apply_event(
        Blocked(issue_number=2, issue_identifier="repo-2", reason="stuck", at=NOW - HOUR)
    )
    await store.close()
    yield Database(db_url)


async def test_counts_by_window(seeded: Database) -> None:
    async with seeded.queries() as q:
        assert await q.closed_count(DAY) == 1
        assert await q.closed_count(7 * DAY) == 2
        assert await q.runs_count(DAY) == 1
        assert await q.runs_count(7 * DAY) == 2
        assert await q.runs_count(30 * DAY) == 3


async def test_daily_series_zero_fills_and_ends_today(seeded: Database, db_url: str) -> None:
    async with seeded.queries() as q:
        series = await q.daily_series(4)
    conn = await connect(db_url)
    try:
        row = await (await conn.execute("SELECT current_date")).fetchone()
    finally:
        await conn.close()
    assert row is not None
    today = row[0]
    assert len(series) == 4
    assert all(isinstance(point, DailyPoint) for point in series)
    assert [point.day for point in series] == [today - timedelta(days=n) for n in (3, 2, 1, 0)]
    by_day = {point.day: point for point in series}
    # Issue 10 and run r1 sit one hour before NOW: today, or yesterday in the first UTC hour.
    recent = (NOW - HOUR).date()
    assert (by_day[recent].closed, by_day[recent].runs) == (1, 1)
    three_days_ago = (NOW - 3 * DAY).date()
    assert (by_day[three_days_ago].closed, by_day[three_days_ago].runs) == (1, 0)
    two_days_ago = (NOW - 2 * DAY).date()
    assert (by_day[two_days_ago].closed, by_day[two_days_ago].runs) == (0, 1)


async def test_issues_by_state_groups_every_role(seeded: Database) -> None:
    async with seeded.queries() as q:
        groups = await q.issues_by_state()
    assert list(groups) == ["todo", "in_progress", "review", "rework", "complete"]
    assert [row.number for row in groups["todo"]] == [1, 4]  # updated_at desc
    assert [row.number for row in groups["in_progress"]] == [2]
    assert [row.number for row in groups["review"]] == [3]
    assert groups["rework"] == []
    assert [row.number for row in groups["complete"]] == [10, 11, 12]  # closed_at desc
    row = groups["todo"][0]
    assert isinstance(row, IssueRow)
    assert (row.identifier, row.title, row.state_label) == ("repo-1", "Issue 1", "issuebot/todo")
    assert row.labels == ["issuebot/todo"]
    assert row.seen_at == NOW


async def test_issues_by_state_caps_the_complete_column(
    db_url: str, make_issue: Callable[..., Issue]
) -> None:
    await migrate(db_url)
    store = PostgresStore(db_url, labels=GitHubLabels())
    await store.connect()
    snapshots = [
        IssueSnapshot(
            issue=make_issue(
                number=n,
                identifier=f"repo-{n}",
                state=StateLabel.COMPLETE,
                github_state="closed",
                closed_at=NOW - n * HOUR,
            ),
            seen_at=NOW,
        )
        for n in range(1, COMPLETE_LIMIT + 6)
    ]
    await store.upsert_issues(snapshots)
    await store.close()
    async with Database(db_url).queries() as q:
        groups = await q.issues_by_state()
    assert len(groups["complete"]) == COMPLETE_LIMIT
    assert groups["complete"][0].number == 1


async def test_runs_for_issue_newest_first(seeded: Database) -> None:
    async with seeded.queries() as q:
        runs = await q.runs_for_issue(2)
        assert await q.runs_for_issue(99) == []
    assert [run.run_id for run in runs] == ["r1", "r2"]
    assert all(isinstance(run, RunRow) for run in runs)
    assert (runs[0].outcome, runs[0].ended_at) == (None, None)
    assert (runs[1].outcome, runs[1].error, runs[1].cost_usd) == ("failed", "turn_failed: x", 0.1)
    assert runs[1].ended_at == runs[1].started_at + timedelta(seconds=30)


async def test_recent_events_newest_first_and_limited(seeded: Database) -> None:
    async with seeded.queries() as q:
        events = await q.recent_events(3)
        everything = await q.recent_events(100)
    assert [event.kind for event in events] == ["blocked", "run_ended", "run_started"]
    assert len(everything) == 5
    assert all(isinstance(event, EventRow) for event in events)
    assert events[0].payload["reason"] == "stuck"
    assert (events[0].issue_number, events[0].run_id) == (2, None)
    assert events[1].run_id == "r2"


async def test_snapshot_is_none_until_written(seeded: Database, db_url: str) -> None:
    async with seeded.queries() as q:
        assert await q.snapshot() is None
    store = PostgresStore(db_url, labels=GitHubLabels())
    await store.connect()
    await store.write_snapshot(NOW, {"tick_count": 3})
    await store.close()
    async with seeded.queries() as q:
        row = await q.snapshot()
    assert row is not None
    assert (row.at, row.data) == (NOW, {"tick_count": 3})
    assert row.written_at >= NOW


async def test_queries_on_an_empty_schema_report_a_database_error(db_url: str) -> None:
    with pytest.raises(StoreError, match="UndefinedTable"):
        async with Database(db_url).queries() as q:
            await q.snapshot()
