"""Tests for the query module against a seeded database (skipped without DATABASE_URL)."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from issuebot.agent.turnlog import TurnCapture
from issuebot.config import GitHubLabels
from issuebot.db import StoreError, connect, migrate
from issuebot.db.database import Database
from issuebot.db.queries import (
    BOARD_LIMIT,
    ISSUE_LIST_LIMIT,
    DailyPoint,
    EventRow,
    IssueRow,
    RepoQueries,
    RunRow,
    RunTotals,
    TurnRow,
    TurnSummaryRow,
)
from issuebot.db.store import IssueSnapshot, PostgresStore
from issuebot.events import Blocked, IssueCompleted, RunEnded, RunStarted, StateChanged
from issuebot.github import Issue, StateLabel

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
REPO = "example/repo"


@asynccontextmanager
async def scoped(database: Database, repo: str = REPO) -> AsyncIterator[RepoQueries]:
    """A RepoQueries bound to ``repo`` on one connection: what a request reads through."""
    async with database.queries() as queries:
        yield queries.scoped(repo)


@pytest.fixture
async def seeded(db_url: str, make_issue: Callable[..., Issue]) -> AsyncIterator[Database]:
    """Issues closed 1 h, 3 d and 10 d ago; a cancelled one; open issues; runs; events."""
    await migrate(db_url)
    store = PostgresStore(db_url, repo=REPO, labels=GitHubLabels())
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


def capture(turn_number: int, **overrides: Any) -> TurnCapture:
    fields: dict[str, Any] = {
        "turn_number": turn_number,
        "model": "claude-opus-5",
        "subtype": "success",
        "is_error": False,
        "num_turns": 19,
        "input_tokens": 38,
        "cache_creation_input_tokens": 23100,
        "cache_read_input_tokens": 490200,
        "output_tokens": 8425,
        "cost_usd": 0.8976,
        "duration_ms": 201719,
        "result_text": "Done.",
        "prompt": "You are working on issue #2.",
        "prompt_bytes": 28,
        "stream": '{"type":"result"}\n',
        "stream_bytes": 18,
        "stream_lines": 1,
        "omitted_lines": 0,
        "stderr": "warning: slow\n",
        "stderr_bytes": 14,
        "truncated": False,
    }
    fields.update(overrides)
    return TurnCapture(**fields)


def run_ended(run_id: str, number: int, at: datetime, **overrides: Any) -> RunEnded:
    fields: dict[str, Any] = {
        "issue_number": number,
        "issue_identifier": f"repo-{number}",
        "run_id": run_id,
        "outcome": "succeeded",
        "error": None,
        "turns": 1,
        "input_tokens": 10,
        "output_tokens": 1,
        "cost_usd": 0.1,
        "duration_s": 30.0,
        "at": at,
    }
    fields.update(overrides)
    return RunEnded(**fields)


@pytest.fixture
async def with_turns(seeded: Database, db_url: str) -> Database:
    """Two captured turns on r2 and one on r0, an older finished run of the same issue."""
    store = PostgresStore(db_url, repo=REPO, labels=GitHubLabels())
    await store.connect()
    await store.apply_event(
        run_ended("r2", 2, NOW - 2 * DAY + timedelta(seconds=30), outcome="failed"),
        turns=[capture(1), capture(2, subtype=None, num_turns=None, truncated=True)],
    )
    await store.apply_event(
        RunStarted(
            issue_number=2,
            issue_identifier="repo-2",
            run_id="r0",
            attempt=1,
            session_id="s0",
            workspace_path="/w",
            at=NOW - 3 * DAY,
        )
    )
    await store.apply_event(
        run_ended("r0", 2, NOW - 3 * DAY + timedelta(seconds=30)),
        turns=[capture(1, model="claude-sonnet-5")],
    )
    await store.close()
    return seeded


async def test_a_no_fault_close_reaches_the_closed_count(
    db_url: str, make_issue: Callable[..., Issue]
) -> None:
    """#34: the events `finish_terminal` publishes for a no-change close must be counted.

    The tile counts `state = 'complete'`, so what makes this work is that the sweep labels the
    issue `complete` rather than clearing it. Drive the real events, not a hand-written row.
    """
    await migrate(db_url)
    store = PostgresStore(db_url, repo=REPO, labels=GitHubLabels())
    await store.connect()
    handed_over = make_issue(
        number=20,
        identifier="repo-20",
        title="Reported defect that does not happen",
        state=StateLabel.REVIEW,
        state_labels=("issuebot/review",),
        labels=("issuebot/review", "issuebot/no-fault"),
        github_state="closed",
        closed_at=NOW - HOUR,
        updated_at=NOW - HOUR,
    )
    await store.upsert_issues([IssueSnapshot(issue=handed_over, seen_at=NOW - HOUR)])
    await store.apply_event(
        StateChanged(
            issue_number=20,
            issue_identifier="repo-20",
            from_label="issuebot/review",
            to_label="issuebot/complete",
            actor="issuebot",
            at=NOW,
        )
    )
    await store.apply_event(
        IssueCompleted(
            issue_number=20,
            issue_identifier="repo-20",
            pr_url=None,
            resolution="no_change",
            at=NOW,
        )
    )
    await store.close()

    database = Database(db_url)
    async with scoped(database) as q:
        assert await q.closed_count(DAY) == 1
        assert (await q.state_counts())["complete"] == 1
        # It is on the board, in a terminal column, rather than gone.
        assert [i.number for i in (await q.issues_by_state())["complete"]] == [20]
        # And the resolution survives on the timeline for the issue page to read.
        payloads = [e.payload for e in await q.events_for_issue(20, 10)]
        assert {"resolution": "no_change"}.items() <= payloads[0].items()


async def test_counts_by_window(seeded: Database) -> None:
    async with scoped(seeded) as q:
        assert await q.closed_count(DAY) == 1
        assert await q.closed_count(7 * DAY) == 2
        assert await q.runs_count(DAY) == 1
        assert await q.runs_count(7 * DAY) == 2
        assert await q.runs_count(30 * DAY) == 3


async def test_run_totals_by_window(seeded: Database, db_url: str) -> None:
    """Cost and tokens over the runs runs_count counts: the ones started inside the window."""
    store = PostgresStore(db_url, repo=REPO, labels=GitHubLabels())
    await store.connect()
    await store.apply_event(
        RunStarted(
            issue_number=2,
            issue_identifier="repo-2",
            run_id="r4",
            attempt=2,
            session_id="s4",
            workspace_path="/w",
            at=NOW - HOUR,
        )
    )
    await store.apply_event(
        run_ended(
            "r4",
            2,
            NOW - HOUR + timedelta(seconds=30),
            input_tokens=200,
            output_tokens=20,
            cost_usd=0.25,
        )
    )
    await store.close()
    async with scoped(seeded) as q:
        day = await q.run_totals(DAY)
        week = await q.run_totals(7 * DAY)
        nothing = await q.run_totals(timedelta(seconds=1))
    assert isinstance(day, RunTotals)
    assert (day.input_tokens, day.output_tokens, day.total_tokens) == (200, 20, 220)
    assert day.cost_usd == pytest.approx(0.25)
    assert (week.input_tokens, week.output_tokens, week.total_tokens) == (210, 21, 231)
    assert week.cost_usd == pytest.approx(0.35)
    assert (nothing.input_tokens, nothing.output_tokens, nothing.total_tokens) == (0, 0, 0)
    assert nothing.cost_usd == 0.0


async def test_daily_series_zero_fills_and_ends_today(seeded: Database, db_url: str) -> None:
    async with scoped(seeded) as q:
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
    async with scoped(seeded) as q:
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


async def test_issues_by_state_caps_every_column(
    db_url: str, make_issue: Callable[..., Issue]
) -> None:
    """Not just complete: an open column that piles up would stretch the board just as far."""
    await migrate(db_url)
    store = PostgresStore(db_url, repo=REPO, labels=GitHubLabels())
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
        for n in range(1, BOARD_LIMIT + 6)
    ] + [
        IssueSnapshot(
            issue=make_issue(
                number=100 + n,
                identifier=f"repo-{100 + n}",
                state=StateLabel.TODO,
                updated_at=NOW - n * HOUR,
            ),
            seen_at=NOW,
        )
        for n in range(1, BOARD_LIMIT + 6)
    ]
    await store.upsert_issues(snapshots)
    await store.close()
    async with scoped(Database(db_url)) as q:
        groups = await q.issues_by_state()
    assert len(groups["complete"]) == BOARD_LIMIT
    assert [row.number for row in groups["complete"]] == list(range(1, BOARD_LIMIT + 1))
    assert len(groups["todo"]) == BOARD_LIMIT
    assert [row.number for row in groups["todo"]] == list(range(101, 101 + BOARD_LIMIT))


async def test_issues_for_state_returns_one_column_past_the_board_cap(seeded: Database) -> None:
    """What the list page is for: the column in full, not the board's five."""
    async with scoped(seeded) as q:
        rows = await q.issues_for_state("complete")
        todo = await q.issues_for_state("todo")
    assert [row.number for row in rows] == [10, 11, 12]  # closed_at desc
    assert all(isinstance(row, IssueRow) for row in rows)
    assert [row.number for row in todo] == [1, 4]  # updated_at desc


