"""The SQL writes behind the sink: one connection, three methods, errors mapped and redacted."""

import contextlib
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from issuebot.config import GitHubLabels
from issuebot.db.connection import Connector, classify, connect, error_text, redact
from issuebot.db.errors import StoreError, StoreUnavailableError
from issuebot.events import (
    Event,
    IssueCancelled,
    IssueCompleted,
    IssueEvent,
    RunEnded,
    RunStarted,
    StateChanged,
)
from issuebot.github import Issue, role_for


@dataclass(frozen=True, slots=True)
class IssueSnapshot:
    """One polled issue and when it was observed; the write is guarded by ``seen_at``."""

    issue: Issue
    seen_at: datetime


class Store(Protocol):
    """What the sink writes through. ``PostgresStore`` is the real one; tests use a fake."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def apply_event(self, event: Event) -> None: ...

    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None: ...

    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None: ...


INSERT_EVENT = """
INSERT INTO events (at, kind, issue_number, run_id, payload)
VALUES (%(at)s, %(kind)s, %(issue_number)s, %(run_id)s, %(payload)s)
"""

RUN_STARTED = """
INSERT INTO runs (run_id, issue_number, issue_identifier, attempt, session_id, started_at,
                  workspace_path)
VALUES (%(run_id)s, %(issue_number)s, %(issue_identifier)s, %(attempt)s, %(session_id)s,
        %(started_at)s, %(workspace_path)s)
ON CONFLICT (run_id) DO UPDATE SET
    issue_number = EXCLUDED.issue_number,
    issue_identifier = EXCLUDED.issue_identifier,
    attempt = EXCLUDED.attempt,
    session_id = EXCLUDED.session_id,
    started_at = EXCLUDED.started_at,
    workspace_path = EXCLUDED.workspace_path
"""

RUN_ENDED = """
INSERT INTO runs (run_id, issue_number, issue_identifier, started_at, ended_at, outcome, error,
                  turns, input_tokens, output_tokens, cost_usd, duration_s, log_dir)
VALUES (%(run_id)s, %(issue_number)s, %(issue_identifier)s, %(started_at)s, %(ended_at)s,
        %(outcome)s, %(error)s, %(turns)s, %(input_tokens)s, %(output_tokens)s, %(cost_usd)s,
        %(duration_s)s, %(log_dir)s)
ON CONFLICT (run_id) DO UPDATE SET
    ended_at = EXCLUDED.ended_at,
    outcome = EXCLUDED.outcome,
    error = EXCLUDED.error,
    turns = EXCLUDED.turns,
    input_tokens = EXCLUDED.input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    cost_usd = EXCLUDED.cost_usd,
    duration_s = EXCLUDED.duration_s,
    log_dir = EXCLUDED.log_dir
"""

STATE_CHANGED = """
UPDATE issues SET state = %(state)s, state_label = %(state_label)s, seen_at = %(at)s
WHERE number = %(number)s AND seen_at <= %(at)s
"""

ISSUE_CLOSED = """
UPDATE issues
SET github_state = 'closed', closed_at = coalesce(closed_at, %(at)s), seen_at = %(at)s
WHERE number = %(number)s AND seen_at <= %(at)s
"""

UPSERT_ISSUE = """
INSERT INTO issues (number, identifier, title, state, state_label, github_state, url, labels,
                    pr_number, pr_url, pr_state, pr_merged_at, created_at, updated_at, closed_at,
                    seen_at)
VALUES (%(number)s, %(identifier)s, %(title)s, %(state)s, %(state_label)s, %(github_state)s,
        %(url)s, %(labels)s, %(pr_number)s, %(pr_url)s, %(pr_state)s, %(pr_merged_at)s,
        %(created_at)s, %(updated_at)s, %(closed_at)s, %(seen_at)s)
ON CONFLICT (number) DO UPDATE SET
    identifier = EXCLUDED.identifier,
    title = EXCLUDED.title,
    state = EXCLUDED.state,
    state_label = EXCLUDED.state_label,
    github_state = EXCLUDED.github_state,
    url = EXCLUDED.url,
    labels = EXCLUDED.labels,
    pr_number = EXCLUDED.pr_number,
    pr_url = EXCLUDED.pr_url,
    pr_state = EXCLUDED.pr_state,
    pr_merged_at = EXCLUDED.pr_merged_at,
    created_at = EXCLUDED.created_at,
    updated_at = EXCLUDED.updated_at,
    closed_at = EXCLUDED.closed_at,
    seen_at = EXCLUDED.seen_at
