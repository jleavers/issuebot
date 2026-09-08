"""Builders for the web tests: rows shaped like the query module's, a clock, a test client."""

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from fakes.database import FakeDatabase
from issuebot.agent.runner import RateLimits, RateLimitWindow
from issuebot.config import GitHubSettings, Settings
from issuebot.db.queries import (
    EventRow,
    IssueRow,
    RunRow,
    SnapshotRow,
    TurnRow,
    TurnSummaryRow,
)
from issuebot.orchestrator.state import (
    ClaudeTotals,
    Counters,
    DispatchHold,
    RetryRow,
    RunningRow,
    RuntimeSnapshot,
)
from issuebot.web import create_app

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def limits(
    five: float = 0.42,
    seven: float = 0.32,
    *,
    five_resets_in: timedelta = timedelta(hours=2),
    seven_resets_in: timedelta = timedelta(days=3),
    observed_ago: timedelta = timedelta(minutes=4),
) -> RateLimits:
    """A rate-limit reading, by default a live one with both windows still open."""
    return RateLimits(
        five_hour=RateLimitWindow(utilization=five, resets_at=NOW + five_resets_in),
        seven_day=RateLimitWindow(utilization=seven, resets_at=NOW + seven_resets_in),
        observed_at=NOW - observed_ago,
    )


RUN_ID = "20260904T202535Z-0964cd"
SETTINGS = Settings(github=GitHubSettings(repo="example/repo"))


class Clock:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.now = NOW

    def __call__(self) -> float:
        return self.mono

    def utcnow(self) -> datetime:
        return self.now


def running_row(**overrides: Any) -> RunningRow:
    fields: dict[str, Any] = {
        "issue_number": 7,
        "identifier": "repo-7",
        "title": "Add a power function",
        "url": "https://github.com/example/repo/issues/7",
        "state": "in_progress",
        "attempt": 1,
        "rework": False,
        "resumed": False,
        "run_id": RUN_ID,
        "session_id": "sess-7",
        "started_at": NOW - timedelta(minutes=3),
        "last_activity_at": NOW - timedelta(seconds=20),
        "last_event": "turn_activity",
        "turns": 1,
        "stop_cause": None,
    }
    fields.update(overrides)
    return RunningRow(**fields)


def retry_row(**overrides: Any) -> RetryRow:
    fields: dict[str, Any] = {
        "issue_number": 9,
        "identifier": "repo-9",
        "url": "https://github.com/example/repo/issues/9",
        "attempt": 2,
        "kind": "failure",
        "due_at": NOW + timedelta(seconds=20),
        "error": "turn_failed: boom",
    }
    fields.update(overrides)
    return RetryRow(**fields)


def snapshot(
    *,
    running: tuple[RunningRow, ...] = (),
    retrying: tuple[RetryRow, ...] = (),
    age_s: float = 5.0,
    poll_interval_ms: int = 30_000,
    dispatch_hold: DispatchHold | None = None,
    credential: str = "subscription",
    rate_limits: RateLimits | None = None,
) -> SnapshotRow:
    data = RuntimeSnapshot(
        at=NOW - timedelta(seconds=age_s + 1),
        workflow_path="/app/WORKFLOW.md",
        workflow_mtime_ns=1,
        config_valid=True,
        config_error=None,
        dispatch_hold=dispatch_hold,
        poll_interval_ms=poll_interval_ms,
        max_concurrent_agents=2,
        tick_count=41,
        last_tick_at=NOW - timedelta(seconds=age_s + 1),
        running=running,
        retrying=retrying,
        totals=ClaudeTotals(
            input_tokens=1000, output_tokens=50, cost_usd=1.25, seconds_running=90.0
        ),
        counters=Counters(runs_started=3, runs_ended=2, issues_completed=1),
        credential=credential,  # type: ignore[arg-type]
        rate_limits=rate_limits,
    ).to_dict()
    at = NOW - timedelta(seconds=age_s + 1)
    return SnapshotRow(at=at, written_at=NOW - timedelta(seconds=age_s), data=data)