async def test_issues_for_state_without_a_state_returns_every_column(seeded: Database) -> None:
    async with scoped(seeded) as q:
        rows = await q.issues_for_state(None)
    assert {row.number for row in rows} == {1, 2, 3, 4, 10, 11, 12}
    assert 5 not in {row.number for row in rows}  # unlabelled: on no column
    assert 13 not in {row.number for row in rows}  # closed in review: awaiting the sweep
    assert 14 not in {row.number for row in rows}  # cancelled


async def test_issues_for_state_skips_an_unknown_role(seeded: Database, db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await conn.execute(
            """
            INSERT INTO issues (repo, number, identifier, title, state, state_label,
                                github_state, url, created_at, updated_at, seen_at)
            VALUES ('example/repo', 78, 'repo-78', 'Mystery', 'mystery', 'issuebot/mystery',
                    'open', 'https://github.com/example/repo/issues/78', now(), now(), now())
            """
        )
    finally:
        await conn.close()
    async with scoped(seeded) as q:
        assert 78 not in {row.number for row in await q.issues_for_state(None)}
        assert await q.issues_for_state("mystery") == []


async def test_issues_for_state_stops_at_the_list_limit(
    db_url: str, make_issue: Callable[..., Issue]
) -> None:
    await migrate(db_url)
    store = PostgresStore(db_url, repo=REPO, labels=GitHubLabels())
    await store.connect()
    await store.upsert_issues(
        [
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
            for n in range(1, ISSUE_LIST_LIMIT + 4)
        ]
    )
    await store.close()
    async with scoped(Database(db_url)) as q:
        rows = await q.issues_for_state("complete")
    assert len(rows) == ISSUE_LIST_LIMIT
    assert rows[0].number == 1


async def test_runs_for_issue_newest_first(seeded: Database) -> None:
    async with scoped(seeded) as q:
        runs = await q.runs_for_issue(2)
        assert await q.runs_for_issue(99) == []
    assert [run.run_id for run in runs] == ["r1", "r2"]
    assert all(isinstance(run, RunRow) for run in runs)
    assert (runs[0].outcome, runs[0].ended_at) == (None, None)
    assert (runs[1].outcome, runs[1].error, runs[1].cost_usd) == ("failed", "turn_failed: x", 0.1)
    assert runs[1].ended_at == runs[1].started_at + timedelta(seconds=30)


async def test_recent_events_newest_first_and_limited(seeded: Database) -> None:
    async with scoped(seeded) as q:
        events = await q.recent_events(3)
        everything = await q.recent_events(100)
    assert [event.kind for event in events] == ["blocked", "run_ended", "run_started"]
    assert len(everything) == 5
    assert all(isinstance(event, EventRow) for event in events)
    assert events[0].payload["reason"] == "stuck"
    assert (events[0].issue_number, events[0].run_id) == (2, None)
    assert events[1].run_id == "r2"


async def test_snapshot_is_none_until_written(seeded: Database, db_url: str) -> None:
    async with scoped(seeded) as q:
        assert await q.snapshot() is None
    store = PostgresStore(db_url, repo=REPO, labels=GitHubLabels())
    await store.connect()
    await store.write_snapshot(NOW, {"tick_count": 3})
    await store.close()
    async with scoped(seeded) as q:
        row = await q.snapshot()
    assert row is not None
    assert (row.at, row.data) == (NOW, {"tick_count": 3})
    assert row.written_at >= NOW


async def test_issue_by_number(seeded: Database) -> None:
    async with scoped(seeded) as q:
        row = await q.issue(3)
        assert await q.issue(99) is None
    assert isinstance(row, IssueRow)
    assert (row.number, row.identifier, row.state) == (3, "repo-3", "review")


async def test_events_for_issue_newest_first_and_limited(seeded: Database) -> None:
    async with scoped(seeded) as q:
        events = await q.events_for_issue(2, 10)
        two = await q.events_for_issue(2, 2)
        assert await q.events_for_issue(99, 10) == []
    assert [event.kind for event in events] == [
        "blocked",
        "run_ended",
        "run_started",
        "run_started",
    ]
    assert [event.run_id for event in events] == [None, "r2", "r2", "r1"]
    assert [event.kind for event in two] == ["blocked", "run_ended"]
    assert all(event.issue_number == 2 for event in events)


async def test_turn_summaries_for_issue_newest_run_first(with_turns: Database) -> None:
    async with scoped(with_turns) as q:
        turns = await q.turn_summaries_for_issue(2)
        assert await q.turn_summaries_for_issue(99) == []
    assert [(turn.run_id, turn.turn_number) for turn in turns] == [("r2", 1), ("r2", 2), ("r0", 1)]
    assert all(isinstance(turn, TurnSummaryRow) for turn in turns)
    assert not any(isinstance(turn, TurnRow) for turn in turns)
    first = turns[0]
    assert (first.model, first.subtype, first.num_turns, first.truncated) == (
        "claude-opus-5",
        "success",
        19,
        False,
    )
    assert (first.cost_usd, first.prompt_bytes, first.stream_bytes, first.stderr_bytes) == (
        0.8976,
        28,
        18,
        14,
    )
    assert (turns[1].subtype, turns[1].num_turns, turns[1].truncated) == (None, None, True)
    assert turns[2].model == "claude-sonnet-5"
    assert not hasattr(first, "stream")


async def test_turn_returns_the_whole_row_or_none(with_turns: Database) -> None:
    async with scoped(with_turns) as q:
        turn = await q.turn("r2", 2)
        assert await q.turn("r2", 9) is None
        assert await q.turn("nope", 1) is None
    assert isinstance(turn, TurnRow)
    assert (turn.run_id, turn.turn_number, turn.subtype) == ("r2", 2, None)
    assert (turn.prompt, turn.stream, turn.stderr) == (
        "You are working on issue #2.",
        '{"type":"result"}\n',
        "warning: slow\n",
    )
    assert turn.captured_at is not None


async def test_state_counts_follow_the_kanban_predicate(seeded: Database) -> None:
    async with scoped(seeded) as q:
        counts = await q.state_counts()
    assert counts == {"todo": 2, "in_progress": 1, "review": 1, "rework": 0, "complete": 3}
    assert list(counts) == ["todo", "in_progress", "review", "rework", "complete"]


async def test_issues_by_state_skips_an_unknown_role(seeded: Database, db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await conn.execute(
            """
            INSERT INTO issues (repo, number, identifier, title, state, state_label,
                                github_state, url, created_at, updated_at, seen_at)
            VALUES ('example/repo', 77, 'repo-77', 'Mystery', 'mystery', 'issuebot/mystery',
                    'open', 'https://github.com/example/repo/issues/77', now(), now(), now())
            """
        )
    finally:
        await conn.close()
    async with scoped(seeded) as q:
        groups = await q.issues_by_state()
        counts = await q.state_counts()
    assert list(groups) == ["todo", "in_progress", "review", "rework", "complete"]
    assert 77 not in {row.number for rows in groups.values() for row in rows}
    assert list(counts) == ["todo", "in_progress", "review", "rework", "complete"]
    assert counts == {"todo": 2, "in_progress": 1, "review": 1, "rework": 0, "complete": 3}


async def test_queries_on_an_empty_schema_report_a_database_error(db_url: str) -> None:
    with pytest.raises(StoreError, match="UndefinedTable"):
        async with scoped(Database(db_url)) as q:
            await q.snapshot()


# --- one database, many repositories ----------------------------------------------------------


async def test_scoped_reads_see_only_their_own_repository(
    seeded: Database, db_url: str, make_issue: Callable[..., Issue]
) -> None:
    """Spec §9: every ``RepoQueries`` method answers for its own repository and no other.

    The other repository is seeded richly enough -- an open issue, a closed ``complete`` one,
    a finished run with tokens and a captured turn, three events and a snapshot -- that a
    missing ``repo`` predicate in any one query would change a number below rather than pass.
    """
    other = PostgresStore(db_url, repo="example/other", labels=GitHubLabels())
    await other.connect()
    try:
        await other.upsert_issues(
            [
                IssueSnapshot(issue=make_issue(number=1, title="Elsewhere"), seen_at=NOW),
                IssueSnapshot(
                    issue=make_issue(
                        number=2,
                        identifier="other-2",
                        title="Theirs closed",
                        state=StateLabel.COMPLETE,
                        state_labels=("issuebot/complete",),
                        labels=("issuebot/complete",),
                        github_state="closed",
                        closed_at=NOW - HOUR,
                        updated_at=NOW - HOUR,
                    ),
                    seen_at=NOW,
                ),
            ]
        )
        await other.apply_event(
            RunStarted(
                issue_number=1,
                issue_identifier="other-1",
                run_id="other-run",
                attempt=1,
                session_id="s",
                workspace_path="/w",
                at=NOW - HOUR,
            )
        )
        await other.apply_event(
            run_ended(
                "other-run",
                1,
                NOW - HOUR + timedelta(seconds=30),
                issue_identifier="other-1",
                input_tokens=700,
                output_tokens=70,
                cost_usd=7.0,
            ),
            turns=[capture(1, model="claude-elsewhere")],
        )
        await other.apply_event(
            StateChanged(
                issue_number=1,
                issue_identifier="other-1",
                from_label="issuebot/review",
                to_label="issuebot/todo",  # keeps issue 1 on the column make_issue put it on
                actor="human",
                at=NOW,
            )
        )
        await other.write_snapshot(NOW, {"tick_count": 99})
    finally:
        await other.close()
    async with scoped(seeded, "example/other") as theirs, scoped(seeded) as ours:
        # issues_for_state
        assert sorted(row.title for row in await theirs.issues_for_state(None)) == [
            "Elsewhere",
            "Theirs closed",
        ]
        assert "Elsewhere" not in [row.title for row in await ours.issues_for_state(None)]
        # issues_by_state: only their two cards, on their own columns
        board = await theirs.issues_by_state()
        assert [(role, [row.title for row in rows]) for role, rows in board.items() if rows] == [
            ("todo", ["Elsewhere"]),
            ("complete", ["Theirs closed"]),
        ]
        assert {"Elsewhere", "Theirs closed"}.isdisjoint(
            row.title for rows in (await ours.issues_by_state()).values() for row in rows
        )
        # state_counts: the board's headers, uncapped, per repository
        assert await theirs.state_counts() == {
            "todo": 1,
            "in_progress": 0,
            "review": 0,
            "rework": 0,
            "complete": 1,
        }
        assert await ours.state_counts() == {
            "todo": 2,
            "in_progress": 1,
            "review": 1,
            "rework": 0,
            "complete": 3,
        }
        # closed_count: theirs closed an hour ago too, and must not reach ours
        assert await theirs.closed_count(DAY) == 1
        assert await ours.closed_count(DAY) == 1
        # runs_for_issue and runs_count
        assert [run.run_id for run in await theirs.runs_for_issue(1)] == ["other-run"]
        assert await theirs.runs_count(DAY) == 1
        assert "other-run" not in [run.run_id for run in await ours.runs_for_issue(1)]
        assert await ours.runs_count(DAY) == 1
        # run_totals: their 7.0 must not land in our window's cost
        theirs_totals = await theirs.run_totals(DAY)
        assert (theirs_totals.total_tokens, theirs_totals.cost_usd) == (770, pytest.approx(7.0))
        assert (await ours.run_totals(7 * DAY)).cost_usd == pytest.approx(0.1)
        # daily_series: two days of buckets, one close and one run each side
        theirs_days = await theirs.daily_series(2)
        assert (sum(p.closed for p in theirs_days), sum(p.runs for p in theirs_days)) == (1, 1)
        ours_days = await ours.daily_series(2)
        assert (sum(p.closed for p in ours_days), sum(p.runs for p in ours_days)) == (1, 1)
        # issue: the same number in both repositories is two different issues
        assert (await theirs.issue(1)).title == "Elsewhere"  # type: ignore[union-attr]
        assert (await ours.issue(1)).title == "Issue 1"  # type: ignore[union-attr]
        # recent_events and events_for_issue
        assert [event.kind for event in await theirs.recent_events(50)] == [
            "state_changed",
            "run_ended",
            "run_started",
        ]
        assert "other-run" not in [event.run_id for event in await ours.recent_events(50)]
        assert len(await theirs.events_for_issue(1, 50)) == 3
        assert await ours.events_for_issue(1, 50) == []
        # turn_summaries_for_issue and turn: run_turns is scoped by its own repo column
        assert [turn.model for turn in await theirs.turn_summaries_for_issue(1)] == [
            "claude-elsewhere"
        ]
        assert await ours.turn_summaries_for_issue(1) == []
        assert (await theirs.turn("other-run", 1)).model == "claude-elsewhere"  # type: ignore[union-attr]
        assert await ours.turn("other-run", 1) is None
        # snapshot
        assert (await theirs.snapshot()).data == {"tick_count": 99}  # type: ignore[union-attr]


async def test_repos_lists_registrations_by_name_and_snapshots_by_repo(
    seeded: Database, db_url: str
) -> None:
    store = PostgresStore(db_url, repo=REPO, labels=GitHubLabels())
    await store.connect()
    try:
        await store.write_snapshot(NOW, {"tick_count": 3})
    finally:
        await store.close()
    await seeded.register_repo("zeta/last", GitHubLabels(), "/configs/z.md")
    await seeded.register_repo("alpha/first", GitHubLabels(review="issuebot/check"), None)
    await seeded.register_repo("zeta/last", GitHubLabels(), "/configs/z2.md")  # re-register
    async with seeded.queries() as queries:
        rows = await queries.repos()
        assert [(r.repo, r.workflow_path) for r in rows] == [
            ("alpha/first", None),
            ("zeta/last", "/configs/z2.md"),
        ]
        assert rows[0].labels["review"] == "issuebot/check"
        assert rows[1].registered_at <= rows[1].seen_at
        assert await queries.repo("nobody/here") is None
        assert (await queries.repo("alpha/first")).labels["todo"] == "issuebot/todo"  # type: ignore[union-attr]
        snapshots = await queries.snapshots()
        assert set(snapshots) == {REPO}  # only the worker that wrote one, not every registration
        assert snapshots[REPO].data == {"tick_count": 3}


# --- the admission ledger's seed (#112) ---------------------------------------------------


@pytest.fixture
async def with_ledger_history(db_url: str) -> AsyncIterator[Database]:
    """Run histories of every shape the seed has to read, plus two it has to skip."""
    await migrate(db_url)
    store = PostgresStore(db_url, repo=REPO, labels=GitHubLabels())
    await store.connect()

    async def run(
        run_id: str, number: int, ago: timedelta, outcome: str | None, **overrides: Any
    ) -> None:
        await store.apply_event(
            RunStarted(
                issue_number=number,
                issue_identifier=f"repo-{number}",
                run_id=run_id,
                attempt=1,
                session_id="s",
                workspace_path="/w",
                at=NOW - ago,
            )
        )
        if outcome is not None:
            await store.apply_event(
                run_ended(
                    run_id,
                    number,
                    NOW - ago + timedelta(minutes=1),
                    outcome=outcome,
                    **overrides,
                )
            )

    # 20: three failures in a row, and nothing has cleared them.
    for n, ago in enumerate((5 * HOUR, 4 * HOUR, 3 * HOUR)):
        await run(f"a{n}", 20, ago, "failed", turns=2, cost_usd=0.5)
    # 21: a failure, then a run that succeeded, then one more failure.
    await run("b0", 21, 5 * HOUR, "failed")
    await run("b1", 21, 4 * HOUR, "succeeded")
    await run("b2", 21, 3 * HOUR, "failed")
    # 22: two failures, then the escape that handed it to a human, then one more failure.
    await run("c0", 22, 5 * HOUR, "failed")
    await run("c1", 22, 4 * HOUR, "failed")
    await store.apply_event(
        Blocked(issue_number=22, issue_identifier="repo-22", reason="stuck", at=NOW - 3 * HOUR)
    )
    await run("c2", 22, 2 * HOUR, "failed")
    # 23: a release, not a fault of the issue's -- a shutdown, a move, a closed issue.
    await run("d0", 23, HOUR, "cancelled")
    # 24: still running, so it has not cost anything yet that anybody can count.
    await run("e0", 24, HOUR, None)
    # 25: older than the window the seed reads.
    await run("f0", 25, timedelta(days=120), "failed")
    await store.close()

    other = PostgresStore(db_url, repo="example/other", labels=GitHubLabels())
    await other.connect()
    await other.apply_event(
        RunStarted(
            issue_number=20,
            issue_identifier="other-20",
            run_id="x0",
            attempt=1,
            session_id="s",
            workspace_path="/w",
            at=NOW - HOUR,
        )
    )
    await other.apply_event(run_ended("x0", 20, NOW, outcome="failed"))
    await other.close()
    yield Database(db_url)


async def test_issue_ledgers_reads_the_chain_and_what_it_cost(
    with_ledger_history: Database,
) -> None:
    async with scoped(with_ledger_history) as queries:
        rows = {row.identifier: row for row in await queries.issue_ledgers()}
    assert set(rows) == {"repo-20", "repo-21", "repo-22", "repo-23"}
    assert (rows["repo-20"].failures, rows["repo-20"].runs) == (3, 3)
    assert (rows["repo-20"].turns, rows["repo-20"].cost_usd) == (6, 1.5)
    # A run that succeeded ends the chain; the runs before it are still spend.
    assert (rows["repo-21"].failures, rows["repo-21"].runs) == (1, 3)
    # So does the blocked escape, which is how the README's "fix it, then relabel" works.
    assert (rows["repo-22"].failures, rows["repo-22"].runs) == (1, 3)
    # A cancelled run is a release, not a fault: it costs the budget but not the chain.
    assert (rows["repo-23"].failures, rows["repo-23"].runs) == (0, 1)
    assert rows["repo-20"].last_run_at is not None


async def test_issue_ledgers_is_bounded_by_repository_window_and_limit(
    with_ledger_history: Database,
) -> None:
    async with scoped(with_ledger_history) as queries:
        assert [row.identifier for row in await queries.issue_ledgers(limit=1)] == ["repo-23"]
        # A run older than the window is not live history.
        assert "repo-25" not in {row.identifier for row in await queries.issue_ledgers()}
        assert "repo-25" in {row.identifier for row in await queries.issue_ledgers(days=365)}
    async with scoped(with_ledger_history, "example/other") as queries:
        assert [row.identifier for row in await queries.issue_ledgers()] == ["other-20"]


async def test_issue_ledgers_on_an_empty_store_is_empty(db_url: str) -> None:
    await migrate(db_url)
    async with scoped(Database(db_url)) as queries:
        assert await queries.issue_ledgers() == []
