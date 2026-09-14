"""Stand-ins for issuebot.db.Database and what it hands out; shared by the CLI and web tests."""

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from issuebot.agent.turnlog import TurnCapture
from issuebot.config import GitHubLabels
from issuebot.db import DatabaseError, MigrationResult, Probe
from issuebot.db.connection import NOT_A_URL, is_postgres_url
from issuebot.db.queries import (
    DailyPoint,
    EventRow,
    IssueRow,
    RepoRow,
    RunRow,
    RunTotals,
    SnapshotRow,
    TurnRow,
    TurnSummaryRow,
)
from issuebot.db.store import IssueSnapshot
from issuebot.events import Event
from issuebot.github import StateLabel

DB_URL = "postgresql://issuebot:s3cret@db.example:5432/issuebot"
PROBE_OK = Probe(server_version="PostgreSQL 18.1", schema_version=2, latest_version=2)


class FakeStore:
    """The sink's store: records every write."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.turns: list[list[TurnCapture]] = []
        self.issues: list[list[IssueSnapshot]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    async def apply_event(self, event: Event, turns: Sequence[TurnCapture] = ()) -> None:
        self.events.append(event)
        self.turns.append(list(turns))

    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None:
        self.issues.append(list(issues))

    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None:
        self.snapshots.append(dict(data))


class FakeQueries:
    """Canned answers for the global reads, and the canned data a scoped view reads through.

    ``error`` makes every read on either object raise it. The data and the recording
    attributes live here, on the object a test configures; the per-repository reads live on
    ``FakeRepoQueries``, exactly as the real split does, so a caller that forgets to scope
    fails here the way it would against a real database.
    """

    def __init__(self) -> None:
        self.snapshot_row: SnapshotRow | None = None
        self.closed = {1: 0, 7: 0}
        self.runs = {1: 0, 7: 0}
        zero = RunTotals(input_tokens=0, output_tokens=0, cost_usd=0.0)
        self.totals = {1: zero, 7: zero}
        self.groups: dict[str, list[IssueRow]] = {role.value: [] for role in StateLabel}
        self.counts: dict[str, int] = {role.value: 0 for role in StateLabel}
        self.issue_list: list[IssueRow] = []
        self.state_asked: str | None = None
        self.series: list[DailyPoint] = []
        self.issue_rows: dict[int, IssueRow] = {}
        self.runs_by_issue: dict[int, list[RunRow]] = {}
        self.events_by_issue: dict[int, list[EventRow]] = {}
        self.turns_by_issue: dict[int, list[TurnSummaryRow]] = {}
        self.turn_rows: dict[tuple[str, int], TurnRow] = {}
        self.error: DatabaseError | None = None
        self.days_asked: int | None = None
        self.calls: list[str] = []
        self.repo_rows: dict[str, RepoRow] = {}
        self.snapshot_rows: dict[str, SnapshotRow] = {}  # per repo; snapshot_row is the default
        self.scoped_repos: list[str] = []

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.error is not None:
            raise self.error

    async def repos(self) -> list[RepoRow]:
        self._check("repos")
        return [self.repo_rows[name] for name in sorted(self.repo_rows)]

    async def repo(self, name: str) -> RepoRow | None:
        self._check("repo")
        return self.repo_rows.get(name)

    async def snapshots(self) -> dict[str, SnapshotRow]:
        self._check("snapshots")
        rows = {name: self.snapshot_rows.get(name, self.snapshot_row) for name in self.repo_rows}
        return {name: row for name, row in rows.items() if row is not None}

    def scoped(self, repo: str) -> FakeRepoQueries:
        self.scoped_repos.append(repo)
        return FakeRepoQueries(self, repo)


class FakeRepoQueries:
    """Stands in for RepoQueries: the fourteen per-repository reads over the parent's data."""

    def __init__(self, parent: FakeQueries, repo: str) -> None:
        self._parent = parent
        self.repo = repo

    def _check(self, name: str) -> None:
        self._parent._check(name)

    async def snapshot(self) -> SnapshotRow | None:
        self._check("snapshot")
        parent = self._parent
        if self.repo in parent.snapshot_rows:
            return parent.snapshot_rows[self.repo]
        return parent.snapshot_row

    async def closed_count(self, window: timedelta) -> int:
        self._check("closed_count")
        return self._parent.closed[window.days]

    async def runs_count(self, window: timedelta) -> int:
        self._check("runs_count")
        return self._parent.runs[window.days]

    async def run_totals(self, window: timedelta) -> RunTotals:
        self._check("run_totals")
        return self._parent.totals[window.days]

    async def issues_by_state(self) -> dict[str, list[IssueRow]]:
        self._check("issues_by_state")
        return self._parent.groups

    async def state_counts(self) -> dict[str, int]:
        self._check("state_counts")
        return self._parent.counts

    async def issues_for_state(self, state: str | None) -> list[IssueRow]:
        self._check("issues_for_state")
        self._parent.state_asked = state
        return self._parent.issue_list

    async def daily_series(self, days: int) -> list[DailyPoint]:
        self._check("daily_series")
        self._parent.days_asked = days
        return self._parent.series

    async def issue(self, number: int) -> IssueRow | None:
        self._check("issue")
        return self._parent.issue_rows.get(number)

    async def runs_for_issue(self, number: int) -> list[RunRow]:
        self._check("runs_for_issue")
        return self._parent.runs_by_issue.get(number, [])

    async def events_for_issue(self, number: int, limit: int) -> list[EventRow]:
        self._check("events_for_issue")
        return self._parent.events_by_issue.get(number, [])[:limit]

    async def recent_events(self, limit: int) -> list[EventRow]:
        self._check("recent_events")
        events = [event for rows in self._parent.events_by_issue.values() for event in rows]
        return sorted(events, key=lambda event: event.id, reverse=True)[:limit]

    async def turn_summaries_for_issue(self, number: int) -> list[TurnSummaryRow]:
        self._check("turn_summaries_for_issue")
        return self._parent.turns_by_issue.get(number, [])

    async def turn(self, run_id: str, turn_number: int) -> TurnRow | None:
        self._check("turn")
        return self._parent.turn_rows.get((run_id, turn_number))