def issue_row(**overrides: Any) -> IssueRow:
    fields: dict[str, Any] = {
        "number": 7,
        "identifier": "repo-7",
        "title": "Add a power function",
        "state": "review",
        "state_label": "issuebot/review",
        "github_state": "open",
        "url": "https://github.com/example/repo/issues/7",
        "labels": ["issuebot/review"],
        "pr_number": 8,
        "pr_url": "https://github.com/example/repo/pull/8",
        "pr_state": "open",
        "pr_merged_at": None,
        "created_at": NOW - timedelta(days=1),
        "updated_at": NOW - timedelta(hours=1),
        "closed_at": None,
        "seen_at": NOW - timedelta(minutes=1),
    }
    fields.update(overrides)
    return IssueRow(**fields)


def run_row(**overrides: Any) -> RunRow:
    fields: dict[str, Any] = {
        "run_id": RUN_ID,
        "issue_number": 7,
        "issue_identifier": "repo-7",
        "attempt": 1,
        "session_id": "sess-7",
        "started_at": NOW - timedelta(hours=2),
        "ended_at": NOW - timedelta(hours=2) + timedelta(minutes=4),
        "outcome": "succeeded",
        "error": None,
        "turns": 1,
        "input_tokens": 513338,
        "output_tokens": 8425,
        "cost_usd": 0.8976,
        "duration_s": 205.4,
        "workspace_path": "/workspaces/repo-7",
    }
    fields.update(overrides)
    fields.setdefault("log_dir", f"/workspaces/repo-7/.issuebot/runs/{fields['run_id']}")
    return RunRow(**fields)


def turn_summary(**overrides: Any) -> TurnSummaryRow:
    fields: dict[str, Any] = {
        "run_id": RUN_ID,
        "turn_number": 1,
        "captured_at": NOW - timedelta(hours=2) + timedelta(minutes=4),
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
        "prompt_bytes": 10106,
        "stream_bytes": 115429,
        "stream_lines": 95,
        "omitted_lines": 0,
        "stderr_bytes": 0,
        "truncated": False,
    }
    fields.update(overrides)
    return TurnSummaryRow(**fields)


def turn_row(**overrides: Any) -> TurnRow:
    summary = turn_summary()
    fields: dict[str, Any] = {name: getattr(summary, name) for name in summary.__slots__}
    fields.update(
        {
            "prompt": "You are working on GitHub issue `repo-7` (#7).\n\n<b>bold</b>",
            "stream": "\n".join(
                [
                    '{"type":"system","subtype":"init","model":"claude-opus-5"}',
                    '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash",'
                    '"input":{"command":"ls <dir>"}}]}}',
                    '{"type":"user","message":{"content":[{"type":"tool_result",'
                    '"tool_use_id":"t","content":"README.md"}]}}',
                    '{"type":"rate_limit_event"}',
                    '{"type":"result","subtype":"success","result":"Done <script>x</script>"}',
                ]
            )
            + "\n",
            "stderr": "warning: something\n",
        }
    )
    fields.update(overrides)
    return TurnRow(**fields)


def event_row(**overrides: Any) -> EventRow:
    fields: dict[str, Any] = {
        "id": 11,
        "at": NOW - timedelta(hours=1),
        "kind": "run_ended",
        "issue_number": 7,
        "run_id": RUN_ID,
        "payload": {
            "kind": "run_ended",
            "run_id": RUN_ID,
            "outcome": "succeeded",
            "turns": 1,
            "cost_usd": 0.8976,
            "input_tokens": 513338,
            "output_tokens": 8425,
            "error": None,
        },
    }
    fields.update(overrides)
    return EventRow(**fields)


class Harness:
    def __init__(self) -> None:
        self.database = FakeDatabase()
        self.queries = self.database.queries_obj
        self.clock = Clock()
        self.client = TestClient(
            create_app(self.database, SETTINGS, clock=self.clock, now=self.clock.utcnow),
            raise_server_exceptions=False,
        )

    def seed_issue(self) -> None:
        self.queries.issue_rows[7] = issue_row()
        self.queries.runs_by_issue[7] = [run_row()]
        self.queries.turns_by_issue[7] = [turn_summary()]
        self.queries.turn_rows[(RUN_ID, 1)] = turn_row()
        self.queries.events_by_issue[7] = [
            event_row(),
            event_row(
                id=10,
                kind="state_changed",
                run_id=None,
                payload={
                    "kind": "state_changed",
                    "from_label": "issuebot/in-progress",
                    "to_label": "issuebot/review",
                    "actor": "agent",
                    "pr_url": "https://github.com/example/repo/pull/8",
                },
            ),
        ]