WHERE issues.seen_at <= EXCLUDED.seen_at
"""

WRITE_SNAPSHOT = """
INSERT INTO runtime_snapshot (id, at, written_at, data) VALUES (true, %(at)s, now(), %(data)s)
ON CONFLICT (id) DO UPDATE SET at = EXCLUDED.at, written_at = now(), data = EXCLUDED.data
"""


def issue_row(snapshot: IssueSnapshot) -> dict[str, Any]:
    """The bound parameters of UPSERT_ISSUE for one polled issue."""
    issue = snapshot.issue
    pr = issue.linked_pr
    return {
        "number": issue.number,
        "identifier": issue.identifier,
        "title": issue.title,
        "state": issue.state.value if issue.state is not None else None,
        "state_label": issue.state_labels[0] if len(issue.state_labels) == 1 else None,
        "github_state": issue.github_state,
        "url": issue.url,
        "labels": list(issue.labels),
        "pr_number": pr.number if pr is not None else None,
        "pr_url": pr.url if pr is not None else None,
        "pr_state": pr.state if pr is not None else None,
        "pr_merged_at": pr.merged_at if pr is not None else None,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "closed_at": issue.closed_at,
        "seen_at": snapshot.seen_at,
    }


class PostgresStore:
    """Writes events, runs, issues and the runtime snapshot over one autocommit connection."""

    def __init__(self, url: str, *, labels: GitHubLabels, connect: Connector = connect) -> None:
        self._url = url
        self._labels = labels
        self._connect = connect
        self._conn: AsyncConnection | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    async def connect(self) -> None:
        """Open the connection, replacing a previous one (broken or not)."""
        await self.close()
        async with self._guard():
            self._conn = await self._connect(self._url)

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(psycopg.Error):
                await conn.close()

    async def apply_event(self, event: Event) -> None:
        """Append the event; then upsert the run or update the issue it is about."""
        conn = self._require()
        async with self._guard(), conn.transaction():
            await conn.execute(INSERT_EVENT, event_row(event))
            if isinstance(event, RunStarted):
                await conn.execute(RUN_STARTED, run_started_row(event))
            elif isinstance(event, RunEnded):
                await conn.execute(RUN_ENDED, run_ended_row(event))
            elif isinstance(event, StateChanged):
                await conn.execute(STATE_CHANGED, self._state_changed_row(event))
            elif isinstance(event, IssueCompleted | IssueCancelled):
                await conn.execute(ISSUE_CLOSED, {"number": event.issue_number, "at": event.at})

    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None:
        if not issues:
            return
        conn = self._require()
        async with self._guard(), conn.transaction(), conn.cursor() as cursor:
            await cursor.executemany(UPSERT_ISSUE, [issue_row(snapshot) for snapshot in issues])

    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None:
        conn = self._require()
        async with self._guard():
            await conn.execute(WRITE_SNAPSHOT, {"at": at, "data": Jsonb(dict(data))})

    def _require(self) -> AsyncConnection:
        if self._conn is None or self._conn.closed:
            raise StoreUnavailableError("not connected")
        return self._conn

    @asynccontextmanager
    async def _guard(self) -> AsyncIterator[None]:
        """Map psycopg errors to StoreUnavailableError/StoreError with the URL redacted."""
        try:
            yield
        except psycopg.Error as exc:
            raise classify(exc, self._url) from exc
        except (TypeError, ValueError) as exc:
            raise StoreError(redact(f"{type(exc).__name__}: {error_text(exc)}", self._url)) from exc

    def _state_changed_row(self, event: StateChanged) -> dict[str, Any]:
        role = role_for(self._labels, event.to_label) if event.to_label is not None else None
        return {
            "number": event.issue_number,
            "state": role.value if role is not None else None,
            "state_label": event.to_label,
            "at": event.at,
        }


def event_row(event: Event) -> dict[str, Any]:
    payload = event.to_dict()
    return {
        "at": event.at,
        "kind": event.kind,
        "issue_number": event.issue_number if isinstance(event, IssueEvent) else None,
        "run_id": payload.get("run_id"),
        "payload": Jsonb(payload),
    }


def run_started_row(event: RunStarted) -> dict[str, Any]:
    return {
        "run_id": event.run_id,
        "issue_number": event.issue_number,
        "issue_identifier": event.issue_identifier,
        "attempt": event.attempt,
        "session_id": event.session_id,
        "started_at": event.at,
        "workspace_path": event.workspace_path or None,
    }


def run_ended_row(event: RunEnded) -> dict[str, Any]:
    return {
        "run_id": event.run_id,
        "issue_number": event.issue_number,
        "issue_identifier": event.issue_identifier,
        "started_at": event.at - timedelta(seconds=event.duration_s),
        "ended_at": event.at,
        "outcome": event.outcome,
        "error": event.error,
        "turns": event.turns,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "cost_usd": event.cost_usd,
        "duration_s": event.duration_s,
        "log_dir": event.log_dir,
    }