class FakeListener:
    def __init__(self, on_notify: Callable[[], None], repo: str | None = None) -> None:
        self.on_notify = on_notify
        self.repo = repo
        self.started = False
        self.closed = False
        self.close_error: Exception | None = None

    def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeDatabase:
    """Stands in for issuebot.db.Database: one instance per test with canned results."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.migrations = 0
        self.migrate_result = MigrationResult(applied=(), version=2)
        self.migrate_error: DatabaseError | None = None
        self.probe_result = PROBE_OK
        self.probe_error: DatabaseError | None = None
        self.queries_obj = FakeQueries()
        self.store_obj = FakeStore()
        self.labels: GitHubLabels | None = None
        self.repo: str | None = None
        self.registrations: list[tuple[str, GitHubLabels, str | None]] = []
        self.register_error: DatabaseError | None = None
        self.listeners: list[FakeListener] = []
        self.listener_close_error: Exception | None = None
        self.notified = 0
        self.notified_repos: list[str | None] = []
        self.notify_error: DatabaseError | None = None
        self.opened = 0
        # Raised on opening a connection, before any query: what an unreachable server does.
        self.queries_error: DatabaseError | None = None

    def factory(self, url: str) -> FakeDatabase:
        # The real facade refuses anything but a postgresql:// URL before it records it (#105).
        if not is_postgres_url(url):
            raise DatabaseError(NOT_A_URL)
        self.urls.append(url)
        return self

    @property
    def description(self) -> str:
        return "postgresql://issuebot@db.example:5432/issuebot"

    async def migrate(self) -> MigrationResult:
        self.migrations += 1
        if self.migrate_error is not None:
            raise self.migrate_error
        return self.migrate_result

    async def probe(self) -> Probe:
        if self.probe_error is not None:
            raise self.probe_error
        return self.probe_result

    @asynccontextmanager
    async def queries(self) -> AsyncIterator[FakeQueries]:
        self.opened += 1
        if self.queries_error is not None:
            raise self.queries_error
        yield self.queries_obj

    async def register_repo(
        self, repo: str, labels: GitHubLabels, workflow_path: str | None
    ) -> None:
        if self.register_error is not None:
            raise self.register_error
        self.registrations.append((repo, labels, workflow_path))

    def store(self, labels: GitHubLabels, repo: str) -> FakeStore:
        self.labels = labels
        self.repo = repo
        return self.store_obj

    def listener(self, on_notify: Callable[[], None], *, repo: str | None = None) -> FakeListener:
        listener = FakeListener(on_notify, repo)
        listener.close_error = self.listener_close_error
        self.listeners.append(listener)
        return listener

    async def notify_refresh(self, repo: str | None = None) -> None:
        if self.notify_error is not None:
            raise self.notify_error
        self.notified += 1
        self.notified_repos.append(repo)
