"""The reads: view models (Phase 7's), the global Queries and a RepoQueries per repository."""

from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from issuebot.github import StateLabel

BOARD_LIMIT = 5
ISSUE_LIST_LIMIT = 200
MAX_WINDOW_DAYS = 365


@dataclass(frozen=True, kw_only=True, slots=True)
class IssueRow:
    number: int
    identifier: str
    title: str
    state: str | None
    state_label: str | None
    github_state: str
    url: str
    labels: list[str]
    pr_number: int | None
    pr_url: str | None
    pr_state: str | None
    pr_merged_at: datetime | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    seen_at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class RunRow:
    run_id: str
    issue_number: int
    issue_identifier: str
    attempt: int
    session_id: str | None
    started_at: datetime
    ended_at: datetime | None
    outcome: str | None
    error: str | None
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_s: float | None
    workspace_path: str | None
    log_dir: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class RunTotals:
    """What the runs of one window cost, summed; zeroes when the window holds no run."""

    input_tokens: int
    output_tokens: int
    cost_usd: float

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, kw_only=True, slots=True)
class EventRow:
    id: int
    at: datetime
    kind: str
    issue_number: int | None
    run_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True, kw_only=True, slots=True)
class DailyPoint:
    day: date
    closed: int
    runs: int


@dataclass(frozen=True, kw_only=True, slots=True)
class SnapshotRow:
    at: datetime
    written_at: datetime
    data: dict[str, Any]


@dataclass(frozen=True, kw_only=True, slots=True)
class TurnSummaryRow:
    """Every ``run_turns`` column except the three texts (prompt, stream, stderr)."""

    run_id: str
    turn_number: int
    captured_at: datetime
    model: str | None
    subtype: str | None
    is_error: bool | None
    num_turns: int | None
    input_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    duration_ms: int | None
    result_text: str | None
    prompt_bytes: int
    stream_bytes: int
    stream_lines: int
    omitted_lines: int
    stderr_bytes: int
    truncated: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class TurnRow(TurnSummaryRow):
    """The whole ``run_turns`` row but its ``repo``, which is in the request."""

    prompt: str
    stream: str
    stderr: str


@dataclass(frozen=True, kw_only=True, slots=True)
class RepoRow:
    """One registered worker: what the dropdown lists and where the board's labels come from.
    ``labels`` is the stored mapping; the web validates it per request (spec §7, §8)."""

    repo: str
    labels: dict[str, Any]
    workflow_path: str | None
    registered_at: datetime
    seen_at: datetime


# Explicit column lists rather than SELECT *: every table now carries a repo column the
# row types do not, and the repository is in the request, not the row.
ISSUE_COLUMNS = ", ".join(f.name for f in fields(IssueRow))
RUN_COLUMNS = ", ".join(f.name for f in fields(RunRow))
EVENT_COLUMNS = ", ".join(f.name for f in fields(EventRow))
SUMMARY_COLUMNS = ", ".join(f"t.{f.name}" for f in fields(TurnSummaryRow))
TURN_COLUMNS = ", ".join(f"t.{f.name}" for f in fields(TurnRow))


CLOSED_COUNT = """
SELECT count(*) AS n FROM issues
WHERE repo = %(repo)s AND state = 'complete' AND closed_at >= now() - %(window)s
"""

RUNS_COUNT = """
SELECT count(*) AS n FROM runs WHERE repo = %(repo)s AND started_at >= now() - %(window)s
"""

RUN_TOTALS = """
SELECT coalesce(sum(input_tokens), 0) AS input_tokens,
       coalesce(sum(output_tokens), 0) AS output_tokens,
       coalesce(sum(cost_usd), 0) AS cost_usd
FROM runs WHERE repo = %(repo)s AND started_at >= now() - %(window)s
"""

DAILY_SERIES = """
WITH days AS (
    SELECT generate_series(
        date_trunc('day', now()) - (%(days)s - 1) * interval '1 day',
        date_trunc('day', now()),
        interval '1 day'
    ) AS day
)
SELECT day::date AS day,
       (SELECT count(*) FROM issues
        WHERE repo = %(repo)s AND state = 'complete'
          AND closed_at >= day AND closed_at < day + interval '1 day') AS closed,
       (SELECT count(*) FROM runs
        WHERE repo = %(repo)s AND started_at >= day AND started_at < day + interval '1 day')
           AS runs
FROM days ORDER BY day
"""

OPEN_ISSUES = f"""
SELECT {ISSUE_COLUMNS} FROM issues
WHERE repo = %(repo)s AND github_state = 'open' AND state IS NOT NULL
ORDER BY updated_at DESC, number DESC
"""

