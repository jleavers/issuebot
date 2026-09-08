"""Runtime records and pure scheduling rules for the orchestrator. No I/O."""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from issuebot.agent import RunResult
from issuebot.agent.runner import Credential, RateLimits
from issuebot.config import GitHubLabels
from issuebot.events import Event, PrOpened, StateChanged
from issuebot.github import Issue, StateLabel

RetryKind = Literal["continuation", "failure", "escape", "slots", "auth"]
DispatchHoldKind = Literal["preflight", "auth"]
StopCause = Literal["stalled", "moved", "closed", "missing", "shutdown"]

CONTINUATION_DELAY_MS = 1_000
BACKOFF_BASE_MS = 10_000
TERMINAL_SWEEP_EVERY_TICKS = 10

_RANK: dict[StateLabel | None, int] = {
    StateLabel.IN_PROGRESS: 0,
    StateLabel.REWORK: 1,
    StateLabel.TODO: 2,
}


def backoff_ms(attempt: int, max_backoff_ms: int) -> int:
    """``min(10000 * 2 ** (attempt - 1), max_backoff_ms)``; ``attempt`` is the one about to run."""
    exponent = max(attempt - 1, 0)
    return min(BACKOFF_BASE_MS * 2**exponent, max_backoff_ms)


def sort_candidates(issues: Iterable[Issue]) -> list[Issue]:
    """Orphaned in_progress first, then rework, then todo; oldest created_at, then number."""

    def key(issue: Issue) -> tuple[int, datetime, int]:
        return (_RANK.get(issue.state, len(_RANK)), issue.created_at, issue.number)

    return sorted(issues, key=key)


def state_label_name(issue: Issue) -> str | None:
    """The raw name of the issue's state label, for StateChanged.from_label/to_label."""
    return issue.state_labels[0] if issue.state_labels else None


def pr_url(issue: Issue) -> str | None:
    return issue.linked_pr.url if issue.linked_pr is not None else None


def claimed_snapshot(issue: Issue, labels: GitHubLabels) -> Issue:
    """The issue after ``claim``: state labels replaced, markers dropped, the rest kept."""
    dropped = {name.lower() for name in (*labels.as_tuple(), *labels.markers())}
    target = labels.in_progress.lower()
    kept = tuple(name for name in issue.labels if name.lower() not in dropped)
    return replace(
        issue,
        state=StateLabel.IN_PROGRESS,
        state_labels=(target,),
        labels=(*kept, target),
        dispatchable=issue.github_state == "open",
    )


def observe_transition(previous: Issue, current: Issue) -> list[Event]:
    """Events for what changed between two snapshots of one open issue.

    ``in_progress`` to ``review`` is the agent's transition; every other observed move is a
    human's. A linked pull request appearing is ``PrOpened``.
    """
    events: list[Event] = []
    before, after = state_label_name(previous), state_label_name(current)
    if before != after:
        agent_move = previous.state is StateLabel.IN_PROGRESS and current.state is StateLabel.REVIEW
        events.append(
            StateChanged(
                issue_number=current.number,
                issue_identifier=current.identifier,
                from_label=before,
                to_label=after,
                actor="agent" if agent_move else "human",
                pr_url=pr_url(current),
            )
        )
    if previous.linked_pr is None and current.linked_pr is not None:
        events.append(
            PrOpened(
                issue_number=current.number,
                issue_identifier=current.identifier,
                pr_number=current.linked_pr.number,
                pr_url=current.linked_pr.url,
            )
        )
    return events


# --- records --------------------------------------------------------------------------


@dataclass(kw_only=True)
class RunningEntry:
    """One worker task and everything the orchestrator knows about it."""

    issue: Issue
    attempt: int
    rework: bool
    resumed: bool
    run_id: str
    started_mono: float
    started_at: datetime
    cancel: asyncio.Event
    task: asyncio.Task[RunResult] | None = None
    session_id: str | None = None
    last_activity_mono: float | None = None
    last_activity_at: datetime | None = None
    last_event: str | None = None
    turns: int = 0
    stop_cause: StopCause | None = None
    stop_detail: str | None = None
    review_seen_mono: float | None = None  # first sight of `review`; the grace runs from here
    terminal_issue: Issue | None = None

    @property
    def issue_id(self) -> str:
        return self.issue.id

    @property
    def identifier(self) -> str:
        return self.issue.identifier

    def stop(self, cause: StopCause, detail: str) -> None:
        """Record the first cause only, then set the cancel event."""
        if self.stop_cause is None:
            self.stop_cause = cause
            self.stop_detail = detail
        self.cancel.set()


