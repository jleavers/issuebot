"""The SQL writes behind the sink: one connection, three methods, errors mapped and redacted."""

import contextlib
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any, Protocol

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from issuebot.agent.turnlog import TurnCapture
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

    async def apply_event(self, event: Event, turns: Sequence[TurnCapture] = ()) -> None: ...

    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None: ...

    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None: ...

    async def reconcile_triage_track(self, numbers: Sequence[int], at: datetime) -> None: ...


INSERT_EVENT = """
INSERT INTO events (repo, at, kind, issue_number, run_id, payload)
VALUES (%(repo)s, %(at)s, %(kind)s, %(issue_number)s, %(run_id)s, %(payload)s)
"""

RUN_STARTED = """
INSERT INTO runs (repo, run_id, issue_number, issue_identifier, attempt, session_id, started_at,
                  workspace_path, kind)
VALUES (%(repo)s, %(run_id)s, %(issue_number)s, %(issue_identifier)s, %(attempt)s, %(session_id)s,
        %(started_at)s, %(workspace_path)s, %(kind)s)
ON CONFLICT (repo, run_id) DO UPDATE SET
    issue_number = EXCLUDED.issue_number,
    issue_identifier = EXCLUDED.issue_identifier,
    attempt = EXCLUDED.attempt,
    session_id = EXCLUDED.session_id,
    started_at = EXCLUDED.started_at,
    workspace_path = EXCLUDED.workspace_path,
    kind = EXCLUDED.kind
"""

RUN_ENDED = """
INSERT INTO runs (repo, run_id, issue_number, issue_identifier, started_at, ended_at, outcome,
                  error, turns, input_tokens, output_tokens, cost_usd, duration_s, log_dir,
                  kind)
VALUES (%(repo)s, %(run_id)s, %(issue_number)s, %(issue_identifier)s, %(started_at)s, %(ended_at)s,
        %(outcome)s, %(error)s, %(turns)s, %(input_tokens)s, %(output_tokens)s, %(cost_usd)s,
        %(duration_s)s, %(log_dir)s, %(kind)s)
ON CONFLICT (repo, run_id) DO UPDATE SET
    ended_at = EXCLUDED.ended_at,
    outcome = EXCLUDED.outcome,
    error = EXCLUDED.error,
    turns = EXCLUDED.turns,
    input_tokens = EXCLUDED.input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    cost_usd = EXCLUDED.cost_usd,
    duration_s = EXCLUDED.duration_s,
    log_dir = EXCLUDED.log_dir,
    kind = EXCLUDED.kind
"""

INSERT_TURN = """
INSERT INTO run_turns (repo, run_id, turn_number, captured_at, model, subtype, is_error,
                       num_turns, input_tokens, cache_creation_input_tokens,
                       cache_read_input_tokens, output_tokens, cost_usd, duration_ms,
                       result_text, prompt, prompt_bytes, stream, stream_bytes, stream_lines,
                       omitted_lines, stderr, stderr_bytes, truncated)
VALUES (%(repo)s, %(run_id)s, %(turn_number)s, now(), %(model)s, %(subtype)s, %(is_error)s,
        %(num_turns)s, %(input_tokens)s, %(cache_creation_input_tokens)s,
        %(cache_read_input_tokens)s, %(output_tokens)s, %(cost_usd)s, %(duration_ms)s,
        %(result_text)s, %(prompt)s, %(prompt_bytes)s, %(stream)s, %(stream_bytes)s,
        %(stream_lines)s, %(omitted_lines)s, %(stderr)s, %(stderr_bytes)s, %(truncated)s)
ON CONFLICT (repo, run_id, turn_number) DO UPDATE SET
    captured_at = now(),
    model = EXCLUDED.model,
    subtype = EXCLUDED.subtype,
    is_error = EXCLUDED.is_error,
    num_turns = EXCLUDED.num_turns,
    input_tokens = EXCLUDED.input_tokens,
    cache_creation_input_tokens = EXCLUDED.cache_creation_input_tokens,
    cache_read_input_tokens = EXCLUDED.cache_read_input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    cost_usd = EXCLUDED.cost_usd,
    duration_ms = EXCLUDED.duration_ms,
    result_text = EXCLUDED.result_text,
    prompt = EXCLUDED.prompt,
    prompt_bytes = EXCLUDED.prompt_bytes,
    stream = EXCLUDED.stream,
    stream_bytes = EXCLUDED.stream_bytes,
    stream_lines = EXCLUDED.stream_lines,
    omitted_lines = EXCLUDED.omitted_lines,
    stderr = EXCLUDED.stderr,
    stderr_bytes = EXCLUDED.stderr_bytes,
    truncated = EXCLUDED.truncated
"""