COMPLETE_ISSUES = f"""
SELECT {ISSUE_COLUMNS} FROM issues
WHERE repo = %(repo)s AND github_state = 'closed' AND state = 'complete'
ORDER BY closed_at DESC NULLS LAST, number DESC LIMIT %(limit)s
"""

# The list page's query. `roles` carries both jobs: it filters to the column asked for, and
# it is what keeps a role this issuebot does not know off the page, the way the board does.
# One ordering for every column, because the page mixes them: a closed issue sorts on when
# it closed, an open one on when it last moved.
ISSUE_LIST = f"""
SELECT {ISSUE_COLUMNS} FROM issues
WHERE repo = %(repo)s AND state = ANY(%(roles)s) AND (github_state = 'open' OR state = 'complete')
ORDER BY coalesce(closed_at, updated_at) DESC, number DESC LIMIT %(limit)s
"""

RUNS_FOR_ISSUE = f"""
SELECT {RUN_COLUMNS} FROM runs WHERE repo = %(repo)s AND issue_number = %(number)s
ORDER BY started_at DESC, run_id DESC
"""

RECENT_EVENTS = f"""
SELECT {EVENT_COLUMNS} FROM events WHERE repo = %(repo)s ORDER BY id DESC LIMIT %(limit)s
"""

SNAPSHOT = "SELECT at, written_at, data FROM runtime_snapshot WHERE repo = %(repo)s"

SNAPSHOTS = "SELECT repo, at, written_at, data FROM runtime_snapshot"

REPOS = "SELECT repo, labels, workflow_path, registered_at, seen_at FROM repos ORDER BY repo"

REPO = "SELECT repo, labels, workflow_path, registered_at, seen_at FROM repos WHERE repo = %(repo)s"

ISSUE = f"SELECT {ISSUE_COLUMNS} FROM issues WHERE repo = %(repo)s AND number = %(number)s"

EVENTS_FOR_ISSUE = f"""
SELECT {EVENT_COLUMNS} FROM events WHERE repo = %(repo)s AND issue_number = %(number)s
ORDER BY id DESC LIMIT %(limit)s
"""

# A run and its turns are the same repository's by construction (0004: run_turns references
# runs on (repo, run_id)), so the join is on both and the predicate is the turn's own column.
TURN_SUMMARIES_FOR_ISSUE = f"""
SELECT {SUMMARY_COLUMNS} FROM run_turns t
JOIN runs r ON r.repo = t.repo AND r.run_id = t.run_id
WHERE t.repo = %(repo)s AND r.issue_number = %(number)s
ORDER BY r.started_at DESC, r.run_id DESC, t.turn_number
"""

TURN = f"""
SELECT {TURN_COLUMNS} FROM run_turns t
WHERE t.repo = %(repo)s AND t.run_id = %(run_id)s AND t.turn_number = %(turn_number)s
"""

STATE_COUNTS = """
SELECT state, count(*) AS n FROM issues
WHERE repo = %(repo)s AND state IS NOT NULL AND (github_state = 'open' OR state = 'complete')
GROUP BY state
"""


class _Reader:
    """One connection and the two helpers every read goes through."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def _count(self, query: str, params: dict[str, Any]) -> int:
        rows = await self._rows(query, params)
        return int(rows[0]["n"])

    async def _rows(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchall()


class Queries(_Reader):
    """The repository-free reads over one connection, and the way to a scoped view."""

    async def repos(self) -> list[RepoRow]:
        """Every registered worker, by name."""
        return [RepoRow(**row) for row in await self._rows(REPOS)]

    async def repo(self, name: str) -> RepoRow | None:
        rows = await self._rows(REPO, {"repo": name})
        return RepoRow(**rows[0]) if rows else None

    async def snapshots(self) -> dict[str, SnapshotRow]:
        """Every worker's latest snapshot, keyed by repository (for /healthz)."""
        return {
            row["repo"]: SnapshotRow(at=row["at"], written_at=row["written_at"], data=row["data"])
            for row in await self._rows(SNAPSHOTS)
        }

    def scoped(self, repo: str) -> RepoQueries:
        """The same connection with ``repo`` bound into every predicate."""
        return RepoQueries(self._conn, repo)