@dataclass(frozen=True, kw_only=True, slots=True)
class BlockedContext:
    """What the blocked escape writes; also carried by an escape retry."""

    reason: str
    run_id: str
    attempt: int
    turns: int
    log_dir: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class RetryEntry:
    issue_id: str
    identifier: str
    issue_number: int
    issue_url: str
    attempt: int
    kind: RetryKind
    due_mono: float
    due_at: datetime
    error: str | None
    escape: BlockedContext | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class ClaudeTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    seconds_running: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, result: RunResult) -> ClaudeTotals:
        return ClaudeTotals(
            input_tokens=self.input_tokens + result.input_tokens,
            output_tokens=self.output_tokens + result.output_tokens,
            cost_usd=round(self.cost_usd + result.cost_usd, 6),
            seconds_running=round(self.seconds_running + result.duration_s, 3),
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class Counters:
    runs_started: int = 0
    runs_ended: int = 0
    issues_completed: int = 0
    issues_cancelled: int = 0
    blocked: int = 0

    def bump(self, **deltas: int) -> Counters:
        changes = {name: getattr(self, name) + delta for name, delta in deltas.items()}
        return replace(self, **changes)


# --- snapshot -------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class DispatchHold:
    """Why a worker that is still ticking will not claim an issue (#29).

    ``preflight`` is a missing executable or token, ``auth`` the authentication hold of #20.
    ``since`` is when this reason first held dispatch, so a hold that outlives its cause is
    visible as one; a changed reason starts it again.
    """

    kind: DispatchHoldKind
    reason: str
    since: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class RunningRow:
    issue_number: int
    identifier: str
    title: str
    url: str
    state: str | None
    attempt: int
    rework: bool
    resumed: bool
    run_id: str
    session_id: str | None
    started_at: datetime
    last_activity_at: datetime | None
    last_event: str | None
    turns: int
    stop_cause: StopCause | None

    @classmethod
    def from_entry(cls, entry: RunningEntry) -> RunningRow:
        return cls(
            issue_number=entry.issue.number,
            identifier=entry.identifier,
            title=entry.issue.title,
            url=entry.issue.url,
            state=entry.issue.state.value if entry.issue.state is not None else None,
            attempt=entry.attempt,
            rework=entry.rework,
            resumed=entry.resumed,
            run_id=entry.run_id,
            session_id=entry.session_id,
            started_at=entry.started_at,
            last_activity_at=entry.last_activity_at,
            last_event=entry.last_event,
            turns=entry.turns,
            stop_cause=entry.stop_cause,
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class RetryRow:
    issue_number: int
    identifier: str
    url: str
    attempt: int
    kind: RetryKind
    due_at: datetime
    error: str | None

    @classmethod
    def from_entry(cls, entry: RetryEntry) -> RetryRow:
        return cls(
            issue_number=entry.issue_number,
            identifier=entry.identifier,
            url=entry.issue_url,
            attempt=entry.attempt,
            kind=entry.kind,
            due_at=entry.due_at,
            error=entry.error,
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class RuntimeSnapshot:
    """The worker's runtime state at one instant; the shape of the future ``/api/v1/state``."""

    at: datetime
    workflow_path: str
    workflow_mtime_ns: int
    config_valid: bool
    config_error: str | None
    dispatch_hold: DispatchHold | None
    poll_interval_ms: int
    max_concurrent_agents: int
    tick_count: int
    last_tick_at: datetime | None
    running: tuple[RunningRow, ...]
    retrying: tuple[RetryRow, ...]
    totals: ClaudeTotals
    counters: Counters
    # What the agent spends, from the startup auth probe, and the newest usage reading any
    # session has seen. The reading is about the account, not an issue, so one worker holds
    # one; it is None until a turn reports one, and stays None for the whole life of a worker
    # on an API key, where claude reports no windows at all.
    credential: Credential = "unknown"
    rate_limits: RateLimits | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe: datetimes as ISO 8601, enums as values, tuples as lists."""
        data = _jsonable(self)
        data["totals"]["total_tokens"] = self.totals.total_tokens
        return data


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    return value
