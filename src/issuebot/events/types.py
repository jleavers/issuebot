"""Domain events published by the orchestrator and consumed by sinks."""

from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True)
class Event:
    """Base event. ``kind`` is a stable string identifier; ``at`` is an aware UTC timestamp."""

    kind: ClassVar[str] = "event"
    at: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe mapping with ``kind``, ISO 8601 ``at`` and every field."""
        data: dict[str, Any] = {"kind": self.kind, "at": self.at.isoformat()}
        for f in fields(self):
            if f.name != "at":
                data[f.name] = getattr(self, f.name)
        return data


@dataclass(frozen=True, kw_only=True)
class IssueEvent(Event):
    issue_number: int
    issue_identifier: str


@dataclass(frozen=True, kw_only=True)
class StateChanged(IssueEvent):
    kind: ClassVar[str] = "state_changed"
    from_label: str | None
    to_label: str | None
    actor: Literal["issuebot", "agent", "human"]
    pr_url: str | None = None
    # A short line a move wants said beside it (triage's verdict: "split into 3 sub-issues").
    note: str | None = None


# A run is the work session or a triage one (spec 2026-10-25, section 4); the store keeps
# the kind, because the admission ledger's failure chain counts work runs only. A parent
# review is the triage session over a split parent's steps (section 6).
RunKind = Literal["work", "triage", "parent_review"]


@dataclass(frozen=True, kw_only=True)
class RunStarted(IssueEvent):
    kind: ClassVar[str] = "run_started"
    run_id: str
    attempt: int
    session_id: str | None
    workspace_path: str
    run_kind: RunKind = "work"


RunOutcome = Literal["succeeded", "failed", "timed_out", "stalled", "cancelled"]


@dataclass(frozen=True, kw_only=True)
class RunEnded(IssueEvent):
    kind: ClassVar[str] = "run_ended"
    run_id: str
    outcome: RunOutcome
    error: str | None
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_s: float
    log_dir: str | None = None
    run_kind: RunKind = "work"


@dataclass(frozen=True, kw_only=True)
class PrOpened(IssueEvent):
    kind: ClassVar[str] = "pr_opened"
    pr_number: int
    pr_url: str


@dataclass(frozen=True, kw_only=True)
class Blocked(IssueEvent):
    kind: ClassVar[str] = "blocked"
    reason: str


CompletionResolution = Literal["merged_pr", "no_change", "parent_review"]


@dataclass(frozen=True, kw_only=True)
class IssueCompleted(IssueEvent):
    """A closed issue issuebot counts as done: a merged PR, or an investigation finding no fault."""

    kind: ClassVar[str] = "issue_completed"
    pr_url: str | None
    # The default is for reading back payloads stored before #34, not for callers: every
    # construction site says which resolution it means.
    resolution: CompletionResolution = "merged_pr"


@dataclass(frozen=True, kw_only=True)
class IssueCancelled(IssueEvent):
    kind: ClassVar[str] = "issue_cancelled"
    reason: str


@dataclass(frozen=True, kw_only=True)
class NotificationSent(IssueEvent):
    kind: ClassVar[str] = "notification_sent"
    channel: str
    about_kind: str


@dataclass(frozen=True, kw_only=True)
class ParentReviewState(IssueEvent):
    """The worker's record of a split parent's review, published whole on every change.

    The store keeps it like any event and the worker reads the latest one per parent back at
    startup, so the counts and a hand-over survive a restart without a table.
    """

    kind: ClassVar[str] = "parent_review_state"
    reviews: int
    gap_rounds: int
    handed_over: str | None = None
    finished: bool = False


EVENT_TYPES: tuple[type[Event], ...] = (
    StateChanged,
    RunStarted,
    RunEnded,
    PrOpened,
    Blocked,
    IssueCompleted,
    IssueCancelled,
    NotificationSent,
    ParentReviewState,
)

EVENT_KINDS: frozenset[str] = frozenset(cls.kind for cls in EVENT_TYPES)
