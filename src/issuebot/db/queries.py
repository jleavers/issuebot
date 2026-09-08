"""The reads: view models (Phase 7's) and a Queries object bound to one connection."""

from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from issuebot.github import StateLabel

COMPLETE_LIMIT = 50
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
    """The whole ``run_turns`` row."""

    prompt: str
    stream: str
    stderr: str


SUMMARY_COLUMNS = ", ".join(f"t.{f.name}" for f in fields(TurnSummaryRow))


CLOSED_COUNT = """
SELECT count(*) AS n FROM issues
WHERE state = 'complete' AND closed_at >= now() - %(window)s
"""

RUNS_COUNT = "SELECT count(*) AS n FROM runs WHERE started_at >= now() - %(window)s"

RUN_TOTALS = """
SELECT coalesce(sum(input_tokens), 0) AS input_tokens,
       coalesce(sum(output_tokens), 0) AS output_tokens,
       coalesce(sum(cost_usd), 0) AS cost_usd
FROM runs WHERE started_at >= now() - %(window)s
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
        WHERE state = 'complete' AND closed_at >= day AND closed_at < day + interval '1 day')
           AS closed,
       (SELECT count(*) FROM runs
        WHERE started_at >= day AND started_at < day + interval '1 day') AS runs
FROM days ORDER BY day
"""

OPEN_ISSUES = """
SELECT * FROM issues WHERE github_state = 'open' AND state IS NOT NULL
ORDER BY updated_at DESC, number DESC
"""

COMPLETE_ISSUES = """
SELECT * FROM issues WHERE github_state = 'closed' AND state = 'complete'
ORDER BY closed_at DESC NULLS LAST, number DESC LIMIT %(limit)s
"""

RUNS_FOR_ISSUE = """
SELECT * FROM runs WHERE issue_number = %(number)s ORDER BY started_at DESC, run_id DESC
"""

RECENT_EVENTS = "SELECT * FROM events ORDER BY id DESC LIMIT %(limit)s"

SNAPSHOT = "SELECT at, written_at, data FROM runtime_snapshot WHERE id"

ISSUE = "SELECT * FROM issues WHERE number = %(number)s"

EVENTS_FOR_ISSUE = """
SELECT * FROM events WHERE issue_number = %(number)s ORDER BY id DESC LIMIT %(limit)s
"""

TURN_SUMMARIES_FOR_ISSUE = f"""
SELECT {SUMMARY_COLUMNS} FROM run_turns t JOIN runs r ON r.run_id = t.run_id
WHERE r.issue_number = %(number)s
ORDER BY r.started_at DESC, r.run_id DESC, t.turn_number
"""

TURN = "SELECT * FROM run_turns WHERE run_id = %(run_id)s AND turn_number = %(turn_number)s"

STATE_COUNTS = """
SELECT state, count(*) AS n FROM issues
WHERE state IS NOT NULL AND (github_state = 'open' OR state = 'complete')
GROUP BY state
"""


class Queries:
    """Read-only queries over one connection; every method is one round trip or two."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def closed_count(self, window: timedelta) -> int:
        """Issues in ``complete`` whose GitHub ``closed_at`` falls inside the window."""
        return await self._count(CLOSED_COUNT, {"window": window})

    async def runs_count(self, window: timedelta) -> int:
        """Worker sessions started inside the window ("agents spun up")."""
        return await self._count(RUNS_COUNT, {"window": window})

    async def run_totals(self, window: timedelta) -> RunTotals:
        """Tokens and cost summed over the same runs ``runs_count`` counts."""
        rows = await self._rows(RUN_TOTALS, {"window": window})
        row = rows[0]
        return RunTotals(
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cost_usd=float(row["cost_usd"]),
        )

    async def daily_series(self, days: int) -> list[DailyPoint]:
        """One point per UTC day for the last ``days`` days, today last, zero-filled."""
        rows = await self._rows(DAILY_SERIES, {"days": days})
        return [DailyPoint(day=row["day"], closed=row["closed"], runs=row["runs"]) for row in rows]

    async def issues_by_state(self) -> dict[str, list[IssueRow]]:
        """Open issues with a state, by StateLabel value, plus the latest complete ones."""
        groups: dict[str, list[IssueRow]] = {role.value: [] for role in StateLabel}
        for row in await self._rows(OPEN_ISSUES):
            if row["state"] in groups:  # a role this issuebot does not know is on no column
                groups[row["state"]].append(IssueRow(**row))
        for row in await self._rows(COMPLETE_ISSUES, {"limit": COMPLETE_LIMIT}):
            groups[StateLabel.COMPLETE.value].append(IssueRow(**row))
        return groups

    async def state_counts(self) -> dict[str, int]:
        """Issues per StateLabel value over the Kanban's predicate; every role key present."""
        counts = {role.value: 0 for role in StateLabel}
        for row in await self._rows(STATE_COUNTS):
            if row["state"] in counts:
                counts[row["state"]] = int(row["n"])
        return counts

    async def issue(self, number: int) -> IssueRow | None:
        rows = await self._rows(ISSUE, {"number": number})
        return IssueRow(**rows[0]) if rows else None

    async def events_for_issue(self, number: int, limit: int) -> list[EventRow]:
        """Newest first."""
        rows = await self._rows(EVENTS_FOR_ISSUE, {"number": number, "limit": limit})
        return [EventRow(**row) for row in rows]

    async def turn_summaries_for_issue(self, number: int) -> list[TurnSummaryRow]:
        """Captured turns of the issue's runs: newest run first (runs.started_at), then turn."""
        rows = await self._rows(TURN_SUMMARIES_FOR_ISSUE, {"number": number})
        return [TurnSummaryRow(**row) for row in rows]

    async def turn(self, run_id: str, turn_number: int) -> TurnRow | None:
        rows = await self._rows(TURN, {"run_id": run_id, "turn_number": turn_number})
        return TurnRow(**rows[0]) if rows else None

    async def runs_for_issue(self, number: int) -> list[RunRow]:
        """Newest first."""
        return [RunRow(**row) for row in await self._rows(RUNS_FOR_ISSUE, {"number": number})]

    async def recent_events(self, limit: int) -> list[EventRow]:
        """Newest first."""
        return [EventRow(**row) for row in await self._rows(RECENT_EVENTS, {"limit": limit})]

    async def snapshot(self) -> SnapshotRow | None:
        rows = await self._rows(SNAPSHOT)
        return SnapshotRow(**rows[0]) if rows else None

    async def _count(self, query: str, params: dict[str, Any]) -> int:
        rows = await self._rows(query, params)
        return int(rows[0]["n"])

    async def _rows(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchall()