class RepoQueries(_Reader):
    """Read-only queries for one repository; every method is one round trip or two.

    Binding the repository into the object rather than adding a parameter to every method
    means a handler cannot forget the predicate.
    """

    def __init__(self, conn: AsyncConnection, repo: str) -> None:
        super().__init__(conn)
        self.repo = repo

    def _params(self, **params: Any) -> dict[str, Any]:
        return {"repo": self.repo, **params}

    async def closed_count(self, window: timedelta) -> int:
        """Issues in ``complete`` whose GitHub ``closed_at`` falls inside the window."""
        return await self._count(CLOSED_COUNT, self._params(window=window))

    async def runs_count(self, window: timedelta) -> int:
        """Worker sessions started inside the window ("agents spun up")."""
        return await self._count(RUNS_COUNT, self._params(window=window))

    async def run_totals(self, window: timedelta) -> RunTotals:
        """Tokens and cost summed over the same runs ``runs_count`` counts."""
        rows = await self._rows(RUN_TOTALS, self._params(window=window))
        row = rows[0]
        return RunTotals(
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cost_usd=float(row["cost_usd"]),
        )

    async def daily_series(self, days: int) -> list[DailyPoint]:
        """One point per UTC day for the last ``days`` days, today last, zero-filled."""
        rows = await self._rows(DAILY_SERIES, self._params(days=days))
        return [DailyPoint(day=row["day"], closed=row["closed"], runs=row["runs"]) for row in rows]

    async def issues_by_state(self) -> dict[str, list[IssueRow]]:
        """The board's columns, newest first and at most ``BOARD_LIMIT`` rows each.

        Every column is capped, not just complete: one long column sets the height of the
        whole board, and an open column falls behind the same way a closed one piles up.
        What the column headers count is ``state_counts``, which is not capped.
        """
        groups: dict[str, list[IssueRow]] = {role.value: [] for role in StateLabel}
        for row in await self._rows(OPEN_ISSUES, self._params()):
            rows = groups.get(row["state"])  # a role this issuebot does not know is on no column
            if rows is not None and len(rows) < BOARD_LIMIT:
                rows.append(IssueRow(**row))
        for row in await self._rows(COMPLETE_ISSUES, self._params(limit=BOARD_LIMIT)):
            groups[StateLabel.COMPLETE.value].append(IssueRow(**row))
        return groups

    async def issues_for_state(self, state: str | None) -> list[IssueRow]:
        """One column in full up to ``ISSUE_LIST_LIMIT``, or every column when ``state`` is None."""
        known = [role.value for role in StateLabel]
        if state is None:
            roles = known
        elif state in known:
            roles = [state]
        else:  # a role this issuebot does not know is on no column, so it lists nothing
            return []
        rows = await self._rows(ISSUE_LIST, self._params(roles=roles, limit=ISSUE_LIST_LIMIT))
        return [IssueRow(**row) for row in rows]

    async def state_counts(self) -> dict[str, int]:
        """Issues per StateLabel value over the Kanban's predicate; every role key present."""
        counts = {role.value: 0 for role in StateLabel}
        for row in await self._rows(STATE_COUNTS, self._params()):
            if row["state"] in counts:
                counts[row["state"]] = int(row["n"])
        return counts

    async def issue(self, number: int) -> IssueRow | None:
        rows = await self._rows(ISSUE, self._params(number=number))
        return IssueRow(**rows[0]) if rows else None

    async def events_for_issue(self, number: int, limit: int) -> list[EventRow]:
        """Newest first."""
        rows = await self._rows(EVENTS_FOR_ISSUE, self._params(number=number, limit=limit))
        return [EventRow(**row) for row in rows]

    async def turn_summaries_for_issue(self, number: int) -> list[TurnSummaryRow]:
        """Captured turns of the issue's runs: newest run first (runs.started_at), then turn."""
        rows = await self._rows(TURN_SUMMARIES_FOR_ISSUE, self._params(number=number))
        return [TurnSummaryRow(**row) for row in rows]

    async def turn(self, run_id: str, turn_number: int) -> TurnRow | None:
        rows = await self._rows(TURN, self._params(run_id=run_id, turn_number=turn_number))
        return TurnRow(**rows[0]) if rows else None

    async def runs_for_issue(self, number: int) -> list[RunRow]:
        """Newest first."""
        rows = await self._rows(RUNS_FOR_ISSUE, self._params(number=number))
        return [RunRow(**row) for row in rows]

    async def recent_events(self, limit: int) -> list[EventRow]:
        """Newest first."""
        rows = await self._rows(RECENT_EVENTS, self._params(limit=limit))
        return [EventRow(**row) for row in rows]

    async def snapshot(self) -> SnapshotRow | None:
        rows = await self._rows(SNAPSHOT, self._params())
        return SnapshotRow(**rows[0]) if rows else None