STATE_CHANGED = """
UPDATE issues SET state = %(state)s, state_label = %(state_label)s, seen_at = %(at)s
WHERE repo = %(repo)s AND number = %(number)s AND seen_at <= %(at)s
"""

TRIAGE_CHANGED = """
UPDATE issues SET triage = %(triage)s, seen_at = %(at)s
WHERE repo = %(repo)s AND number = %(number)s AND seen_at <= %(at)s
"""

# The complete set of open triage-track issues, read at %(at)s: an open row missing from it has
# left the track (closed, or stripped of its label). Closed rows are on no column either way.
RECONCILE_TRIAGE = """
UPDATE issues SET triage = NULL
WHERE repo = %(repo)s AND triage IS NOT NULL AND github_state = 'open'
  AND NOT (number = ANY(%(numbers)s)) AND seen_at <= %(at)s
"""

ISSUE_CLOSED = """
UPDATE issues
SET github_state = 'closed', closed_at = coalesce(closed_at, %(at)s), seen_at = %(at)s
WHERE repo = %(repo)s AND number = %(number)s AND seen_at <= %(at)s
"""

UPSERT_ISSUE = """
INSERT INTO issues (repo, number, identifier, title, state, state_label, triage, github_state,
                    url, labels, pr_number, pr_url, pr_state, pr_merged_at, created_at, updated_at,
                    closed_at, seen_at)
VALUES (%(repo)s, %(number)s, %(identifier)s, %(title)s, %(state)s, %(state_label)s,
        %(triage)s, %(github_state)s, %(url)s, %(labels)s, %(pr_number)s, %(pr_url)s, %(pr_state)s,
        %(pr_merged_at)s, %(created_at)s, %(updated_at)s, %(closed_at)s, %(seen_at)s)
ON CONFLICT (repo, number) DO UPDATE SET
    identifier = EXCLUDED.identifier,
    title = EXCLUDED.title,
    state = EXCLUDED.state,
    state_label = EXCLUDED.state_label,
    triage = EXCLUDED.triage,
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
INSERT INTO runtime_snapshot (repo, at, written_at, data)
VALUES (%(repo)s, %(at)s, now(), %(data)s)
ON CONFLICT (repo) DO UPDATE SET at = EXCLUDED.at, written_at = now(), data = EXCLUDED.data
"""

REGISTER_REPO = """
INSERT INTO repos (repo, labels, workflow_path, registered_at, seen_at)
VALUES (%(repo)s, %(labels)s, %(workflow_path)s, now(), now())
ON CONFLICT (repo) DO UPDATE SET
    labels = EXCLUDED.labels,
    workflow_path = EXCLUDED.workflow_path,
    seen_at = now()
"""


def triage_track(names: Iterable[str], labels: GitHubLabels) -> str | None:
    """The ``issues.triage`` value for a set of label names, against the configured names.

    ``triage_todo`` wins over ``triage_complete``: a human re-triaging a parent adds the
    former and may leave the latter on, and the issue is waiting again.
    """
    lowered = {name.lower() for name in names}
    if labels.triage_todo.lower() in lowered:
        return "triage_todo"
    if labels.triage_complete.lower() in lowered:
        return "triage_complete"
    return None


def issue_row(snapshot: IssueSnapshot, labels: GitHubLabels) -> dict[str, Any]:
    """The bound parameters of UPSERT_ISSUE for one polled issue."""
    issue = snapshot.issue
    pr = issue.linked_pr
    return {
        "number": issue.number,
        "identifier": issue.identifier,
        "title": issue.title,
        "state": issue.state.value if issue.state is not None else None,
        "state_label": issue.state_labels[0] if len(issue.state_labels) == 1 else None,
        "triage": triage_track(issue.labels, labels),
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
    """Writes events, runs, issues and the runtime snapshot over one autocommit connection,
    every row stamped with the worker's repository."""

    def __init__(
        self, url: str, *, repo: str, labels: GitHubLabels, connect: Connector = connect
    ) -> None:
        self._url = url
        self._repo = repo
        self._labels = labels
        self._connect = connect
        self._conn: AsyncConnection | None = None

    @property
    def repo(self) -> str:
        return self._repo

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

    async def apply_event(self, event: Event, turns: Sequence[TurnCapture] = ()) -> None:
        """Append the event; then upsert the run (and its captured turns) or update the issue."""
        conn = self._require()
        async with self._guard(), conn.transaction():
            await conn.execute(INSERT_EVENT, self._stamp(event_row(event)))
            if isinstance(event, RunStarted):
                await conn.execute(RUN_STARTED, self._stamp(run_started_row(event)))
            elif isinstance(event, RunEnded):
                await conn.execute(RUN_ENDED, self._stamp(run_ended_row(event)))
                if turns:
                    async with conn.cursor() as cursor:
                        await cursor.executemany(
                            INSERT_TURN,
                            [self._stamp(turn_row(event.run_id, turn)) for turn in turns],
                        )
            elif isinstance(event, StateChanged):
                if not _on_the_triage_track(event, self._labels):
                    await conn.execute(STATE_CHANGED, self._stamp(self._state_changed_row(event)))
                else:
                    # Only a move that lands on a triage label says what the track now is; one
                    # that leaves it does not say which labels remain, so the poll settles it.
                    track = (
                        triage_track([event.to_label], self._labels)
                        if event.to_label is not None
                        else None
                    )
                    if track is not None:
                        await conn.execute(
                            TRIAGE_CHANGED,
                            self._stamp(
                                {"number": event.issue_number, "triage": track, "at": event.at}
                            ),
                        )
            elif isinstance(event, IssueCompleted | IssueCancelled):
                await conn.execute(
                    ISSUE_CLOSED, self._stamp({"number": event.issue_number, "at": event.at})
                )

    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None:
        if not issues:
            return
        conn = self._require()
        async with self._guard(), conn.transaction(), conn.cursor() as cursor:
            await cursor.executemany(
                UPSERT_ISSUE,
                [self._stamp(issue_row(snapshot, self._labels)) for snapshot in issues],
            )

    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None:
        conn = self._require()
        async with self._guard():
            await conn.execute(WRITE_SNAPSHOT, self._stamp({"at": at, "data": Jsonb(dict(data))}))

    async def reconcile_triage_track(self, numbers: Sequence[int], at: datetime) -> None:
        """Clear ``triage`` on every open row the complete triage-track read did not list.

        ``at`` guards the write but is not stamped into ``seen_at``: the sink drains in order,
        so every snapshot of the same read has already been written, and bumping ``seen_at``
        would only make a poll that began earlier lose a race it has already won.
        """
        conn = self._require()
        async with self._guard():
            await conn.execute(RECONCILE_TRIAGE, self._stamp({"numbers": list(numbers), "at": at}))

    def _stamp(self, row: dict[str, Any]) -> dict[str, Any]:
        """The bound parameters of one write, with this store's repository forced in.

        Forced, not merged (#111): the repository a row belongs to is the store's, decided
        when the worker registered, and a row that arrived carrying one of its own would
        otherwise be written into another repository's history. Every write goes through
        here, ``INSERT_TURN`` included, so this is the one place a row's tenancy is set.
        """
        return {**row, "repo": self._repo}

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


def _on_the_triage_track(event: StateChanged, labels: GitHubLabels) -> bool:
    """Whether a move is triage's own (spec 2026-10-25, section 7).

    The triage labels sit beside the issue's state rather than in it: an issue waiting to be
    triaged is still ``todo``, and the verdict's label is not a column. The event is still
    written, so the history has the move; the recorded state is left alone, and the track
    (``issues.triage``) is what records it instead.
    """
    track = {labels.triage_todo.lower(), labels.triage_complete.lower()}
    return any(
        name is not None and name.lower() in track for name in (event.from_label, event.to_label)
    )


def run_started_row(event: RunStarted) -> dict[str, Any]:
    return {
        "run_id": event.run_id,
        "issue_number": event.issue_number,
        "issue_identifier": event.issue_identifier,
        "attempt": event.attempt,
        "session_id": event.session_id,
        "started_at": event.at,
        "workspace_path": event.workspace_path or None,
        "kind": event.run_kind,
    }


def turn_row(run_id: str, turn: TurnCapture) -> dict[str, Any]:
    """The bound parameters of INSERT_TURN but the repository: every TurnCapture field plus
    the run id; ``_stamp`` adds the repository, as for every other write."""
    return {"run_id": run_id, **{f.name: getattr(turn, f.name) for f in fields(turn)}}


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
        "kind": event.run_kind,
    }
