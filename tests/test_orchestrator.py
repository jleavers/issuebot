"""Tests for the orchestrator against FakeGitHub, a scripted run_session and a fake clock."""

import asyncio
import io
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from issuebot.agent import ClaudeRunner, RunResult, SessionRecord, TurnEvent, WorkspaceManager
from issuebot.agent.runner import RateLimits, RateLimitWindow
from issuebot.agent.scrub import DEFAULT_SCRUBBER, Scrubber
from issuebot.agent.session import run_session
from issuebot.agent.workspace import workspace_key
from issuebot.config import Settings, load_workflow, overlay_path_for
from issuebot.events import (
    Blocked,
    Event,
    EventBus,
    IssueCancelled,
    IssueCompleted,
    PrOpened,
    StateChanged,
)
from issuebot.github import WORKPAD_MARKER, FakeGitHub, GhResult, GitHubError, Issue, StateLabel
from issuebot.github.status import MAX_DETAIL_CHARS
from issuebot.log import configure_logging
from issuebot.orchestrator import orchestrator as orchestrator_module
from issuebot.orchestrator.admission import IssueLedger
from issuebot.orchestrator.orchestrator import (
    CANDIDATE_STATES,
    MAX_FETCH_FAILURES,
    MAX_HOLD_ERROR_CHARS,
    MAX_UNREADABLE_AUTH_PROBES,
    OBSERVED_STATES,
    Orchestrator,
    OrchestratorStartupError,
    RunObserver,
    fetch_states,
    preflight,
)
from issuebot.orchestrator.state import RetryEntry, RunningEntry

START = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
START_MONO = 1000.0
LOGGED_IN = '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}'
LOGGED_OUT = '{"loggedIn": false, "authMethod": "none"}'
FAKE_CLAUDE = Path(__file__).parent / "fakes" / "claude"
posix = pytest.mark.skipif(sys.platform == "win32", reason="the fakes are POSIX scripts")

WORKFLOW_TEMPLATE = """---
github:
  repo: example/repo
polling:
  interval_ms: {interval_ms}
workspace:
  root: {root}
agent:
  max_concurrent_agents: {max_concurrent}
  max_turns: {max_turns}
  max_attempts: {max_attempts}
  max_retry_backoff_ms: {max_retry_backoff_ms}
  max_conflict_reworks: {max_conflict_reworks}
  max_issue_cost_usd: {max_issue_cost_usd}
claude:
  command: {claude}
  turn_timeout_ms: 30000
  stall_timeout_ms: {stall_timeout_ms}
{claude_extra}hooks:
  timeout_ms: 5000
{hooks}---
{prompt}
"""


class FakeClock:
    def __init__(self, start: float = START_MONO) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]

    def of(self, kind: type[Event]) -> list[Any]:
        return [event for event in self.events if isinstance(event, kind)]


class StubGh:
    async def run(self, args: list[str], *, stdin: str | None = None) -> GhResult:
        if list(args)[:2] == ["repo", "clone"]:
            subprocess.run(["git", "init", "-q", list(args)[3]], check=True)
        return GhResult(returncode=0, stdout="", stderr="")


class PendingRun:
    """One scripted worker session: the test decides when and how it ends."""

    def __init__(self, issue: Issue, workflow: Any, kwargs: dict[str, Any]) -> None:
        self.issue = issue
        self.workflow = workflow
        self.kwargs = kwargs
        self.future: asyncio.Future[RunResult] = asyncio.get_running_loop().create_future()

    @property
    def cancel(self) -> asyncio.Event:
        return self.kwargs["cancel"]

    @property
    def observer(self) -> RunObserver:
        return self.kwargs["observer"]

    def result(self, **overrides: Any) -> RunResult:
        fields: dict[str, Any] = {
            "run_id": self.kwargs["run_id"],
            "issue_number": self.issue.number,
            "issue_identifier": self.issue.identifier,
            "attempt": self.kwargs["attempt"],
            "session_id": self.kwargs.get("resume_session_id") or "sess",
            "outcome": "succeeded",
            "stop_reason": "issue_moved",
            "error_category": None,
            "error": None,
            "turns": 1,
            "input_tokens": 100,
            "output_tokens": 10,
            "cost_usd": 0.5,
            "duration_s": 12.0,
            "final_state": StateLabel.REVIEW,
            "final_issue": None,
            "workspace_path": Path("/workspaces") / self.issue.identifier,
            "log_dir": Path("/workspaces") / self.issue.identifier / ".issuebot/runs/run",
        }
        fields.update(overrides)
        return RunResult(**fields)

    def finish(self, **overrides: Any) -> None:
        self.future.set_result(self.result(**overrides))

    def fail(self, exc: BaseException) -> None:
        self.future.set_exception(exc)


class ScriptedSessions:
    """A run_session substitute: every call registers a PendingRun the test completes."""

    def __init__(self) -> None:
        self.runs: list[PendingRun] = []

    async def __call__(
        self, issue: Issue, workflow: Any, adapter: Any, bus: Any, **kwargs: Any
    ) -> RunResult:
        run = PendingRun(issue, workflow, kwargs)
        self.runs.append(run)
        waiter = asyncio.create_task(run.cancel.wait())
        try:
            await asyncio.wait({run.future, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not waiter.done():
                waiter.cancel()
        if run.future.done():
            return run.future.result()
        return run.result(
            outcome="cancelled",
            stop_reason="cancelled",
            error_category="cancelled",
            error="cancelled",
            turns=0,
            final_state=issue.state,
            final_issue=None,
        )


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        max_concurrent: int = 2,
        max_attempts: int = 3,
        max_turns: int = 3,
        stall_timeout_ms: int = 300_000,
        interval_ms: int = 30_000,
        max_retry_backoff_ms: int = 300_000,
        max_conflict_reworks: int = 3,
        max_issue_cost_usd: float = 0.0,
        hooks: dict[str, str] | None = None,
        prompt: str = "Task {{ issue.identifier }}",
        claude: str = "claude",
        real_sessions: bool = False,
        observe_issues: bool = False,
        model: str | None = None,
        model_labels: dict[str, str] | None = None,
        initial_rate_limits: RateLimits | None = None,
        initial_ledger: Mapping[str, IssueLedger] | None = None,
        scrubber: Scrubber = DEFAULT_SCRUBBER,
    ) -> None:
        self.tmp_path = tmp_path
        self.path = tmp_path / "WORKFLOW.md"
        self.root = tmp_path / "workspaces"
        self.claude = claude
        self.model = model
        self.model_labels = model_labels or {}
        self.runner_settings: list[Settings] = []
        self.workspace_settings: list[Settings] = []
        self.environ = {
            "GH_TOKEN": "fake-token",
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        self.write_workflow(
            max_concurrent=max_concurrent,
            max_attempts=max_attempts,
            max_turns=max_turns,
            stall_timeout_ms=stall_timeout_ms,
            interval_ms=interval_ms,
            max_retry_backoff_ms=max_retry_backoff_ms,
            max_conflict_reworks=max_conflict_reworks,
            max_issue_cost_usd=max_issue_cost_usd,
            hooks=hooks,
            prompt=prompt,
        )
        self.workflow = load_workflow(self.path, environ=self.environ)
        self.clock = FakeClock()
        self.github = FakeGitHub(self.workflow.config.github, now=self.now)
        self.sessions = ScriptedSessions()
        self.recorder = Recorder()
        self.bus = EventBus([self.recorder])
        self.snapshots: list[Any] = []
        self.polled: list[list[Issue]] = []
        self.which_missing: set[str] = set()
        self.claude_auth_output: str | None = LOGGED_IN
        self.claude_auth_calls: list[tuple[str, Mapping[str, str]]] = []
        # githubstatus.com never answers unless a test says so, so no test reaches the network
        # and the annotation is absent exactly where it is not being exercised (#88).
        self.github_status_output: str | None = None
        self.github_status_calls = 0
        self.orchestrator = Orchestrator(
            self.workflow,
            bus=self.bus,
            adapter_factory=lambda _settings: self.github,
            workspaces_factory=self.make_workspaces,
            runner_factory=self.make_runner,
            run_session=run_session if real_sessions else self.sessions,
            which=self.which,
            claude_auth=self.claude_auth,
            github_status=self.github_status,
            clock=self.clock,
            now=self.now,
            environ=self.environ,
            on_snapshot=self.snapshots.append,
            on_issues=self.record_polled if observe_issues else None,
            initial_rate_limits=initial_rate_limits,
            initial_ledger=initial_ledger,
            scrubber=scrubber,
        )

    def record_polled(self, issues: Any) -> None:
        self.polled.append(list(issues))

    # --- construction helpers ---------------------------------------------------------

    def now(self) -> datetime:
        return START + timedelta(seconds=self.clock.value - START_MONO)

    def which(self, name: str) -> str | None:
        return None if name in self.which_missing else f"/usr/bin/{name}"

    def claude_auth(
        self, command: str, environ: Mapping[str, str], *, run_as: str | None = None
    ) -> str | None:
        self.claude_auth_calls.append((command, environ))
        return self.claude_auth_output

    def github_status(self) -> str | None:
        self.github_status_calls += 1
        return self.github_status_output

    def make_runner(self, settings: Settings) -> ClaudeRunner:
        self.runner_settings.append(settings)
        return ClaudeRunner(settings, environ=self.environ)

    def make_workspaces(self, settings: Settings) -> WorkspaceManager:
        # Recorded like `runner_settings`: the manager's own `agent.run_as` is what decides the
        # account its `~/.claude` sweep clears and whose uid its `Boundary` will accept (#121),
        # so a test needs to be able to see what the orchestrator narrowed it to.
        self.workspace_settings.append(settings)
        return WorkspaceManager(
            settings, gh=StubGh(), environ=self.environ, hook_shell=("bash", "-c")
        )

    def write_workflow(
        self,
        *,
        max_concurrent: int = 2,
        max_attempts: int = 3,
        max_turns: int = 3,
        stall_timeout_ms: int = 300_000,
        interval_ms: int = 30_000,
        max_retry_backoff_ms: int = 300_000,
        max_conflict_reworks: int = 3,
        max_issue_cost_usd: float = 0.0,
        hooks: dict[str, str] | None = None,
        prompt: str = "Task {{ issue.identifier }}",
        text: str | None = None,
    ) -> None:
        hook_lines = "".join(f"  {name}: {script}\n" for name, script in (hooks or {}).items())
        content = text or WORKFLOW_TEMPLATE.format(
            interval_ms=interval_ms,
            root=self.root,
            max_concurrent=max_concurrent,
            max_turns=max_turns,
            max_attempts=max_attempts,
            max_retry_backoff_ms=max_retry_backoff_ms,
            max_conflict_reworks=max_conflict_reworks,
            max_issue_cost_usd=max_issue_cost_usd,
            claude=self.claude,
            claude_extra=self.claude_extra(),
            stall_timeout_ms=stall_timeout_ms,
            hooks=hook_lines,
            prompt=prompt,
        )
        self.path.write_text(content, encoding="utf-8")
        # Force a distinct mtime so a rewrite within the same tick is noticed.
        previous = getattr(self, "_mtime", 1_700_000_000)
        self._mtime = previous + 1
        os.utime(self.path, ns=(self._mtime * 1_000_000_000, self._mtime * 1_000_000_000))

    def write_overlay(self, text: str) -> Path:
        """The local overlay beside the workflow file, with a forced distinct mtime."""
        overlay = overlay_path_for(self.path)
        overlay.write_text(text, encoding="utf-8")
        previous = getattr(self, "_overlay_mtime", 1_700_000_000)
        self._overlay_mtime = previous + 1
        os.utime(overlay, ns=(self._overlay_mtime * 1_000_000_000,) * 2)
        return overlay

    def claude_extra(self) -> str:
        lines = [f"  model: {self.model}\n"] if self.model else []
        if self.model_labels:
            lines.append("  model_labels:\n")
            lines += [f"    {name}: {model}\n" for name, model in self.model_labels.items()]
        return "".join(lines)

    @property
    def labels(self) -> Any:
        return self.workflow.config.github.labels

    def add_issue(
        self,
        number: int,
        state: str = "todo",
        *,
        title: str | None = None,
        extra_labels: tuple[str, ...] = (),
    ) -> Issue:
        label = getattr(self.labels, state)
        return self.github.add_issue(
            title or f"Issue {number}", labels=(label, *extra_labels), number=number
        )

    def add_conflicting_review(self, number: int, *, pr_number: int) -> Issue:
        issue = self.add_issue(number, "review")
        self.github.open_pr(number, pr_number=pr_number)
        self.github.set_pr_mergeable(pr_number, "conflicting")
        return issue

    def workspace_dir(self, identifier: str) -> Path:
        path = self.root / identifier
        (path / ".git").mkdir(parents=True, exist_ok=True)
        (path / ".issuebot").mkdir(exist_ok=True)
        return path

    def write_session(
        self,
        identifier: str,
        *,
        issue_number: int,
        attempt: int = 1,
        session_id: str = "sess-1",
        last_outcome: str | None = None,
    ) -> None:
        path = self.workspace_dir(identifier)
        self.make_workspaces(self.workflow.config).write_session(
            path,
            SessionRecord(
                issue_number=issue_number,
                issue_identifier=identifier,
                run_id="run-old",
                session_id=session_id,
                attempt=attempt,
                turn_number=1,
                last_outcome=last_outcome,  # type: ignore[arg-type]
                updated_at=START,
            ),
        )

    # --- driving ----------------------------------------------------------------------

    async def tick(self) -> None:
        await self.orchestrator.tick()
        await asyncio.sleep(0)  # let freshly dispatched worker tasks start

    async def exit(self, run: PendingRun, **overrides: Any) -> None:
        """Finish a scripted run and let the orchestrator handle the exit."""
        run.finish(**overrides)
        await self.drain()

    async def drain(self) -> None:
        """Handle every worker exit that has been posted to the queue."""
        for _ in range(10):  # the finished session, its task and the done-callback each need a turn
            await asyncio.sleep(0)
        queue = self.orchestrator._queue
        while not queue.empty():
            message = queue.get_nowait()
            if isinstance(message, orchestrator_module._WorkerExited):
                await self.orchestrator.handle_worker_exit(message.issue_id)

    async def fire(self, seconds: float) -> None:
        self.clock.advance(seconds)
        await self.orchestrator.fire_due_retries()
        await asyncio.sleep(0)

    def run_for(self, number: int) -> PendingRun:
        return next(run for run in reversed(self.sessions.runs) if run.issue.number == number)

    def entry(self, number: int) -> RunningEntry:
        return self.orchestrator.running[str(number)]

    def retry(self, number: int) -> Any:
        return self.orchestrator.retries[str(number)]

    def calls(self, name: str) -> list[tuple[Any, ...]]:
        return [args for called, args in self.github.calls if called == name]

    def fail_on(self, method: str, monkeypatch: pytest.MonkeyPatch) -> None:
        original = getattr(self.github, method)

        async def failing(*args: Any, **kwargs: Any) -> Any:
            self.github.fail_next("transport")
            return await original(*args, **kwargs)

        monkeypatch.setattr(self.github, method, failing)


def activity(turn: int = 1, kind: str = "turn_activity", **fields: Any) -> TurnEvent:
    return TurnEvent(kind=kind, turn_number=turn, **fields)  # type: ignore[arg-type]


# --- preflight, observer, startup -------------------------------------------------------


def test_preflight_names_every_problem(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert preflight(h.workflow.config, which=h.which) == []
    h.which_missing = {"claude", "gh"}
    no_token = load_workflow(h.path, environ={"PATH": "/bin"}).config
    problems = preflight(no_token, which=h.which)
    assert problems == [
        "claude.command 'claude' not found on PATH",
        "'gh' not found on PATH",
        "github.token not set; export GH_TOKEN or set github.token: $VAR",
    ]


def test_run_observer_feeds_the_entry(tmp_path: Path, make_issue: Any) -> None:
    h = Harness(tmp_path)
    entry = RunningEntry(
        issue=make_issue(),
        attempt=1,
        rework=False,
        resumed=False,
        run_id="run-1",
        started_mono=h.clock(),
        started_at=h.now(),
        cancel=asyncio.Event(),
    )
    observer = RunObserver(entry, clock=h.clock, now=h.now)
    h.clock.advance(5)
    observer.on_turn_event(activity(kind="session_started", session_id="sess-9"))
    assert (entry.session_id, entry.last_event) == ("sess-9", "session_started")
    assert entry.last_activity_mono == START_MONO + 5
    assert entry.last_activity_at == START + timedelta(seconds=5)
    observer.on_turn_event(activity(tool_name="Read", message_type="assistant"))
    assert entry.last_event == "turn_activity:Read"
    observer.on_turn_event(activity(message_type="user"))
    assert entry.last_event == "turn_activity:user"
    observer.on_turn_event(activity(turn=2, kind="turn_completed"))
    assert (entry.turns, entry.last_event) == (2, "turn_completed")


def rate_limits(five: float = 0.42, seven: float = 0.32, *, at: datetime = START) -> RateLimits:
    return RateLimits(
        five_hour=RateLimitWindow(utilization=five, resets_at=at + timedelta(hours=2)),
        seven_day=RateLimitWindow(utilization=seven, resets_at=at + timedelta(days=3)),
        observed_at=at,
    )


def test_run_observer_reports_rate_limits(tmp_path: Path, make_issue: Any) -> None:
    """An account-wide reading goes to the callback, not into the per-issue entry."""
    h = Harness(tmp_path)
    entry = RunningEntry(
        issue=make_issue(),
        attempt=1,
        rework=False,
        resumed=False,
        run_id="run-1",
        started_mono=h.clock(),
        started_at=h.now(),
        cancel=asyncio.Event(),
    )
    seen: list[RateLimits] = []
    observer = RunObserver(entry, clock=h.clock, now=h.now, on_rate_limits=seen.append)
    limits = rate_limits()
    observer.on_turn_event(activity(kind="rate_limits", rate_limits=limits))
    assert seen == [limits]
    assert entry.last_event == "rate_limits"
    assert entry.last_activity_mono == START_MONO


def test_run_observer_without_a_callback_still_records_activity(
    tmp_path: Path, make_issue: Any
) -> None:
    h = Harness(tmp_path)
    entry = RunningEntry(
        issue=make_issue(),
        attempt=1,
        rework=False,
        resumed=False,
        run_id="run-1",
        started_mono=h.clock(),
        started_at=h.now(),
        cancel=asyncio.Event(),
    )
    observer = RunObserver(entry, clock=h.clock, now=h.now)
    observer.on_turn_event(activity(kind="rate_limits", rate_limits=rate_limits()))
    assert entry.last_event == "rate_limits"


@pytest.mark.parametrize(
    ("status", "credential"),
    [
        (LOGGED_IN, "subscription"),
        ('{"loggedIn": true, "authMethod": "oauth_token"}', "subscription"),
        (
            '{"loggedIn": true, "authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"}',
            "api_key",
        ),
        (
            '{"loggedIn": true, "authMethod": "claude.ai", "apiKeySource": "ANTHROPIC_API_KEY"}',
            "unknown",
        ),
        (None, "unknown"),
    ],
)
async def test_snapshot_names_the_credential_from_startup(
    tmp_path: Path, status: str | None, credential: str
) -> None:
    h = Harness(tmp_path)
    h.claude_auth_output = status
    await h.orchestrator.startup()
    assert h.orchestrator.snapshot().credential == credential


async def test_a_seeded_reading_shows_before_any_turn_has_run(tmp_path: Path) -> None:
    """A restart inherits the last reading, so the tile is not blank until the next dispatch."""
    inherited = rate_limits(0.42, at=START - timedelta(hours=1))
    h = Harness(tmp_path, initial_rate_limits=inherited)
    assert h.orchestrator.snapshot().rate_limits == inherited
    h.add_issue(1, "todo")
    await h.tick()
    fresh = rate_limits(0.55)
    h.run_for(1).observer.on_turn_event(activity(kind="rate_limits", rate_limits=fresh))
    assert h.orchestrator.snapshot().rate_limits == fresh, "a live reading replaces the seed"


async def test_snapshot_carries_the_latest_rate_limit_reading(tmp_path: Path) -> None:
    """Readings are account-wide, so the newest wins and it outlives the run that saw it."""
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    assert h.orchestrator.snapshot().rate_limits is None
    run = h.run_for(1)
    later = START + timedelta(seconds=30)
    run.observer.on_turn_event(
        activity(kind="rate_limits", rate_limits=rate_limits(0.55, at=later))
    )
    run.observer.on_turn_event(activity(kind="rate_limits", rate_limits=rate_limits(0.10)))
    limits = h.orchestrator.snapshot().rate_limits
    assert limits is not None and limits.five_hour is not None
    assert limits.five_hour.utilization == 0.55, "an older reading must not overwrite a newer one"
    await h.exit(run)
    kept = h.orchestrator.snapshot().rate_limits
    assert kept is not None and kept.five_hour is not None
    assert kept.five_hour.utilization == 0.55


async def test_startup_succeeds_and_logs(tmp_path: Path) -> None:
    stream = io.StringIO()
    configure_logging(fmt="json", level="INFO", stream=stream)
    h = Harness(tmp_path)
    await h.orchestrator.startup()
    assert [name for name, _ in h.github.calls] == ["auth_status", "missing_labels"]
    # The probe runs the resolved command under the worker's own environment, once.
    assert h.claude_auth_calls == [("/usr/bin/claude", h.environ)]
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    started = [line for line in lines if line["event"] == "orchestrator_started"]
    assert started[0]["claude_auth"] == "logged in (claude.ai, max)"
    assert not [line for line in lines if line["event"] == "orchestrator_startup_warning"]


async def test_startup_fails_when_claude_is_logged_out(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.claude_auth_output = LOGGED_OUT
    with pytest.raises(OrchestratorStartupError) as exc:
        await h.orchestrator.startup()
    assert exc.value.problems == [
        "claude auth: not logged in; run claude auth login or set ANTHROPIC_API_KEY"
    ]
    # Nothing was fetched or claimed: the probe ran after the gh probes and before any tick.
    assert [name for name, _ in h.github.calls] == ["auth_status", "missing_labels"]


@pytest.mark.parametrize(
    ("output", "warning"),
    [
        (None, "could not read auth status (no output)"),
        ("error: unknown command auth\n", "could not read auth status (unparseable output "),
        (
            '{"loggedIn": true, "authMethod": "claude.ai", "apiKeySource": "ANTHROPIC_API_KEY"}',
            "logged in (claude.ai) with ANTHROPIC_API_KEY also set; ",
        ),
    ],
)
async def test_startup_warns_but_starts_when_the_claude_probe_is_inconclusive(
    tmp_path: Path, output: str | None, warning: str
) -> None:
    """A timeout, an older claude, or an ambiguous login warns; only a definite logout fails."""
    stream = io.StringIO()
    configure_logging(fmt="json", level="INFO", stream=stream)
    h = Harness(tmp_path)
    h.claude_auth_output = output
    await h.orchestrator.startup()
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    warned = [line for line in lines if line["event"] == "orchestrator_startup_warning"]
    assert len(warned) == 1
    assert warned[0]["claude_auth"].startswith(warning)
    started = [line for line in lines if line["event"] == "orchestrator_started"]
    assert started[0]["claude_auth"] == warned[0]["claude_auth"]


async def test_startup_skips_the_claude_probe_when_preflight_fails(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.which_missing = {"claude"}
    h.claude_auth_output = LOGGED_OUT
    with pytest.raises(OrchestratorStartupError) as exc:
        await h.orchestrator.startup()
    assert exc.value.problems == ["claude.command 'claude' not found on PATH"]
    assert h.claude_auth_calls == []


async def test_startup_fails_on_preflight_auth_or_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.which_missing = {"gh"}
    with pytest.raises(OrchestratorStartupError) as exc:
        await h.orchestrator.startup()
    assert exc.value.problems == ["'gh' not found on PATH"]
    h.which_missing = set()
    with monkeypatch.context() as patch:
        h.fail_on("auth_status", patch)
        with pytest.raises(OrchestratorStartupError) as exc:
            await h.orchestrator.startup()
    assert exc.value.problems[0].startswith("gh auth: injected transport failure")
    assert "gh auth login" in exc.value.problems[0]
    h.github.repo_labels.pop("issuebot/review")
    with pytest.raises(OrchestratorStartupError) as exc:
        await h.orchestrator.startup()
    assert exc.value.problems == ["labels missing: issuebot/review; run issuebot labels ensure"]
    # Every probe reports, so one restart fixes everything at once.
    h.claude_auth_output = LOGGED_OUT
    with pytest.raises(OrchestratorStartupError) as exc:
        await h.orchestrator.startup()
    assert exc.value.problems == [
        "labels missing: issuebot/review; run issuebot labels ensure",
        "claude auth: not logged in; run claude auth login or set ANTHROPIC_API_KEY",
    ]


# --- dispatch -----------------------------------------------------------------------------


async def test_dispatch_order_claims_and_slots(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_concurrent=2)
    h.add_issue(1, "todo")
    h.clock.advance(1)
    h.add_issue(2, "todo")
    h.add_issue(3, "rework")
    h.add_issue(4, "in_progress")
    await h.tick()
    assert sorted(h.orchestrator.running) == ["3", "4"]
    assert [run.issue.number for run in h.sessions.runs] == [4, 3]
    assert h.calls("set_state") == [(3, StateLabel.IN_PROGRESS)]
    assert h.github.issue(3).state is StateLabel.IN_PROGRESS
    assert h.run_for(3).kwargs["rework"] is True
    assert h.run_for(4).kwargs["rework"] is False
    assert h.run_for(3).issue.state is StateLabel.IN_PROGRESS
    assert h.recorder.kinds == ["state_changed"]
    assert h.snapshots[-1].counters.runs_started == 2
    await h.tick()
    assert len(h.sessions.runs) == 2
    assert h.github.issue(1).state is StateLabel.TODO
    assert h.github.issue(2).state is StateLabel.TODO


async def test_a_model_label_picks_the_model_for_that_issue(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        max_concurrent=2,
        model="opus",
        model_labels={"issuebot/model/sonnet": "sonnet"},
    )
    h.add_issue(1, "todo", extra_labels=("issuebot/model/sonnet",))
    h.clock.advance(1)
    h.add_issue(2, "todo")
    await h.tick()
    assert [run.issue.number for run in h.sessions.runs] == [1, 2]
    assert [settings.claude.model for settings in h.runner_settings] == ["sonnet", "opus"]


async def test_non_candidates_are_skipped(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_concurrent=5)
    h.add_issue(1, "review")
    h.github.add_issue("Unlabelled", number=2)
    h.github.add_issue("Conflict", labels=("issuebot/todo", "issuebot/rework"), number=3)
    h.add_issue(4, "todo")
    await h.tick()
    assert list(h.orchestrator.running) == ["4"]
    await h.tick()
    assert len(h.sessions.runs) == 1


async def test_claim_failure_aborts_the_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    with monkeypatch.context() as patch:
        h.fail_on("set_state", patch)
        await h.tick()
    assert h.orchestrator.running == {}
    assert h.sessions.runs == []
    assert h.github.issue(1).state is StateLabel.TODO
    await h.tick()
    assert list(h.orchestrator.running) == ["1"]


async def test_candidate_fetch_failure_skips_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    with monkeypatch.context() as patch:
        h.fail_on("fetch_issues_by_states", patch)
        await h.tick()
    assert h.sessions.runs == []
    assert h.snapshots[-1].tick_count == 1


async def test_worker_receives_the_dispatch_context(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    run = h.run_for(1)
    assert run.workflow is h.workflow
    assert run.issue.state is StateLabel.IN_PROGRESS
    assert run.issue.labels == ("issuebot/in-progress",)
    assert run.kwargs["attempt"] == 1
    assert run.kwargs["rework"] is False
    assert run.kwargs["resume_session_id"] is None
    assert isinstance(run.kwargs["cancel"], asyncio.Event)
    assert isinstance(run.kwargs["observer"], RunObserver)
    assert isinstance(run.kwargs["runner"], ClaudeRunner)
    assert isinstance(run.kwargs["workspaces"], WorkspaceManager)
    assert run.kwargs["run_id"].startswith("20260903T120000Z-")
    entry = h.entry(1)
    assert entry.run_id == run.kwargs["run_id"]
    assert (entry.attempt, entry.resumed, entry.started_at) == (1, False, START)


@pytest.mark.parametrize(
    ("last_outcome", "expected_attempt", "expected_resume"),
    [(None, 2, "sess-1"), ("cancelled", 2, "sess-1"), ("failed", 1, None), ("succeeded", 1, None)],
)
async def test_orphan_resume_follows_the_session_file(
    tmp_path: Path,
    last_outcome: str | None,
    expected_attempt: int,
    expected_resume: str | None,
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "in_progress")
    h.write_session("repo-1", issue_number=1, attempt=2, last_outcome=last_outcome)
    await h.tick()
    run = h.run_for(1)
    assert run.kwargs["attempt"] == expected_attempt
    assert run.kwargs["resume_session_id"] == expected_resume
    assert h.entry(1).resumed is (expected_resume is not None)
    assert h.calls("set_state") == []


async def test_orphan_with_a_foreign_session_file_starts_fresh(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "in_progress")
    h.write_session("repo-1", issue_number=99)
    await h.tick()
    assert h.run_for(1).kwargs["resume_session_id"] is None
    assert h.run_for(1).kwargs["attempt"] == 1


async def test_workspace_path_error_skips_the_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from issuebot.agent import AgentError

    h = Harness(tmp_path)
    h.add_issue(1, "in_progress")

    def refuse(identifier: str) -> Path:
        raise AgentError("workspace_error", "escapes the root")

    monkeypatch.setattr(h.orchestrator._workspaces, "path_for", refuse)
    await h.tick()
    assert h.sessions.runs == []


# --- worker exits and retries -----------------------------------------------------------


async def test_a_freed_slot_dispatches_the_oldest_todo_next(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_concurrent=2)
    h.add_issue(1, "todo")
    h.clock.advance(1)
    h.add_issue(2, "todo")
    h.add_issue(3, "rework")
    h.add_issue(4, "in_progress")
    await h.tick()
    assert sorted(h.orchestrator.running) == ["3", "4"]
    h.github.human_set_state(4, StateLabel.REVIEW)
    await h.exit(h.run_for(4), final_issue=h.github.issue(4))
    assert "4" not in h.orchestrator.running
    await h.tick()
    assert [run.issue.number for run in h.sessions.runs] == [4, 3, 1]
    assert h.github.issue(2).state is StateLabel.TODO


async def test_normal_exit_to_review_schedules_a_continuation_that_releases(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.open_pr(1, pr_number=2)
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1))
    assert h.orchestrator.running == {}
    retry = h.retry(1)
    assert (retry.kind, retry.attempt, retry.error) == ("continuation", 1, None)
    assert retry.due_mono == START_MONO + 1
    assert retry.due_at == START + timedelta(seconds=1)
    assert h.recorder.kinds == ["state_changed", "state_changed", "pr_opened"]
    agent_move = h.recorder.of(StateChanged)[1]
    assert (agent_move.actor, agent_move.to_label) == ("agent", "issuebot/review")
    assert agent_move.pr_url == "https://github.com/example/repo/pull/2"
    snapshot = h.orchestrator.snapshot()
    assert snapshot.counters.runs_ended == 1
    assert (snapshot.totals.input_tokens, snapshot.totals.cost_usd) == (100, 0.5)
    assert snapshot.totals.seconds_running == 12.0
    await h.fire(0.5)
    assert "1" in h.orchestrator.retries
    await h.fire(0.5)
    assert h.orchestrator.retries == {}
    assert len(h.sessions.runs) == 1


async def test_normal_exit_to_todo_redispatches_a_fresh_attempt(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.human_set_state(1, StateLabel.TODO)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1), final_state=StateLabel.TODO)
    assert h.recorder.of(StateChanged)[-1].actor == "human"
    await h.fire(1)
    assert len(h.sessions.runs) == 2
    assert h.run_for(1).kwargs["attempt"] == 1
    assert h.calls("set_state") == [(1, StateLabel.IN_PROGRESS), (1, StateLabel.IN_PROGRESS)]


async def test_failure_backoff_doubles_and_caps_then_escapes(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_attempts=3, max_retry_backoff_ms=30_000)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(
        h.run_for(1),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom",
        final_state=StateLabel.IN_PROGRESS,
    )
    retry = h.retry(1)
    assert (retry.kind, retry.attempt, retry.error) == ("failure", 2, "process_exit: boom")
    assert retry.due_mono == START_MONO + 20
    await h.fire(19)
    assert len(h.sessions.runs) == 1
    await h.fire(1)
    assert len(h.sessions.runs) == 2
    assert h.run_for(1).kwargs["attempt"] == 2
    assert h.run_for(1).kwargs["resume_session_id"] is None
    assert h.calls("set_state") == [(1, StateLabel.IN_PROGRESS)]
    await h.exit(
        h.run_for(1),
        outcome="timed_out",
        stop_reason="failure",
        error_category="turn_timeout",
        error="no output for 3600s",
        final_state=StateLabel.IN_PROGRESS,
    )
    retry = h.retry(1)
    assert (retry.attempt, retry.due_mono - h.clock()) == (3, 30.0)
    await h.fire(30)
    assert h.run_for(1).kwargs["attempt"] == 3
    await h.exit(
        h.run_for(1),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom again",
        final_state=StateLabel.IN_PROGRESS,
        turns=2,
    )
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is StateLabel.REVIEW
    body = h.github.comments_for(1)[0].body
    assert body.startswith(WORKPAD_MARKER)
    assert "3 consecutive worker sessions failed; last error: process_exit: boom again." in body
    assert "(attempt 3, 2 turns)" in body
    assert h.recorder.of(Blocked)[0].reason.startswith("3 consecutive worker sessions failed")
    assert h.orchestrator.snapshot().counters.blocked == 1


async def test_the_blocked_escape_scrubs_what_it_writes_on_the_issue(tmp_path: Path) -> None:
    """The workpad block and the Blocked event are public (#91): the reason quotes the run's
    error, which the runner scrubbed at its source, and the log directory names the operator's
    home, which only the deployment's scrubber can read as `~`."""
    scrubber = Scrubber(secrets=["literal-token-value"], home="/workspaces")
    h = Harness(tmp_path, max_attempts=1, scrubber=scrubber)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(
        h.run_for(1),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="worker crashed: literal-token-value under /workspaces/repo-1",
        final_state=StateLabel.IN_PROGRESS,
    )
    assert h.github.issue(1).state is StateLabel.REVIEW
    body = h.github.comments_for(1)[0].body
    assert "last error: process_exit: worker crashed: *** under ~/repo-1." in body
    assert "logs: `~/repo-1/.issuebot/runs/run`" in body
    assert "literal-token-value" not in body and "/workspaces" not in body
    reason = h.recorder.of(Blocked)[0].reason
    assert "literal-token-value" not in reason and "*** under ~/repo-1" in reason


async def test_a_retrys_error_is_scrubbed_before_it_reaches_the_snapshot(tmp_path: Path) -> None:
    """`worker crashed: <exc>` names whatever the exception did; the retry's error is what
    `issuebot status`, `/state` and the dashboard show (#91)."""
    scrubber = Scrubber(secrets=["literal-token-value"], home="/workspaces")
    h = Harness(tmp_path, max_attempts=3, scrubber=scrubber)
    h.add_issue(1, "todo")
    await h.tick()
    h.run_for(1).fail(OSError("cannot write /workspaces/repo-1/x: literal-token-value"))
    await h.drain()
    assert h.retry(1).error == "worker crashed: cannot write ~/repo-1/x: ***"
    (retry,) = h.orchestrator.snapshot().retrying
    assert retry.error == h.retry(1).error


async def test_max_turns_while_in_progress_escapes_at_once(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.github.comment(1, f"{WORKPAD_MARKER}\n\n### Plan\n")
    await h.exit(
        h.run_for(1),
        stop_reason="max_turns",
        final_state=StateLabel.IN_PROGRESS,
        final_issue=h.github.issue(1),
        turns=3,
    )
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is StateLabel.REVIEW
    comments = h.github.comments_for(1)
    assert len(comments) == 1
    assert "Turn budget exhausted: 3 turns in attempt 1 without reaching `issuebot/review`." in (
        comments[0].body
    )
    assert h.recorder.kinds == ["state_changed", "state_changed", "blocked"]
    assert h.recorder.of(StateChanged)[1].actor == "issuebot"


async def test_a_blocked_stop_while_in_progress_escapes_with_the_agents_reason(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.github.comment(1, f"{WORKPAD_MARKER}\n\n### Plan\n")
    reason = "GitHub Actions is not allocating runners; a human must clear the billing hold."
    await h.exit(
        h.run_for(1),
        stop_reason="blocked",
        blocker=reason,
        final_state=StateLabel.IN_PROGRESS,
        final_issue=h.github.issue(1),
        turns=1,
    )
    assert h.orchestrator.retries == {}
    assert h.orchestrator.running == {}
    assert h.github.issue(1).state is StateLabel.REVIEW
    body = h.github.comments_for(1)[0].body
    assert "### Issuebot blocked (" in body
    assert f"\n\n{reason}\n" in body
    assert "(attempt 1, 1 turn)" in body
    assert "Turn budget" not in body
    assert h.recorder.kinds == ["state_changed", "state_changed", "blocked"]
    assert h.recorder.of(Blocked)[0].reason == reason
    assert h.recorder.of(StateChanged)[1].actor == "issuebot"
    assert h.orchestrator.snapshot().counters.blocked == 1


async def test_a_blocked_stop_without_a_line_still_escapes(tmp_path: Path) -> None:
    """Defensive: the session never produces this pair, but the escape must not write None."""
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(
        h.run_for(1),
        stop_reason="blocked",
        blocker=None,
        final_state=StateLabel.IN_PROGRESS,
        final_issue=h.github.issue(1),
    )
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.recorder.of(Blocked)[0].reason == "the session reported a blocker"


async def test_a_blocked_stop_after_the_label_moved_is_released(tmp_path: Path) -> None:
    """The label is the truth: if the agent handed off, there is nothing to escape."""
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.exit(
        h.run_for(1),
        stop_reason="blocked",
        blocker="written after the hand-off",
        final_state=StateLabel.REVIEW,
        final_issue=h.github.issue(1),
    )
    assert h.github.comments_for(1) == []
    assert h.recorder.of(Blocked) == []
    assert h.github.issue(1).state is StateLabel.REVIEW
    await h.fire(1.0)  # the continuation retry a succeeded exit queues; review releases it
    assert h.orchestrator.retries == {}
    assert h.orchestrator.running == {}


async def test_escape_failure_is_retried_with_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    run = h.run_for(1)
    with monkeypatch.context() as patch:
        h.fail_on("set_state", patch)
        await h.exit(run, stop_reason="max_turns", final_state=StateLabel.IN_PROGRESS, turns=3)
    retry = h.retry(1)
    assert (retry.kind, retry.attempt, retry.error) == ("escape", 1, "blocked escape failed")
    assert retry.due_mono == START_MONO + 10
    assert retry.escape is not None
    assert retry.escape.run_id == run.kwargs["run_id"]
    assert h.github.issue(1).state is StateLabel.IN_PROGRESS
    with monkeypatch.context() as patch:
        h.fail_on("set_state", patch)
        await h.fire(10)
    retry = h.retry(1)
    assert (retry.kind, retry.attempt, retry.due_mono - h.clock()) == ("escape", 2, 20.0)
    await h.fire(20)
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.github.comments_for(1)[0].body.count("### Issuebot blocked") == 1
    assert h.orchestrator.snapshot().counters.blocked == 1


async def test_retry_requeues_when_no_slot_is_free(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_concurrent=2)
    h.add_issue(1, "todo")
    h.add_issue(2, "todo")
    await h.tick()
    await h.exit(
        h.run_for(2),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom",
        final_state=StateLabel.IN_PROGRESS,
    )
    h.add_issue(3, "todo")
    await h.tick()
    assert sorted(h.orchestrator.running) == ["1", "3"]
    await h.fire(20)
    retry = h.retry(2)
    assert (retry.kind, retry.attempt, retry.error) == (
        "slots",
        2,
        "no available orchestrator slots",
    )
    assert retry.due_mono == h.clock() + 30
    assert len(h.sessions.runs) == 3


async def test_every_retry_kind_carries_the_issue_title(tmp_path: Path) -> None:
    """A queued retry knows the issue's title, not just its identifier (#42).

    The Retrying table renders it the way the Running table renders a title, so it has to
    survive every route a retry is queued by: `_schedule` builds one from the `Issue` it is
    given, and `_requeue` `replace`s an existing entry, which keeps it.
    """
    h = Harness(tmp_path, max_concurrent=1, max_attempts=3)
    h.add_issue(1, "todo", title="Add a power function")
    await h.tick()

    # continuation: the session reached `review` and the issue is polled once more.
    h.github.open_pr(1, pr_number=2)
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1))
    assert (h.retry(1).kind, h.retry(1).title) == ("continuation", "Add a power function")
    await h.fire(1)

    # failure: a run that ended badly with attempts left.
    h.github.human_set_state(1, StateLabel.TODO)
    await h.tick()
    await h.exit(
        h.run_for(1),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom",
        final_state=StateLabel.IN_PROGRESS,
    )
    assert (h.retry(1).kind, h.retry(1).title) == ("failure", "Add a power function")

    # auth: the requeue of a due retry while the credential is held.
    h.add_issue(5, "todo", title="Teach the parser about tabs")
    await h.tick()
    await h.exit(h.run_for(5), **AUTH_FAILURE)  # defined with the #20 tests further down
    await h.fire(20)
    assert (h.retry(1).kind, h.retry(1).title) == ("auth", "Add a power function")

    # slots: the requeue of a due retry with the one agent busy on another issue.
    h.claude_auth_output = LOGGED_IN
    h.add_issue(6, "todo", title="Vendor the stylesheet")
    await h.tick()
    assert list(h.orchestrator.running) == ["6"]
    await h.fire(30)
    assert (h.retry(1).kind, h.retry(1).title) == ("slots", "Add a power function")


async def test_an_escape_retry_carries_the_issue_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fifth kind: the blocked escape, both as scheduled and as requeued (#42)."""
    h = Harness(tmp_path)
    h.add_issue(1, "todo", title="Add a power function")
    await h.tick()
    with monkeypatch.context() as patch:
        h.fail_on("set_state", patch)
        await h.exit(
            h.run_for(1), stop_reason="max_turns", final_state=StateLabel.IN_PROGRESS, turns=3
        )
    assert (h.retry(1).kind, h.retry(1).title) == ("escape", "Add a power function")
    with monkeypatch.context() as patch:
        h.fail_on("set_state", patch)
        await h.fire(10)
    assert (h.retry(1).kind, h.retry(1).attempt) == ("escape", 2)
    assert h.retry(1).title == "Add a power function"


async def test_retry_refresh_failure_requeues_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(
        h.run_for(1),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom",
        final_state=StateLabel.IN_PROGRESS,
    )
    with monkeypatch.context() as patch:
        h.fail_on("fetch_issues_by_ids", patch)
        await h.fire(20)
    retry = h.retry(1)
    assert (retry.kind, retry.attempt) == ("failure", 2)
    assert retry.error is not None and retry.error.startswith("retry refresh failed: ")
    assert retry.due_mono == h.clock() + 30
    assert len(h.sessions.runs) == 1


async def test_retry_finds_the_issue_closed_or_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path, max_concurrent=3)
    h.add_issue(1, "todo")
    h.add_issue(2, "todo")
    await h.tick()
    for number in (1, 2):
        await h.exit(
            h.run_for(number),
            outcome="failed",
            stop_reason="failure",
            error_category="process_exit",
            error="boom",
            final_state=StateLabel.IN_PROGRESS,
        )
    h.workspace_dir("repo-1")
    h.github.close_issue(1)

    original = h.github.fetch_issues_by_ids

    async def hide_two(ids: Any) -> list[Issue]:
        return [issue for issue in await original(ids) if issue.number != 2]

    monkeypatch.setattr(h.github, "fetch_issues_by_ids", hide_two)
    await h.fire(20)
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is None
    assert isinstance(h.recorder.of(IssueCancelled)[0], IssueCancelled)
    assert not (h.root / "repo-1").exists()
    assert len(h.sessions.runs) == 2


async def test_crashed_worker_is_retried(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.clock.advance(7)
    h.run_for(1).fail(RuntimeError("kaboom"))
    await h.drain()
    retry = h.retry(1)
    assert (retry.kind, retry.attempt, retry.error) == ("failure", 2, "worker crashed: kaboom")
    snapshot = h.orchestrator.snapshot()
    assert snapshot.counters.runs_ended == 1
    assert snapshot.totals.seconds_running == 7.0


async def test_terminal_sweep_drops_a_retry_for_a_closed_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(
        h.run_for(1),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom",
        final_state=StateLabel.IN_PROGRESS,
    )
    h.github.close_issue(1)
    for _ in range(10):
        await h.tick()
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is None


# --- a credential that lapses mid-life (#20) ---------------------------------------------


AUTH_FAILURE = {
    "outcome": "failed",
    "stop_reason": "failure",
    "error_category": "auth_failed",
    "error": "error_during_execution: API Error: 401 authentication_error",
    "final_state": StateLabel.IN_PROGRESS,
}


async def test_an_auth_failure_escapes_at_once_and_names_authentication(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_attempts=3)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), turns=2, **AUTH_FAILURE)
    # No retry, even though two attempts were left: retrying cannot fix a credential.
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is StateLabel.REVIEW
    body = h.github.comments_for(1)[0].body
    assert "Claude could not authenticate in attempt 1" in body
    assert "API Error: 401 authentication_error" in body
    assert "resumes on its own once `claude auth status` reports a login" in body
    assert h.recorder.of(Blocked)[0].reason.startswith("Claude could not authenticate")
    assert h.orchestrator.snapshot().counters.blocked == 1


async def test_no_further_issue_is_claimed_while_the_credential_is_unusable(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **AUTH_FAILURE)
    h.add_issue(2, "todo")
    h.claude_auth_output = LOGGED_OUT
    await h.tick()
    assert h.claude_auth_calls == [("/usr/bin/claude", h.environ)]
    assert len(h.sessions.runs) == 1
    assert h.github.issue(2).state is StateLabel.TODO
    assert h.calls("set_state") == [(1, StateLabel.IN_PROGRESS), (1, StateLabel.REVIEW)]


async def test_dispatch_resumes_once_the_credential_works_again(tmp_path: Path) -> None:
    stream = io.StringIO()
    configure_logging(fmt="json", level="INFO", stream=stream)
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **AUTH_FAILURE)
    h.add_issue(2, "todo")
    h.claude_auth_output = LOGGED_OUT
    await h.tick()
    assert len(h.sessions.runs) == 1
    h.claude_auth_output = LOGGED_IN
    await h.tick()
    assert len(h.claude_auth_calls) == 2
    assert h.github.issue(2).state is StateLabel.IN_PROGRESS
    assert h.run_for(2).kwargs["attempt"] == 1
    # The hold is gone: a later tick does not probe again.
    await h.tick()
    assert len(h.claude_auth_calls) == 2
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    held = [line for line in lines if line["event"] == "dispatch_auth_held"]
    assert len(held) == 1
    assert held[0]["claude_auth"].startswith("not logged in")
    [recovered] = [line for line in lines if line["event"] == "dispatch_auth_recovered"]
    assert recovered["claude_auth"] == "logged in (claude.ai, max)"


@pytest.mark.parametrize(
    ("output", "held"),
    [
        (LOGGED_IN, False),
        # A login with an API key also set is still a login: dispatch resumes.
        (
            '{"loggedIn": true, "authMethod": "claude.ai", "apiKeySource": "ANTHROPIC_API_KEY"}',
            False,
        ),
        (LOGGED_OUT, True),
        # Unlike startup, an unreadable answer keeps the hold: a run has already failed, and
        # nothing here says that has changed.
        (None, True),
        ("not json at all", True),
    ],
)
async def test_only_a_probe_that_reports_a_login_lifts_the_hold(
    tmp_path: Path, output: str | None, held: bool
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **AUTH_FAILURE)
    h.add_issue(2, "todo")
    h.claude_auth_output = output
    await h.tick()
    assert (len(h.sessions.runs) == 1) is held


async def test_a_probe_that_never_answers_gives_up_so_a_wedged_claude_cannot_hold_forever(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    configure_logging(fmt="json", level="INFO", stream=stream)
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **AUTH_FAILURE)
    h.add_issue(2, "todo")
    h.claude_auth_output = None  # an older or wedged claude: no usable answer, ever
    for _ in range(MAX_UNREADABLE_AUTH_PROBES - 1):
        await h.tick()
        assert len(h.sessions.runs) == 1
    await h.tick()
    assert len(h.claude_auth_calls) == MAX_UNREADABLE_AUTH_PROBES
    assert h.github.issue(2).state is StateLabel.IN_PROGRESS
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    [given_up] = [line for line in lines if line["event"] == "dispatch_auth_hold_abandoned"]
    assert given_up["probes"] == MAX_UNREADABLE_AUTH_PROBES
    # Giving up is a fallback, not a recovery: nothing claims the credential works.
    assert [line for line in lines if line["event"] == "dispatch_auth_recovered"] == []


async def test_a_definite_logged_out_answer_never_runs_the_hold_out(tmp_path: Path) -> None:
    """Only probes that cannot answer are counted; "not logged in" holds for as long as it lasts."""
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **AUTH_FAILURE)
    h.add_issue(2, "todo")
    for index in range(MAX_UNREADABLE_AUTH_PROBES * 2):
        h.claude_auth_output = None if index % 2 else LOGGED_OUT
        await h.tick()
    assert len(h.sessions.runs) == 1
    assert h.github.issue(2).state is StateLabel.TODO


async def test_a_failed_escape_is_still_retried_while_authentication_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape needs GitHub, not claude, so the hold must not defer the blocker."""
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    with monkeypatch.context() as patch:
        h.fail_on("set_state", patch)
        await h.exit(h.run_for(1), **AUTH_FAILURE)
    retry = h.retry(1)
    assert (retry.kind, retry.attempt, retry.error) == ("escape", 1, "blocked escape failed")
    assert h.github.issue(1).state is StateLabel.IN_PROGRESS
    h.claude_auth_output = LOGGED_OUT
    await h.fire(10)
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is StateLabel.REVIEW
    body = h.github.comments_for(1)[0].body
    assert body.count("### Issuebot blocked") == 1
    assert "Claude could not authenticate in attempt 1" in body


async def test_a_due_retry_waits_while_authentication_is_held(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_attempts=3)
    h.add_issue(1, "todo")
    h.add_issue(2, "todo")
    await h.tick()
    await h.exit(
        h.run_for(2),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom",
        final_state=StateLabel.IN_PROGRESS,
    )
    assert h.retry(2).kind == "failure"
    await h.exit(h.run_for(1), **AUTH_FAILURE)
    await h.fire(20)
    retry = h.retry(2)
    assert (retry.kind, retry.attempt) == ("auth", 2)
    assert retry.error is not None and "claude authentication unavailable" in retry.error
    assert retry.due_mono == h.clock() + 30.0
    assert len(h.sessions.runs) == 2
    # The credential comes back: the tick lifts the hold and the retry runs.
    h.claude_auth_output = LOGGED_IN
    await h.tick()
    await h.fire(30)
    assert len(h.sessions.runs) == 3
    assert h.run_for(2).kwargs["attempt"] == 2


# --- reconcile ----------------------------------------------------------------------------


async def test_reconcile_with_nothing_running_makes_no_request(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.tick()
    h.github.calls.clear()
    await h.orchestrator.reconcile()
    assert h.github.calls == []


async def test_reconcile_refresh_failure_keeps_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.human_set_state(1, StateLabel.TODO)
    with monkeypatch.context() as patch:
        h.fail_on("fetch_issues_by_ids", patch)
        await h.tick()
    assert h.entry(1).stop_cause is None
    assert not h.entry(1).cancel.is_set()


async def test_reconcile_updates_the_snapshot_and_sees_the_pr(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.open_pr(1, pr_number=5)
    await h.tick()
    entry = h.entry(1)
    assert entry.issue.linked_pr is not None and entry.issue.linked_pr.number == 5
    assert entry.stop_cause is None
    assert h.recorder.of(PrOpened)[0].pr_number == 5
    await h.tick()
    assert len(h.recorder.of(PrOpened)) == 1


async def test_reconcile_gives_review_one_interval_of_grace(tmp_path: Path) -> None:
    h = Harness(tmp_path, interval_ms=30_000)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")
    h.github.human_set_state(1, StateLabel.REVIEW)
    h.clock.advance(30)
    await h.tick()
    entry = h.entry(1)
    assert entry.review_seen_mono == h.clock.value
    assert not entry.cancel.is_set()
    assert [event.actor for event in h.recorder.of(StateChanged)] == ["issuebot", "agent"]
    h.clock.advance(1)  # a refresh-driven tick inside the interval leaves the worker alone
    await h.tick()
    assert not entry.cancel.is_set()
    h.clock.advance(29)
    await h.tick()
    assert entry.cancel.is_set()
    assert (entry.stop_cause, entry.stop_detail) == ("moved", "review")
    await h.drain()
    assert h.orchestrator.running == {}
    assert h.orchestrator.retries == {}
    assert (h.root / "repo-1").is_dir()
    assert len(h.recorder.of(StateChanged)) == 2


@pytest.mark.parametrize("state", [StateLabel.TODO, StateLabel.REWORK])
async def test_reconcile_cancels_other_moves_at_once(tmp_path: Path, state: StateLabel) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.human_set_state(1, state)
    await h.tick()
    entry = h.entry(1)
    assert entry.cancel.is_set()
    assert (entry.stop_cause, entry.stop_detail) == ("moved", state.value)
    assert h.recorder.of(StateChanged)[-1].actor == "human"
    await h.drain()
    assert h.orchestrator.running == {}
    assert h.orchestrator.retries == {}


async def test_reconcile_cancels_an_unlabelled_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.human_remove_label(1, "issuebot/in-progress")
    await h.tick()
    assert (h.entry(1).stop_cause, h.entry(1).stop_detail) == ("moved", "unlabelled")


async def test_reconcile_completes_a_closed_issue_after_the_worker_exits(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")
    h.github.open_pr(1, pr_number=5)
    h.github.merge_pr(5)
    await h.tick()
    entry = h.entry(1)
    assert entry.stop_cause == "closed"
    assert entry.terminal_issue is not None
    assert (h.root / "repo-1").is_dir()
    await h.drain()
    assert h.orchestrator.running == {}
    assert h.github.issue(1).state is StateLabel.COMPLETE
    assert h.recorder.of(IssueCompleted)[0].pr_url == "https://github.com/example/repo/pull/5"
    assert not (h.root / "repo-1").exists()
    assert h.orchestrator.snapshot().counters.issues_completed == 1


async def test_reconcile_cancels_a_closed_unmerged_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")
    h.github.close_issue(1)
    await h.tick()
    await h.drain()
    assert h.github.issue(1).state is None
    assert len(h.recorder.of(IssueCancelled)) == 1
    assert not (h.root / "repo-1").exists()
    assert h.orchestrator.snapshot().counters.issues_cancelled == 1


async def test_reconcile_completes_a_closed_no_fault_issue(tmp_path: Path) -> None:
    """The #34 case end to end: closing a no-fault handoff counts, and keeps a state label."""
    h = Harness(tmp_path)
    h.add_issue(1, "review", extra_labels=("issuebot/no-fault",))
    h.github.close_issue(1)
    await h.tick()
    await h.drain()
    assert h.github.issue(1).state is StateLabel.COMPLETE
    assert h.recorder.of(IssueCancelled) == []
    completed = h.recorder.of(IssueCompleted)
    assert len(completed) == 1
    assert (completed[0].resolution, completed[0].pr_url) == ("no_change", None)
    counters = h.orchestrator.snapshot().counters
    assert (counters.issues_completed, counters.issues_cancelled) == (1, 0)


async def test_reconcile_releases_a_missing_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")

    async def nothing(ids: Any) -> list[Issue]:
        return []

    monkeypatch.setattr(h.github, "fetch_issues_by_ids", nothing)
    await h.tick()
    assert h.entry(1).stop_cause == "missing"
    await h.drain()
    assert h.orchestrator.running == {}
    assert h.orchestrator.retries == {}
    assert (h.root / "repo-1").is_dir()


async def test_stall_detection_kills_and_retries(tmp_path: Path) -> None:
    h = Harness(tmp_path, stall_timeout_ms=300_000)
    h.add_issue(1, "todo")
    await h.tick()
    run = h.run_for(1)
    h.clock.advance(200)
    run.observer.on_turn_event(activity(tool_name="Bash", message_type="assistant"))
    h.clock.advance(150)
    await h.tick()
    assert h.entry(1).stop_cause is None
    h.clock.advance(151)
    await h.tick()
    entry = h.entry(1)
    assert (entry.stop_cause, entry.stop_detail) == ("stalled", "no activity for 301 s")
    assert entry.cancel.is_set()
    await h.drain()
    retry = h.retry(1)
    assert (retry.kind, retry.attempt, retry.error) == (
        "failure",
        2,
        "stalled: no activity for 301 s",
    )


async def test_stall_detection_is_disabled_at_zero(tmp_path: Path) -> None:
    h = Harness(tmp_path, stall_timeout_ms=0)
    h.add_issue(1, "todo")
    await h.tick()
    h.clock.advance(100_000)
    await h.tick()
    assert h.entry(1).stop_cause is None


async def test_reconcile_releases_a_reopened_issue_without_finishing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")
    h.github.close_issue(1)
    await h.tick()
    entry = h.entry(1)
    assert entry.stop_cause == "closed"
    assert entry.terminal_issue is not None
    h.github.reopen_issue(1)
    await h.tick()
    assert entry.terminal_issue is None
    await h.drain()
    assert h.orchestrator.running == {}
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is StateLabel.IN_PROGRESS
    assert (h.root / "repo-1").is_dir()
    counters = h.orchestrator.snapshot().counters
    assert (counters.issues_completed, counters.issues_cancelled) == (0, 0)


# --- terminal sweep -----------------------------------------------------------------------


async def test_terminal_sweep_runs_on_the_first_and_every_tenth_tick(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "review")
    h.github.open_pr(1, pr_number=5)
    h.github.merge_pr(5)
    h.workspace_dir("repo-1")
    await h.tick()
    assert h.github.issue(1).state is StateLabel.COMPLETE
    assert len(h.recorder.of(IssueCompleted)) == 1
    assert not (h.root / "repo-1").exists()
    h.add_issue(2, "in_progress")
    h.github.close_issue(2)
    h.github.calls.clear()
    for _ in range(9):
        await h.tick()
    assert h.calls("fetch_terminal_issues") == []
    assert h.github.issue(2).state is StateLabel.IN_PROGRESS
    await h.tick()
    assert len(h.calls("fetch_terminal_issues")) == 1
    assert h.github.issue(2).state is None
    assert h.calls("set_state") == []
    assert len(h.recorder.of(IssueCompleted)) == 1


async def test_terminal_sweep_failure_only_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    with monkeypatch.context() as patch:
        h.fail_on("fetch_terminal_issues", patch)
        await h.tick()
    assert list(h.orchestrator.running) == ["1"]


# --- reload and preflight -----------------------------------------------------------------


async def test_reload_applies_interval_slots_and_prompt(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_concurrent=1)
    h.add_issue(1, "todo")
    h.add_issue(2, "todo")
    await h.tick()
    assert len(h.sessions.runs) == 1
    h.write_workflow(max_concurrent=2, interval_ms=60_000, prompt="New {{ issue.number }}")
    await h.tick()
    assert len(h.sessions.runs) == 2
    assert h.run_for(2).workflow.prompt_template == "New {{ issue.number }}"
    assert h.run_for(1).workflow.prompt_template == "Task {{ issue.identifier }}"
    snapshot = h.snapshots[-1]
    assert (snapshot.poll_interval_ms, snapshot.max_concurrent_agents) == (60_000, 2)
    assert snapshot.config_valid is True
    assert h.orchestrator.workflow.config.polling.interval_ms == 60_000


async def test_invalid_reload_keeps_the_last_good_workflow(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    h.write_workflow(text="---\ngithub:\n  repo: example/repo\nagent:\n  bogus: 1\n---\nbody\n")
    await h.tick()
    snapshot = h.snapshots[-1]
    assert snapshot.config_valid is False
    assert snapshot.config_error is not None and "bogus" in snapshot.config_error
    assert h.orchestrator.workflow is h.workflow
    assert list(h.orchestrator.running) == ["1"]
    h.write_workflow(max_concurrent=4)
    await h.tick()
    assert h.snapshots[-1].config_valid is True
    assert h.snapshots[-1].max_concurrent_agents == 4


async def test_a_stale_single_file_mount_is_reported_at_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path resolving to an unlinked inode is a bind mount serving a replaced file (#46)."""
    h = Harness(tmp_path)
    await h.tick()
    assert h.snapshots[-1].config_valid is True

    # The operator edits the file on the host and their editor saves it by rename, so the
    # host path gets a new inode. A single-file bind mount stays on the old one, which is
    # now unlinked: the container sees nlink 0 and an mtime that will never move again.
    h.write_workflow(max_concurrent=4)
    watched = h.orchestrator.workflow.path
    real_stat = Path.stat
    pinned = real_stat(watched)

    def stale_stat(self: Path, **kwargs: Any) -> Any:
        if self == watched:
            return SimpleNamespace(
                st_nlink=0,
                st_dev=pinned.st_dev,
                st_ino=pinned.st_ino,
                st_mtime_ns=h.workflow.source_mtime_ns,
            )
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", stale_stat)
    with capture_logs() as logs:
        await h.tick()

    complaint = next(entry for entry in logs if entry["event"] == "workflow_reload_failed")
    assert complaint["log_level"] == "error"
    assert "stale mount" in complaint["error"]
    snapshot = h.snapshots[-1]
    assert snapshot.config_valid is False
    assert "stale mount" in (snapshot.config_error or "")
    # It keeps running the settings it started with rather than pretending to be current.
    assert h.orchestrator.workflow is h.workflow
    assert snapshot.max_concurrent_agents == h.workflow.config.agent.max_concurrent_agents


async def test_a_file_mounted_singly_is_reported_before_it_goes_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file on a different device from its own directory is a mount point (#46)."""
    h = Harness(tmp_path)
    await h.tick()
    assert h.snapshots[-1].config_valid is True

    # The file keeps the identity it was loaded with -- a mounted file already had the
    # mount's device when it was read -- and its directory is the one on another device.
    watched = h.orchestrator.workflow.path
    real_stat = Path.stat

    def mounted_stat(self: Path, **kwargs: Any) -> Any:
        source = real_stat(self, **kwargs)
        if self != watched.parent:
            return source
        return SimpleNamespace(st_nlink=2, st_dev=source.st_dev + 1, st_ino=source.st_ino)

    monkeypatch.setattr(Path, "stat", mounted_stat)
    with capture_logs() as logs:
        await h.tick()

    complaint = next(entry for entry in logs if entry["event"] == "workflow_reload_failed")
    assert complaint["log_level"] == "error"
    assert "single-file mount" in complaint["error"]
    assert "single-file mount" in (h.snapshots[-1].config_error or "")


async def test_a_loose_link_count_never_suppresses_a_real_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The complaint is advisory: it is only reached when there is nothing to load (#46)."""
    h = Harness(tmp_path)
    await h.tick()
    h.write_workflow(max_concurrent=4)

    watched = h.orchestrator.workflow.path
    real_stat = Path.stat

    def zero_nlink_stat(self: Path, **kwargs: Any) -> Any:
        source = real_stat(self, **kwargs)
        if self != watched:
            return source
        return SimpleNamespace(
            st_nlink=0,
            st_dev=source.st_dev,
            st_ino=source.st_ino,
            st_mtime_ns=source.st_mtime_ns,
        )

    monkeypatch.setattr(Path, "stat", zero_nlink_stat)
    await h.tick()

    # The file really did change, so it is loaded and nothing is complained about.
    assert h.snapshots[-1].config_valid is True
    assert h.snapshots[-1].max_concurrent_agents == 4


async def test_reload_notices_a_replacement_that_kept_its_mtime(tmp_path: Path) -> None:
    """Identity is (dev, ino, mtime_ns): the mtime alone misses a timestamp-preserving save."""
    h = Harness(tmp_path)
    await h.tick()
    stamp = h.path.stat().st_mtime_ns
    ino = h.path.stat().st_ino

    # A restore from an archive, or a checkout that preserves timestamps: the content is
    # new, the inode is new, and the mtime is the one the old file already had.
    h.write_workflow(max_concurrent=4)
    replacement = tmp_path / "replacement.md"
    replacement.write_text(h.path.read_text(encoding="utf-8"), encoding="utf-8")
    os.replace(replacement, h.path)
    os.utime(h.path, ns=(stamp, stamp))
    assert h.path.stat().st_mtime_ns == stamp
    assert h.path.stat().st_ino != ino

    await h.tick()
    assert h.snapshots[-1].config_valid is True
    assert h.snapshots[-1].max_concurrent_agents == 4


async def test_reload_follows_the_overlay_being_created_edited_and_deleted(
    tmp_path: Path,
) -> None:
    """The overlay reloads on the same terms as the base: presence and identity."""
    h = Harness(tmp_path)
    await h.tick()
    assert h.snapshots[-1].max_concurrent_agents == 2
    assert h.snapshots[-1].workflow_overlay_path is None

    overlay = h.write_overlay("---\nagent:\n  max_concurrent_agents: 4\n---\n")
    with capture_logs() as logs:
        await h.tick()
    assert h.snapshots[-1].max_concurrent_agents == 4
    assert h.snapshots[-1].workflow_overlay_path == str(overlay)
    assert h.snapshots[-1].config_valid is True
    reloaded = next(entry for entry in logs if entry["event"] == "workflow_reloaded")
    assert (reloaded["overlay"], reloaded["changed"]) == (str(overlay), ["agent"])

    h.write_overlay("---\nagent:\n  max_concurrent_agents: 5\n---\n")
    await h.tick()
    assert h.snapshots[-1].max_concurrent_agents == 5

    overlay.unlink()
    await h.tick()
    assert h.snapshots[-1].max_concurrent_agents == 2
    assert h.snapshots[-1].workflow_overlay_path is None
    assert h.snapshots[-1].config_valid is True


async def test_an_invalid_overlay_keeps_the_last_good_workflow_and_names_it(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    await h.tick()
    h.write_overlay("---\nagent:\n  bogus: 1\n---\n")
    await h.tick()
    snapshot = h.snapshots[-1]
    assert snapshot.config_valid is False
    assert snapshot.config_error is not None
    assert "(+ WORKFLOW.local.md)" in snapshot.config_error and "bogus" in snapshot.config_error
    assert h.orchestrator.workflow is h.workflow
    assert snapshot.workflow_overlay_path is None


async def test_removing_a_bad_overlay_clears_the_error_it_caused(tmp_path: Path) -> None:
    """An error the overlay caused must not outlive the overlay (final review)."""
    h = Harness(tmp_path)
    await h.tick()
    overlay = h.write_overlay("---\nagent:\n  bogus: 1\n---\n")
    await h.tick()
    assert h.snapshots[-1].config_valid is False

    overlay.unlink()
    await h.tick()
    snapshot = h.snapshots[-1]
    assert snapshot.config_valid is True
    assert snapshot.config_error is None
    assert snapshot.workflow_overlay_path is None
    assert h.orchestrator.workflow is h.workflow

    # The same error must be reported again if the same bad overlay comes back.
    h.write_overlay("---\nagent:\n  bogus: 1\n---\n")
    with capture_logs() as logs:
        await h.tick()
    assert any(entry["event"] == "workflow_reload_failed" for entry in logs)
    assert h.snapshots[-1].config_valid is False


async def test_an_overlay_mounted_singly_is_reported_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overlay on a different device from its directory is a mount point too (#46)."""
    h = Harness(tmp_path)
    await h.tick()
    overlay = h.write_overlay("---\nagent:\n  max_concurrent_agents: 4\n---\n")
    real_stat = Path.stat

    def mounted_stat(self: Path, **kwargs: Any) -> Any:
        source = real_stat(self, **kwargs)
        if self != overlay:
            return source
        return SimpleNamespace(
            st_nlink=1,
            st_dev=source.st_dev + 1,
            st_ino=source.st_ino,
            st_mtime_ns=source.st_mtime_ns,
            st_mode=source.st_mode,
        )

    # Mounted from the start: the overlay is loaded with the mount's device, as a mounted
    # file would be, and on the next tick nothing has changed and the complaint runs.
    monkeypatch.setattr(Path, "stat", mounted_stat)
    await h.tick()
    assert h.snapshots[-1].max_concurrent_agents == 4
    assert h.snapshots[-1].config_valid is True
    with capture_logs() as logs:
        await h.tick()
    complaint = next(entry for entry in logs if entry["event"] == "workflow_reload_failed")
    assert complaint["log_level"] == "error"
    assert "single-file mount" in complaint["error"]
    assert str(overlay) in complaint["error"]
    assert str(h.path) not in complaint["error"]
    assert "single-file mount" in (h.snapshots[-1].config_error or "")
    # Advisory: the settings in force are still the overlay's.
    assert h.snapshots[-1].max_concurrent_agents == 4


async def test_an_overlay_that_cannot_be_stated_is_a_reload_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a missing overlay means "no overlay"; any other answer is reported."""
    h = Harness(tmp_path)
    await h.tick()
    overlay = overlay_path_for(h.path)
    real_stat = Path.stat

    def forbidden_stat(self: Path, **kwargs: Any) -> Any:
        if self == overlay:
            raise PermissionError(13, "Permission denied", str(overlay))
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", forbidden_stat)
    await h.tick()
    snapshot = h.snapshots[-1]
    assert snapshot.config_valid is False
    assert "workflow overlay unreadable" in (snapshot.config_error or "")
    assert h.orchestrator.workflow is h.workflow


async def test_missing_workflow_file_is_reported_not_fatal(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.path.unlink()
    await h.tick()
    assert h.snapshots[-1].config_valid is False
    assert "unreadable" in (h.snapshots[-1].config_error or "")


async def test_preflight_failure_skips_dispatch_but_reconciles(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_conflict_reworks=0)  # nothing polls review, so no fetch at all
    h.add_issue(1, "todo")
    await h.tick()
    h.add_issue(2, "todo")
    h.which_missing = {"claude"}
    h.github.human_set_state(1, StateLabel.TODO)
    h.github.calls.clear()
    await h.tick()
    assert len(h.sessions.runs) == 1
    assert h.calls("fetch_issues_by_states") == []
    assert h.entry(1).stop_cause == "moved"
    h.which_missing = set()
    await h.tick()
    assert len(h.sessions.runs) == 2


# --- the dispatch hold in the snapshot (#29) ----------------------------------------------


async def test_a_preflight_problem_is_carried_in_the_snapshot(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.which_missing = {"claude"}
    await h.tick()
    hold = h.snapshots[-1].dispatch_hold
    assert hold is not None
    assert (hold.kind, hold.since) == ("preflight", h.now())
    assert hold.reason == "claude.command 'claude' not found on PATH"
    # The worker is ticking and its config is fine: only the hold says it will not claim.
    assert (h.snapshots[-1].config_valid, h.snapshots[-1].config_error) == (True, None)
    h.which_missing = set()
    await h.tick()
    assert h.snapshots[-1].dispatch_hold is None


async def test_an_authentication_hold_is_carried_in_the_snapshot(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    assert h.snapshots[-1].dispatch_hold is None
    await h.exit(h.run_for(1), **AUTH_FAILURE)
    h.claude_auth_output = LOGGED_OUT
    await h.tick()
    hold = h.snapshots[-1].dispatch_hold
    assert hold is not None
    assert (hold.kind, hold.since) == ("auth", h.now())
    assert hold.reason == (
        "claude authentication unavailable: not logged in; "
        "run claude auth login or set ANTHROPIC_API_KEY"
    )
    h.claude_auth_output = LOGGED_IN
    await h.tick()
    assert h.snapshots[-1].dispatch_hold is None


async def test_a_hold_that_lasts_keeps_the_moment_it_started(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.which_missing = {"claude"}
    await h.tick()
    started = h.snapshots[-1].dispatch_hold
    assert started is not None
    h.clock.advance(120.0)
    await h.tick()
    assert h.snapshots[-1].dispatch_hold == started
    # A different reason is a different hold, so the clock restarts with it.
    h.which_missing = {"claude", "gh"}
    await h.tick()
    changed = h.snapshots[-1].dispatch_hold
    assert changed is not None and changed.since == h.now() > started.since
    assert changed.reason == "claude.command 'claude' not found on PATH; 'gh' not found on PATH"


async def test_the_snapshot_hold_survives_the_round_trip_through_json(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.which_missing = {"gh"}
    await h.tick()
    data = json.loads(json.dumps(h.snapshots[-1].to_dict()))
    assert data["dispatch_hold"] == {
        "kind": "preflight",
        "reason": "'gh' not found on PATH",
        "since": h.now().isoformat(),
    }


async def test_issues_are_still_polled_while_authentication_holds_dispatch(tmp_path: Path) -> None:
    """The board would otherwise go stale for as long as the hold lasts."""
    h = Harness(tmp_path, observe_issues=True)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **AUTH_FAILURE)
    h.claude_auth_output = LOGGED_OUT
    h.add_issue(2, "todo")
    h.polled.clear()
    h.github.calls.clear()
    await h.tick()
    assert [issue.number for issue in h.polled[-1]] == [1, 2]
    assert h.calls("fetch_issues_by_states") == [(OBSERVED_STATES,)]
    assert h.github.issue(2).state is StateLabel.TODO  # polled, but still not claimed


async def test_a_preflight_hold_polls_nothing_when_the_fetch_is_what_it_reports(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, observe_issues=True)
    h.add_issue(1, "todo")
    h.which_missing = {"gh"}
    await h.tick()
    assert h.polled == []
    assert h.calls("fetch_issues_by_states") == []


async def test_a_preflight_hold_still_polls_when_only_claude_is_missing(tmp_path: Path) -> None:
    """`gh` and the token are fine, so the board can be kept current while nothing is claimed."""
    h = Harness(tmp_path, observe_issues=True)
    h.add_issue(1, "todo")
    h.which_missing = {"claude"}
    await h.tick()
    assert [issue.number for issue in h.polled[-1]] == [1]
    assert h.calls("fetch_issues_by_states") == [(OBSERVED_STATES,)]
    assert h.github.issue(1).state is StateLabel.TODO  # polled, but still not claimed
    assert h.snapshots[-1].dispatch_hold is not None


async def test_giving_up_on_an_unreadable_probe_clears_the_hold_from_the_snapshot(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **AUTH_FAILURE)
    h.claude_auth_output = None  # an older or wedged claude: no usable answer, ever
    for _ in range(MAX_UNREADABLE_AUTH_PROBES - 1):
        await h.tick()
        assert h.snapshots[-1].dispatch_hold is not None
    await h.tick()
    assert h.snapshots[-1].dispatch_hold is None


async def test_an_unreadable_probe_that_garbles_itself_keeps_the_moment_it_started(
    tmp_path: Path,
) -> None:
    """The reason follows the probe, but `since` must still say how long the hold has lasted."""
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **AUTH_FAILURE)
    h.claude_auth_output = "garbage one"
    await h.tick()
    started = h.snapshots[-1].dispatch_hold
    assert started is not None and "garbage one" in started.reason
    h.clock.advance(60.0)
    h.claude_auth_output = "garbage two"
    await h.tick()
    hold = h.snapshots[-1].dispatch_hold
    assert hold is not None and "garbage two" in hold.reason
    assert hold.since == started.since


async def test_a_preflight_hold_gives_way_to_an_authentication_one(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **AUTH_FAILURE)
    h.claude_auth_output = LOGGED_OUT
    h.which_missing = {"gh"}
    await h.tick()
    hold = h.snapshots[-1].dispatch_hold
    assert hold is not None and hold.kind == "preflight"
    h.which_missing = set()
    h.clock.advance(30.0)
    await h.tick()
    changed = h.snapshots[-1].dispatch_hold
    assert changed is not None and changed.kind == "auth"
    assert changed.since == h.now() > hold.since


# --- snapshot -----------------------------------------------------------------------------


async def test_snapshot_rows_and_active_seconds(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo", title="First")
    h.add_issue(2, "todo")
    await h.tick()
    h.run_for(1).observer.on_turn_event(activity(kind="session_started", session_id="sess-1"))
    await h.exit(
        h.run_for(2),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom",
        final_state=StateLabel.IN_PROGRESS,
    )
    h.clock.advance(5)
    snapshot = h.orchestrator.snapshot()
    assert snapshot.at == h.now()
    assert snapshot.workflow_path == str(h.path)
    assert (snapshot.tick_count, snapshot.last_tick_at) == (1, START)
    row = snapshot.running[0]
    assert (row.issue_number, row.identifier, row.title, row.state) == (
        1,
        "repo-1",
        "First",
        "in_progress",
    )
    assert (row.session_id, row.attempt, row.started_at) == ("sess-1", 1, START)
    assert row.last_activity_at == START
    assert row.last_event == "session_started"
    retry = snapshot.retrying[0]
    assert (retry.issue_number, retry.kind, retry.attempt) == (2, "failure", 2)
    assert retry.due_at == START + timedelta(seconds=20)
    assert snapshot.totals.seconds_running == 12.0 + 5.0
    assert snapshot.counters.runs_started == 2
    assert snapshot.to_dict()["running"][0]["identifier"] == "repo-1"


# --- the issues observer (Phase 6) ----------------------------------------------------------


async def test_on_issues_receives_every_fetch(tmp_path: Path) -> None:
    h = Harness(tmp_path, observe_issues=True)
    h.add_issue(1, "todo")
    h.add_issue(2, "review")
    closed = h.add_issue(3, "review")
    h.github.open_pr(3, pr_number=7)
    h.github.merge_pr(7)
    h.github.close_issue(3)
    await h.tick()
    # the first tick: the sweep's closed issue, then the candidate fetch (review included)
    assert [[issue.number for issue in batch] for batch in h.polled] == [[3], [1, 2]]
    assert h.polled[0][0].github_state == "closed"
    assert closed.number == 3
    assert list(h.orchestrator.running) == ["1"]
    assert h.calls("fetch_issues_by_states")[-1] == (
        (StateLabel.IN_PROGRESS, StateLabel.REWORK, StateLabel.TODO, StateLabel.REVIEW),
    )
    await h.tick()
    # the second tick: reconcile's refresh of the running issue, then the candidate fetch
    assert [[issue.number for issue in batch] for batch in h.polled[2:]] == [[1], [1, 2]]
    assert h.polled[2][0].state is StateLabel.IN_PROGRESS


async def test_on_issues_receives_a_fired_retry_refresh(tmp_path: Path) -> None:
    h = Harness(tmp_path, observe_issues=True)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1))
    h.polled.clear()
    await h.fire(1.0)
    assert [[issue.number for issue in batch] for batch in h.polled] == [[1]]
    assert h.polled[0][0].state is StateLabel.REVIEW
    assert h.orchestrator.retries == {}


@pytest.mark.parametrize(
    ("observed", "conflicts", "expected"),
    [
        (False, False, CANDIDATE_STATES),
        (True, False, OBSERVED_STATES),
        (False, True, OBSERVED_STATES),
        (True, True, OBSERVED_STATES),
    ],
)
def test_fetch_states_adds_review_for_either_reason(
    observed: bool, conflicts: bool, expected: tuple[StateLabel, ...]
) -> None:
    assert fetch_states(observed=observed, conflicts=conflicts) == expected


async def test_without_an_observer_or_the_bounce_review_is_not_fetched(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_conflict_reworks=0)
    h.add_issue(1, "review")
    await h.tick()
    assert h.polled == []
    assert h.calls("fetch_issues_by_states")[-1] == (
        (StateLabel.IN_PROGRESS, StateLabel.REWORK, StateLabel.TODO),
    )
    assert h.orchestrator.running == {}


async def test_a_raising_issues_consumer_is_logged_and_the_tick_continues(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    configure_logging(fmt="json", level="INFO", stream=stream)
    h = Harness(tmp_path, observe_issues=True)

    def explode(issues: Any) -> None:
        raise RuntimeError("consumer bug")

    h.orchestrator._on_issues = explode
    h.add_issue(1, "todo")
    await h.tick()
    assert list(h.orchestrator.running) == ["1"]
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    failed = [line for line in lines if line["event"] == "issues_consumer_failed"]
    assert len(failed) == 1  # the candidate fetch; the sweep found nothing to report
    assert failed[0]["count"] == 1
    assert "consumer bug" in failed[0]["exception"]


async def test_shutdown_publishes_a_final_snapshot(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    assert len(h.snapshots) == 1
    assert len(h.snapshots[-1].running) == 1
    await h.orchestrator.shutdown()
    assert len(h.snapshots) == 2
    assert h.snapshots[-1].running == ()
    assert h.snapshots[-1].retrying == ()
    assert h.snapshots[-1].counters.runs_ended == 1


# --- the loop -----------------------------------------------------------------------------


async def wait_until(condition: Any, *, timeout: float = 10.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def test_run_ticks_refreshes_and_stops(tmp_path: Path) -> None:
    h = Harness(tmp_path, interval_ms=60_000)
    h.orchestrator._clock = __import__("time").monotonic
    task = asyncio.create_task(h.orchestrator.run())
    await wait_until(lambda: len(h.snapshots) == 1)
    h.orchestrator.request_refresh()
    h.orchestrator.request_refresh()
    await wait_until(lambda: len(h.snapshots) == 2, timeout=2.0)
    await asyncio.sleep(0.1)
    assert len(h.snapshots) == 2
    h.orchestrator.request_stop()
    await asyncio.wait_for(task, timeout=5)
    assert h.orchestrator.stopping is True


async def test_shutdown_cancels_workers_and_leaves_the_label(tmp_path: Path) -> None:
    h = Harness(tmp_path, interval_ms=60_000)
    h.orchestrator._clock = __import__("time").monotonic
    h.add_issue(1, "todo")
    task = asyncio.create_task(h.orchestrator.run())
    await wait_until(lambda: len(h.sessions.runs) == 1)
    h.orchestrator.request_stop()
    await asyncio.wait_for(task, timeout=5)
    assert h.run_for(1).cancel.is_set()
    assert h.orchestrator.running == {}
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is StateLabel.IN_PROGRESS
    assert h.orchestrator.snapshot().counters.runs_ended == 1


async def test_shutdown_cancels_stragglers_after_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path, interval_ms=60_000)
    h.orchestrator._clock = __import__("time").monotonic
    monkeypatch.setattr(orchestrator_module, "TERMINATE_GRACE_S", 0.0)
    monkeypatch.setattr(orchestrator_module, "SHUTDOWN_MARGIN_S", 0.2)
    h.write_workflow(hooks={"timeout_ms": "1"})
    h.orchestrator._workflow = load_workflow(h.path, environ=h.environ)

    async def stubborn(*args: Any, **kwargs: Any) -> RunResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    h.orchestrator._run_session = stubborn
    h.add_issue(1, "todo")
    task = asyncio.create_task(h.orchestrator.run())
    await wait_until(lambda: len(h.orchestrator.running) == 1)
    h.orchestrator.request_stop()
    await asyncio.wait_for(task, timeout=5)
    assert h.orchestrator.running == {}
    assert h.orchestrator.snapshot().counters.runs_ended == 1


async def test_success_during_shutdown_publishes_the_agent_transition(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.open_pr(1, pr_number=2)
    h.github.human_set_state(1, StateLabel.REVIEW)
    h.run_for(1).finish(final_issue=h.github.issue(1))
    await h.orchestrator.shutdown()
    assert h.orchestrator.running == {}
    assert h.orchestrator.retries == {}
    assert h.recorder.kinds == ["state_changed", "state_changed", "pr_opened"]
    agent_move = h.recorder.of(StateChanged)[1]
    assert (agent_move.actor, agent_move.to_label) == ("agent", "issuebot/review")
    assert agent_move.pr_url == "https://github.com/example/repo/pull/2"
    assert h.recorder.of(PrOpened)[0].pr_number == 2
    assert h.orchestrator.snapshot().counters.runs_ended == 1


async def test_run_propagates_startup_errors(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.which_missing = {"claude"}
    with pytest.raises(OrchestratorStartupError):
        await h.orchestrator.run()


# --- end to end ---------------------------------------------------------------------------


@posix
async def test_end_to_end_with_the_fakes(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        max_turns=1,
        interval_ms=1000,
        claude=str(FAKE_CLAUDE),
        hooks={"after_run": "touch after-run-ran"},
        real_sessions=True,
    )
    h.orchestrator._clock = __import__("time").monotonic
    h.add_issue(1, "todo", title="Add a function")
    task = asyncio.create_task(h.orchestrator.run())
    await wait_until(lambda: h.github.issue(1).state is StateLabel.REVIEW, timeout=20.0)
    h.orchestrator.request_stop()
    await asyncio.wait_for(task, timeout=10)
    assert h.recorder.kinds == [
        "state_changed",
        "run_started",
        "run_ended",
        "state_changed",
        "blocked",
    ]
    workspace = h.root / "repo-1"
    assert (workspace / ".git").is_dir()
    assert (workspace / "after-run-ran").exists()
    assert (workspace / ".issuebot" / "session.json").exists()
    runs = list((workspace / ".issuebot" / "runs").iterdir())
    assert len(runs) == 1
    assert (runs[0] / "turn-1.jsonl").exists()
    body = h.github.comments_for(1)[0].body
    assert body.startswith(WORKPAD_MARKER)
    assert "Turn budget exhausted: 1 turns in attempt 1" in body
    assert str(runs[0]) in body
    totals = h.orchestrator.snapshot().totals
    assert totals.input_tokens > 0 and totals.cost_usd > 0


# --- conflict bounce (spec 2026-09-13-conflict-rework-design.md) ----------------------------


async def test_a_conflicting_review_issue_is_bounced_then_dispatched_as_rework(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.add_conflicting_review(1, pr_number=7)
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REWORK
    assert h.calls("fetch_issues_by_states")[-1] == (OBSERVED_STATES,)
    assert h.calls("set_state") == [(1, StateLabel.REWORK)]
    body = h.github.comments_for(1)[0].body
    assert body.startswith(f"{WORKPAD_MARKER}\n\n### Issuebot merge conflict (")
    assert "(bounce 1 of 3)" in body
    changed = h.recorder.of(StateChanged)
    assert [(e.from_label, e.to_label, e.actor) for e in changed] == [
        ("issuebot/review", "issuebot/rework", "issuebot")
    ]
    assert h.orchestrator.running == {}
    await h.tick()
    assert list(h.orchestrator.running) == ["1"]
    assert h.run_for(1).kwargs["rework"] is True
    assert h.github.issue(1).state is StateLabel.IN_PROGRESS


async def test_the_bounce_waits_for_the_review_grace_to_end(tmp_path: Path) -> None:
    """A running entry would report the move as a human's and stop for the wrong reason."""
    h = Harness(tmp_path, interval_ms=30_000)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")
    h.github.human_set_state(1, StateLabel.REVIEW)
    h.github.open_pr(1, pr_number=7)
    h.github.set_pr_mergeable(7, "conflicting")
    h.clock.advance(30)
    await h.tick()  # grace starts; the entry is still running
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.calls("set_state") == [(1, StateLabel.IN_PROGRESS)]
    h.clock.advance(30)
    await h.tick()  # grace over: the entry is stopped, but it has not exited yet
    assert h.entry(1).cancel.is_set()
    assert h.github.issue(1).state is StateLabel.REVIEW
    await h.drain()
    assert h.orchestrator.running == {}
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REWORK
    assert h.recorder.of(StateChanged)[-1].actor == "issuebot"


@pytest.mark.parametrize(
    ("pr_state", "mergeable"),
    [
        ("open", "mergeable"),
        ("open", "unknown"),
        ("merged", "conflicting"),
        ("closed", "conflicting"),
    ],
)
async def test_only_an_open_conflicting_pr_is_bounced(
    tmp_path: Path, pr_state: str, mergeable: str
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "review")
    h.github.open_pr(1, pr_number=7)
    h.github.set_pr_mergeable(7, mergeable)  # type: ignore[arg-type]
    if pr_state == "merged":
        h.github.merge_pr(7)
        h.github.reopen_issue(1)  # merge_pr closes the issue; a closed one is swept, not bounced
    elif pr_state == "closed":
        h.github.close_pr(7)
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.calls("set_state") == []
    assert h.github.comments_for(1) == []


async def test_the_setting_at_zero_turns_the_bounce_off(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_conflict_reworks=0)
    h.add_conflicting_review(1, pr_number=7)
    await h.tick()
    assert h.calls("fetch_issues_by_states")[-1] == (CANDIDATE_STATES,)
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.calls("set_state") == []


async def test_the_setting_at_zero_with_an_observer_still_does_not_bounce(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_conflict_reworks=0, observe_issues=True)
    h.add_conflicting_review(1, pr_number=7)
    await h.tick()
    assert h.calls("fetch_issues_by_states")[-1] == (OBSERVED_STATES,)
    assert h.github.issue(1).state is StateLabel.REVIEW


async def test_a_held_worker_still_bounces(tmp_path: Path) -> None:
    """The hold stops claude, not gh; a conflict is about the board, not dispatch."""
    h = Harness(tmp_path)
    h.which_missing = {"claude"}
    h.add_conflicting_review(1, pr_number=7)
    await h.tick()
    assert h.snapshots[-1].dispatch_hold is not None
    assert h.github.issue(1).state is StateLabel.REWORK
    await h.tick()
    assert h.orchestrator.running == {}  # held: bounced, not dispatched


async def test_the_bounce_stops_at_the_limit(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_conflict_reworks=1)
    h.add_conflicting_review(1, pr_number=7)
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REWORK
    await h.tick()  # dispatched as rework
    assert list(h.orchestrator.running) == ["1"]
    # The session returns the issue to review; the fake PR still reads conflicting, as it
    # would after the next sibling merge.
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.exit(h.run_for(1), final_state=StateLabel.REVIEW, final_issue=h.github.issue(1))
    await h.fire(1.0)  # the continuation retry a review exit queues; it finds review and clears
    assert h.orchestrator.running == {}
    assert h.orchestrator.retries == {}
    h.github.calls.clear()
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.calls("set_state") == []
    body = h.github.comments_for(1)[0].body
    assert body.count("### Issuebot merge conflict (") == 1
    assert "### Issuebot merge conflict limit (" in body
    await h.tick()
    assert h.github.comments_for(1)[0].body == body


async def test_a_bounce_failure_is_logged_and_retried_next_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_conflicting_review(1, pr_number=7)
    original = h.github.set_state
    refusals = {"left": 1}

    async def refuse_once(*args: Any, **kwargs: Any) -> None:
        if refusals["left"]:
            refusals["left"] -= 1
            raise GitHubError("server_error", "injected")
        await original(*args, **kwargs)

    monkeypatch.setattr(h.github, "set_state", refuse_once)
    with capture_logs() as logs:
        await h.tick()
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.github.comments_for(1) == []
    assert any(entry["event"] == "conflict_rework_failed" for entry in logs)
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REWORK


async def test_the_bounce_skips_an_issue_with_a_queued_retry(tmp_path: Path) -> None:
    """A queued retry is an in-flight decision about the same issue, so the bounce leaves it
    alone until the retry has been fired; the next poll gets it."""
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()  # dispatched
    h.github.human_set_state(1, StateLabel.REVIEW)
    h.github.open_pr(1, pr_number=7)
    h.github.set_pr_mergeable(7, "conflicting")
    # A session that reaches review queues a continuation retry and releases the entry.
    await h.exit(h.run_for(1), final_state=StateLabel.REVIEW, final_issue=h.github.issue(1))
    assert h.orchestrator.running == {}
    assert list(h.orchestrator.retries) == ["1"]
    h.github.calls.clear()
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.calls("set_state") == []
    assert h.github.comments_for(1) == []
    await h.fire(1.0)  # the continuation retry finds review and clears
    assert h.orchestrator.retries == {}
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REWORK
    assert h.calls("set_state") == [(1, StateLabel.REWORK)]


# --- the GitHub hold (#88) ----------------------------------------------------------------

GITHUB_DOWN = json.dumps(
    {
        "status": {"indicator": "major", "description": "Partial System Outage"},
        "components": [{"name": "Pull Requests", "status": "major_outage", "group": False}],
    }
)
GITHUB_UP = json.dumps(
    {"status": {"indicator": "none", "description": "All Systems Operational"}, "components": []}
)


class FetchOutage:
    """The board goes unreadable the way an outage makes it: the poll fails, nothing else does.

    Narrower than ``fail_next``, which would also consume the terminal sweep's calls and the
    running refresh's, and it is only the poll that #88 counts.
    """

    def __init__(self, h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
        self.error = "http 502: Bad Gateway"
        self.down = True
        self._original = h.github.fetch_issues_by_states
        monkeypatch.setattr(h.github, "fetch_issues_by_states", self)

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.down:
            raise GitHubError("transport", self.error)
        return await self._original(*args, **kwargs)


def held(h: Harness) -> Any:
    return h.snapshots[-1].dispatch_hold


async def test_one_failed_poll_does_not_hold_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gh` retries a transport error of its own, so a single failure is a blip, not an outage."""
    h = Harness(tmp_path)
    outage = FetchOutage(h, monkeypatch)
    for _ in range(MAX_FETCH_FAILURES - 1):
        await h.tick()
        assert held(h) is None
    assert outage.down


async def test_consecutive_failed_polls_hold_dispatch_and_say_so_in_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap #88 names: the board stops moving and every surface reads as a healthy worker."""
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    FetchOutage(h, monkeypatch)
    for _ in range(MAX_FETCH_FAILURES):
        await h.tick()
    hold = held(h)
    assert hold is not None
    assert (hold.kind, hold.since) == ("github", h.now())
    assert hold.reason == "GitHub is not answering this worker: transport: http 502: Bad Gateway"
    # The worker itself is fine; only the hold says why the board is not moving.
    assert (h.snapshots[-1].config_valid, h.snapshots[-1].config_error) == (True, None)
    assert h.sessions.runs == []


async def test_the_first_successful_poll_releases_the_hold_and_dispatch_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    outage = FetchOutage(h, monkeypatch)
    for _ in range(MAX_FETCH_FAILURES):
        await h.tick()
    assert held(h) is not None
    outage.down = False
    with capture_logs() as lines:
        await h.tick()
    assert held(h) is None
    assert [line["event"] for line in lines if line["event"] == "dispatch_github_recovered"]
    assert len(h.sessions.runs) == 1


async def test_a_poll_that_succeeds_before_the_threshold_starts_the_count_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consecutive, not cumulative: a flaky link that keeps answering is not an outage."""
    h = Harness(tmp_path)
    outage = FetchOutage(h, monkeypatch)
    for _ in range(MAX_FETCH_FAILURES * 3):
        outage.down = not outage.down
        await h.tick()
        assert held(h) is None


async def test_the_hold_keeps_the_moment_it_started_as_the_error_rewords_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One outage answers differently from poll to poll; the operator wants its real age."""
    h = Harness(tmp_path)
    outage = FetchOutage(h, monkeypatch)
    for _ in range(MAX_FETCH_FAILURES):
        await h.tick()
    started = held(h)
    assert started is not None
    h.clock.advance(120.0)
    outage.error = "http 503: Service Unavailable"
    await h.tick()
    hold = held(h)
    assert hold is not None
    assert hold.since == started.since
    assert "503" in hold.reason


async def test_the_status_page_annotates_the_hold_and_is_read_once_per_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lagging indicator can name an outage this worker has already found, and nothing more."""
    h = Harness(tmp_path)
    h.github_status_output = GITHUB_DOWN
    FetchOutage(h, monkeypatch)
    for _ in range(MAX_FETCH_FAILURES + 3):
        await h.tick()
    hold = held(h)
    assert hold is not None
    assert hold.reason == (
        "GitHub is not answering this worker: transport: http 502: Bad Gateway"
        " \u2014 githubstatus.com at 12:00Z: Pull Requests, major outage"
    )
    # Once when the hold engaged, and never again while it lasts: a third party stays out of
    # the steady-state tick path.
    assert h.github_status_calls == 1


async def test_an_operational_status_page_is_still_worth_carrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "GitHub says it is fine" points the operator at their own network instead of GitHub's."""
    h = Harness(tmp_path)
    h.github_status_output = GITHUB_UP
    FetchOutage(h, monkeypatch)
    for _ in range(MAX_FETCH_FAILURES):
        await h.tick()
    hold = held(h)
    assert hold is not None
    assert hold.reason.endswith("githubstatus.com at 12:00Z: All Systems Operational")


async def test_a_status_page_that_cannot_answer_costs_the_annotation_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail open, both ways: neither silence nor an exception may change what issuebot does."""
    h = Harness(tmp_path)
    FetchOutage(h, monkeypatch)

    def exploding() -> str | None:
        h.github_status_calls += 1
        raise RuntimeError("the status page went up in smoke")

    monkeypatch.setattr(h.orchestrator, "_github_status", exploding)
    for _ in range(MAX_FETCH_FAILURES):
        await h.tick()
    hold = held(h)
    assert hold is not None
    assert hold.reason == "GitHub is not answering this worker: transport: http 502: Bad Gateway"
    assert h.github_status_calls == 1


async def test_a_second_outage_reads_the_status_page_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once per hold, not once per process: the release has to re-arm the probe."""
    h = Harness(tmp_path)
    h.github_status_output = GITHUB_DOWN
    outage = FetchOutage(h, monkeypatch)
    for _ in range(MAX_FETCH_FAILURES):
        await h.tick()
    first = held(h)
    assert first is not None and h.github_status_calls == 1
    outage.down = False
    await h.tick()
    assert held(h) is None
    h.clock.advance(600.0)
    outage.down = True
    for _ in range(MAX_FETCH_FAILURES):
        await h.tick()
    second = held(h)
    assert second is not None and h.github_status_calls == 2
    assert second.since > first.since


async def test_a_status_body_the_parser_cannot_survive_cannot_end_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`json.loads` raises RecursionError on a deeply nested body, and this runs mid-outage."""
    h = Harness(tmp_path)
    h.github_status_output = "[" * 100_000 + "]" * 100_000
    FetchOutage(h, monkeypatch)
    for _ in range(MAX_FETCH_FAILURES):
        await h.tick()
    hold = held(h)
    assert hold is not None
    assert hold.reason == "GitHub is not answering this worker: transport: http 502: Bad Gateway"


async def test_a_hold_reason_does_not_carry_an_unbounded_complaint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gh`'s stderr is not capped, and the reason is stored and drawn on every tick it lasts."""
    h = Harness(tmp_path)
    # The longest reason there is: a capped complaint and a capped annotation behind it.
    h.github_status_output = json.dumps(
        {
            "status": {"indicator": "major", "description": "Partial System Outage"},
            "components": [
                {"name": f"Component {index} with a long name", "status": "major_outage"}
                for index in range(20)
            ],
        }
    )
    outage = FetchOutage(h, monkeypatch)
    outage.error = "x" * 5_000
    for _ in range(MAX_FETCH_FAILURES):
        await h.tick()
    hold = held(h)
    assert hold is not None
    assert "\u2026 \u2014 githubstatus.com at 12:00Z: " in hold.reason
    assert len(hold.reason) <= MAX_HOLD_ERROR_CHARS + MAX_DETAIL_CHARS + 100


async def test_a_status_probe_that_will_not_return_cannot_stall_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetch's socket timeout does not reach the name lookup, and the tick is also where
    worker exits and the refresh are waited on."""
    h = Harness(tmp_path)
    FetchOutage(h, monkeypatch)
    released = threading.Event()

    def wedged() -> str | None:
        h.github_status_calls += 1
        released.wait(30)  # a resolver with nowhere to ask
        return None

    monkeypatch.setattr(h.orchestrator, "_github_status", wedged)
    monkeypatch.setattr(orchestrator_module, "GITHUB_STATUS_DEADLINE_S", 0.05)
    try:
        for _ in range(MAX_FETCH_FAILURES):
            await h.tick()
        hold = held(h)
        assert hold is not None
        # Held on this worker's own evidence, with no annotation and no delay.
        assert hold.reason == (
            "GitHub is not answering this worker: transport: http 502: Bad Gateway"
        )
        assert h.github_status_calls == 1
    finally:
        released.set()


async def test_the_status_page_is_never_read_while_the_board_answers(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    for _ in range(MAX_FETCH_FAILURES + 2):
        await h.tick()
    assert h.github_status_calls == 0


async def test_the_hold_reports_itself_every_tick_but_shouts_only_when_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idle worker says nothing else, and an operator reading ERROR wants news in it."""
    h = Harness(tmp_path)
    outage = FetchOutage(h, monkeypatch)
    with capture_logs() as lines:
        for _ in range(MAX_FETCH_FAILURES + 2):
            await h.tick()
        outage.error = "http 503: Service Unavailable"
        await h.tick()
    levels = [line["log_level"] for line in lines if line["event"] == "dispatch_github_held"]
    assert levels == ["error", "warning", "warning", "error"]


async def test_a_preflight_hold_outranks_a_github_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `gh` that will not run is why the poll failed, not a second, independent fault."""
    h = Harness(tmp_path, observe_issues=True)
    FetchOutage(h, monkeypatch)
    for _ in range(MAX_FETCH_FAILURES):
        await h.tick()
    assert held(h).kind == "github"
    h.which_missing = {"gh"}
    await h.tick()
    hold = held(h)
    assert hold is not None
    assert (hold.kind, hold.reason) == ("preflight", "'gh' not found on PATH")


async def test_an_authentication_hold_outranks_a_github_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path, observe_issues=True)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **AUTH_FAILURE)
    h.claude_auth_output = LOGGED_OUT
    FetchOutage(h, monkeypatch)
    for _ in range(MAX_FETCH_FAILURES):
        await h.tick()
    hold = held(h)
    assert hold is not None
    assert hold.kind == "auth"
    # The GitHub hold is still counted underneath, and surfaces the moment the credential does.
    h.claude_auth_output = LOGGED_IN
    await h.tick()
    assert held(h).kind == "github"


async def test_a_due_retry_waits_while_the_board_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claiming is a write to a board this worker has just failed to read three times."""
    h = Harness(tmp_path, max_attempts=3)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), outcome="failed", stop_reason="failure", error_category="transport")
    assert h.retry(1).kind == "failure"
    outage = FetchOutage(h, monkeypatch)
    for _ in range(MAX_FETCH_FAILURES):
        await h.tick()
    await h.fire(60.0)
    assert h.retry(1).kind == "github"
    assert len(h.sessions.runs) == 1
    outage.down = False
    await h.tick()
    await h.fire(60.0)
    assert len(h.sessions.runs) == 2


async def test_the_github_hold_survives_the_round_trip_through_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    FetchOutage(h, monkeypatch)
    for _ in range(MAX_FETCH_FAILURES):
        await h.tick()
    data = h.orchestrator.snapshot().to_dict()
    assert data["dispatch_hold"] == {
        "kind": "github",
        "reason": "GitHub is not answering this worker: transport: http 502: Bad Gateway",
        "since": h.now().isoformat(),
    }


# --- agent.run_as (#75) -----------------------------------------------------------------


def _with_run_as(
    h: Harness, probe: Callable[[Sequence[str], Mapping[str, str]], list[str]]
) -> Orchestrator:
    """The harness's orchestrator over a workflow whose session runs as `agent`."""
    workflow = load_workflow(h.path, environ={**h.environ, "ISSUEBOT_AGENT_USER": "agent"})
    assert workflow.config.agent.run_as == ("agent",)
    return Orchestrator(
        workflow,
        bus=h.bus,
        adapter_factory=lambda _settings: h.github,
        workspaces_factory=h.make_workspaces,
        runner_factory=h.make_runner,
        run_session=h.sessions,
        which=h.which,
        claude_auth=h.claude_auth,
        github_status=h.github_status,
        run_as_probe=probe,
        clock=h.clock,
        now=h.now,
        environ=h.environ,
    )


async def test_startup_fails_when_the_session_account_cannot_be_established(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    orchestrator = _with_run_as(
        h,
        lambda accounts, environ: [
            f"cannot run as {account!r}: sudo: a password is required" for account in accounts
        ],
    )
    with pytest.raises(OrchestratorStartupError) as exc:
        await orchestrator.startup()
    assert exc.value.problems == [
        "agent.run_as: cannot run as 'agent': sudo: a password is required"
    ]


async def test_startup_probes_the_session_account_and_passes_it_to_the_auth_probe(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    probed: list[str] = []
    orchestrator = _with_run_as(h, lambda accounts, environ: probed.extend(accounts) or [])
    await orchestrator.startup()
    assert probed == ["agent"]
    assert h.claude_auth_calls, "the login was probed after the account"


# --- a pool of session accounts (#121) ----------------------------------------------------

POOL_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "pool-credential"}


def _with_pool(
    h: Harness,
    accounts: str = "agent-1,agent-2",
    *,
    probe: Callable[[Sequence[str], Mapping[str, str]], list[str]] = lambda accounts, environ: [],
    environ: Mapping[str, str] | None = None,
) -> Orchestrator:
    """The harness driving an orchestrator whose sessions run as a pool of accounts."""
    env = {
        **h.environ,
        "ISSUEBOT_AGENT_USER": accounts,
        **(POOL_ENV if environ is None else environ),
    }
    workflow = load_workflow(h.path, environ=env)
    # One account is allowed here too: a reload *into* a pool is a case worth driving.
    assert workflow.config.agent.run_as == tuple(a.strip() for a in accounts.split(","))
    h.orchestrator = Orchestrator(
        workflow,
        bus=h.bus,
        adapter_factory=lambda _settings: h.github,
        workspaces_factory=h.make_workspaces,
        runner_factory=h.make_runner,
        run_session=h.sessions,
        which=h.which,
        claude_auth=h.claude_auth,
        github_status=h.github_status,
        run_as_probe=probe,
        clock=h.clock,
        now=h.now,
        environ=env,
        on_snapshot=h.snapshots.append,
    )
    return h.orchestrator


def _pool_workflow(h: Harness, accounts: str) -> None:
    """Rewrite the workflow with ``agent.run_as`` spelled out, so a reload can change it: the
    environment fallback is fixed for the life of the orchestrator."""
    h.write_workflow(
        text=WORKFLOW_TEMPLATE.format(
            interval_ms=30_000,
            root=h.root,
            max_concurrent=2,
            max_turns=3,
            max_attempts=3,
            max_retry_backoff_ms=300_000,
            max_conflict_reworks=3,
            max_issue_cost_usd=0.0,
            claude=h.claude,
            claude_extra=h.claude_extra(),
            stall_timeout_ms=300_000,
            hooks="",
            prompt="Task {{ issue.identifier }}",
        ).replace("agent:\n", f"agent:\n  run_as: [{accounts}]\n", 1)
    )


async def test_a_reload_into_an_unusable_pool_holds_dispatch_instead_of_claiming(
    tmp_path: Path,
) -> None:
    """`agent.run_as` is a setting like any other, so a reload can introduce exactly what
    startup refuses (#121). The probe runs again and the fault holds dispatch."""
    h = Harness(tmp_path)
    h.add_issue(1, StateLabel.TODO)
    orchestrator = _with_pool(
        h,
        probe=lambda accounts, environ: [
            f"{a}: cannot run as {a!r}" for a in accounts if a == "agent-3"
        ],
    )
    await orchestrator.startup()
    _pool_workflow(h, "agent-1, agent-3")
    await orchestrator.tick()
    assert h.github.issue(1).state is StateLabel.TODO, "nothing is claimed for a broken pool"
    hold = h.snapshots[-1].dispatch_hold
    assert hold is not None and hold.kind == "accounts"
    assert "agent-3: cannot run as 'agent-3'" in hold.reason


async def test_a_reload_that_repairs_the_pool_lifts_the_hold_and_dispatches(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, StateLabel.TODO)
    orchestrator = _with_pool(
        h,
        probe=lambda accounts, environ: [
            f"{a}: cannot run as {a!r}" for a in accounts if a == "agent-3"
        ],
    )
    await orchestrator.startup()
    _pool_workflow(h, "agent-1, agent-3")
    await orchestrator.tick()
    assert h.snapshots[-1].dispatch_hold is not None
    _pool_workflow(h, "agent-1, agent-2")
    await orchestrator.tick()
    assert h.snapshots[-1].dispatch_hold is None
    assert h.github.issue(1).state is StateLabel.IN_PROGRESS


async def test_a_terminal_removal_is_narrowed_to_the_account_that_owns_the_files(
    tmp_path: Path,
) -> None:
    """The removal is a delegated unlink, so it has to run as the account whose files they
    are -- and never as no account at all, which is what the host route would do (#121)."""
    h = Harness(tmp_path)
    orchestrator = _with_pool(h)
    await orchestrator.startup()
    record = h.add_issue(1, StateLabel.TODO)
    assert orchestrator._pool is not None
    key = workspace_key(record.identifier)
    orchestrator._pool.allocate(key)
    assert orchestrator._pool.bound(key) == "agent-1"
    assert orchestrator._workspaces_for(record)._account == "agent-1"


async def test_a_removal_whose_binding_is_unknown_still_delegates(tmp_path: Path) -> None:
    """A binding the record has forgotten, or one it cannot read, must not become the host
    route: the worker owns the workspace but not the directories inside the clone, so a
    removal with no delegation leaves a tree nothing can remove (#121)."""
    h = Harness(tmp_path)
    orchestrator = _with_pool(h)
    await orchestrator.startup()
    record = h.add_issue(1, StateLabel.TODO)
    assert orchestrator._pool is not None
    assert orchestrator._pool.bound(workspace_key(record.identifier)) is None
    # The pool's first member: not the binding, but an account the delegation works for, from
    # which the manager finds the files' real owner.
    assert orchestrator._workspaces_for(record)._account == "agent-1"


async def test_a_host_side_repair_lifts_the_accounts_hold_without_a_restart(
    tmp_path: Path,
) -> None:
    """Three of the four faults are properties of the host, not of `WORKFLOW.md`: an operator
    fixes them with `usermod` and never touches the file. A hold keyed on the file alone would
    last until the worker restarted, so it is re-probed while it lasts (#121)."""
    h = Harness(tmp_path)
    h.add_issue(1, StateLabel.TODO)
    broken = [False]
    orchestrator = _with_pool(
        h,
        probe=lambda accounts, environ: (
            ["agent-3: the worker is not in group agent-3"] if broken[0] else []
        ),
    )
    await orchestrator.startup()
    # The pool grows by an account whose group the worker was never added to.
    broken[0] = True
    _pool_workflow(h, "agent-1, agent-2, agent-3")
    await orchestrator.tick()
    hold = h.snapshots[-1].dispatch_hold
    assert hold is not None and hold.kind == "accounts"
    assert h.github.issue(1).state is StateLabel.TODO
    # A second tick with nothing changed keeps the hold and its start: the file never moved.
    await orchestrator.tick()
    assert h.snapshots[-1].dispatch_hold == hold
    # `usermod --append`, and nothing in the workflow file changes. (In a real deployment only
    # some faults clear without a restart; the probe is the seam, so the test drives it.)
    broken[0] = False
    await orchestrator.tick()
    assert h.snapshots[-1].dispatch_hold is None
    assert h.github.issue(1).state is StateLabel.IN_PROGRESS


async def test_the_accounts_hold_says_it_once_loudly_and_says_when_it_lifts(
    tmp_path: Path,
) -> None:
    """An idle worker says nothing else, so an unchanged fault must not fill the log with it:
    ERROR on the first and on a changed reason, WARNING after, and a line when it lifts, as the
    auth hold does (#121)."""
    stream = io.StringIO()
    configure_logging(fmt="json", level="INFO", stream=stream)
    h = Harness(tmp_path)
    broken = [""]
    orchestrator = _with_pool(h, probe=lambda accounts, environ: [broken[0]] if broken[0] else [])
    await orchestrator.startup()
    _pool_workflow(h, "agent-1, agent-2, agent-3")
    fault = "agent-3: the worker is not in group agent-3"
    for reason in (fault, fault, "agent-3: cannot run as 'agent-3'", ""):
        broken[0] = reason
        await orchestrator.tick()
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    held = [(x["level"], x["problems"]) for x in lines if x["event"] == "dispatch_run_as_unusable"]
    assert [level for level, _ in held] == ["error", "warning", "error"]
    [recovered] = [x for x in lines if x["event"] == "dispatch_run_as_recovered"]
    assert recovered["run_as"] == ["agent-1", "agent-2", "agent-3"]


async def test_a_reload_into_a_pool_without_a_credential_holds_dispatch(tmp_path: Path) -> None:
    """The one rule a pool adds beyond #75's: no shared login, so the credential has to be one
    `claude` needs no file for. Startup refuses it; a reload holds instead of failing every
    session's authentication."""
    h = Harness(tmp_path)
    h.add_issue(1, StateLabel.TODO)
    orchestrator = _with_pool(h, accounts="agent-1", environ={})
    await orchestrator.startup()
    _pool_workflow(h, "agent-1, agent-2")
    await orchestrator.tick()
    assert h.github.issue(1).state is StateLabel.TODO
    hold = h.snapshots[-1].dispatch_hold
    assert hold is not None and hold.kind == "accounts"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in hold.reason


async def test_startup_probes_every_account_in_the_pool(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    probed: list[str] = []
    orchestrator = _with_pool(h, probe=lambda accounts, environ: probed.extend(accounts) or [])
    await orchestrator.startup()
    assert probed == ["agent-1", "agent-2"]


async def test_startup_fails_when_one_account_in_the_pool_cannot_be_reached(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    orchestrator = _with_pool(
        h,
        probe=lambda accounts, environ: [
            f"{a}: cannot run as {a!r}" for a in accounts if a != "agent-1"
        ],
    )
    with pytest.raises(OrchestratorStartupError) as exc:
        await orchestrator.startup()
    assert exc.value.problems == ["agent.run_as: agent-2: cannot run as 'agent-2'"]


async def test_a_pool_refuses_to_start_without_a_credential_in_the_environment(
    tmp_path: Path,
) -> None:
    """The accounts share no login on purpose (#121), so every session would fail to
    authenticate: that is a startup failure, the way a definite logged-out is."""
    h = Harness(tmp_path)
    orchestrator = _with_pool(h, environ={})
    with pytest.raises(OrchestratorStartupError) as exc:
        await orchestrator.startup()
    assert exc.value.problems == [
        "agent.run_as: a pool of session accounts needs a credential in the environment, "
        "since each account has its own home and no login is shared between them: set one of "
        "CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY, or name a single account"
    ]


async def test_two_concurrent_sessions_run_as_two_different_accounts(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_concurrent=2)
    orchestrator = _with_pool(h)
    h.add_issue(1, "todo")
    h.clock.advance(1)
    h.add_issue(2, "todo")
    await h.tick()
    assert sorted(entry.account for entry in orchestrator.running.values()) == [
        "agent-1",
        "agent-2",
    ]
    # The narrowing reaches the runner, so nothing below the orchestrator sees a pool.
    assert sorted(settings.agent.run_as for settings in h.runner_settings) == [
        ("agent-1",),
        ("agent-2",),
    ]
    assert not any(settings.agent.run_as_pooled for settings in h.runner_settings)
    # And the workspace manager, which is the half that matters for the files: its account is
    # the uid its `Boundary` accepts as the session's and the home its `~/.claude` sweep clears
    # (#101), so a manager left holding the whole pool would sweep and trust the wrong account.
    # Each session's manager is narrowed to its own account. The orchestrator's own manager is
    # deliberately not (it is built from the whole config and reads only files whose declared
    # writer is the worker, `session.json` among them), which is why this is a subset rather
    # than an equality.
    assert {("agent-1",), ("agent-2",)} <= {
        settings.agent.run_as for settings in h.workspace_settings
    }


async def test_a_workspace_is_dispatched_to_the_same_account_for_as_long_as_it_exists(
    tmp_path: Path,
) -> None:
    """A rework lands back in the clone the first session made, so it needs the same uid."""
    h = Harness(tmp_path, max_concurrent=2)
    orchestrator = _with_pool(h)
    h.add_issue(1, "todo")
    h.clock.advance(1)
    h.add_issue(2, "todo")
    await h.tick()
    first = {entry.issue.number: entry.account for entry in orchestrator.running.values()}
    assert set(first.values()) == {"agent-1", "agent-2"}
    for number in (1, 2):
        h.workspace_dir(h.github.issue(number).identifier)
        h.github.human_set_state(number, StateLabel.REVIEW)
        await h.exit(h.run_for(number), final_issue=h.github.issue(number))
    await h.fire(2)  # the continuation retries release: the issues are with a reviewer
    assert not orchestrator.running and not orchestrator.retries
    for number in (1, 2):
        h.github.human_set_state(number, StateLabel.REWORK)
    await h.tick()
    assert {entry.issue.number: entry.account for entry in orchestrator.running.values()} == first


async def test_a_candidate_whose_account_is_busy_waits_rather_than_sharing_a_uid(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, max_concurrent=3)
    orchestrator = _with_pool(h)
    for number in (1, 2, 3):
        h.add_issue(number, "todo")
        h.clock.advance(1)
    await h.tick()
    # Three slots, two accounts: the third candidate is left, and never claimed.
    assert len(orchestrator.running) == 2
    assert h.github.issue(3).state is StateLabel.TODO
    assert sorted(entry.account for entry in orchestrator.running.values()) == [
        "agent-1",
        "agent-2",
    ]


async def test_a_removed_workspace_gives_its_account_back_on_the_sweep(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_concurrent=2)
    orchestrator = _with_pool(h)
    h.add_issue(1, "todo")
    await h.tick()
    identifier = h.github.issue(1).identifier
    assert orchestrator._pool is not None
    assert orchestrator._pool.bound(identifier) == "agent-1"
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1))
    await h.fire(2)
    # The workspace was never created and nothing holds the key, so the next terminal sweep --
    # the tenth tick after the first -- forgets the binding rather than leaving it to skew the
    # load for ever.
    for _ in range(10):
        await h.tick()
    assert orchestrator._pool.bound(identifier) is None


async def test_a_retry_whose_account_is_busy_is_requeued_rather_than_dropped(
    tmp_path: Path,
) -> None:
    """A free slot is not a free account (#121): requeueing keeps the attempt count, which
    letting `_dispatch` refuse the issue would spend."""
    h = Harness(tmp_path, max_concurrent=3, max_retry_backoff_ms=30_000)
    orchestrator = _with_pool(h)
    for number in (1, 2, 3):
        h.add_issue(number, "todo")
        h.clock.advance(1)
    await h.tick()
    assert h.entry(1).account == "agent-1"
    await h.exit(
        h.run_for(1),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom",
        final_state=StateLabel.IN_PROGRESS,
    )
    # Issue 3 takes the freed account, so when issue 1's retry comes due both are busy.
    await h.tick()
    assert h.entry(3).account == "agent-1"
    assert orchestrator._slots() == 1
    await h.fire(20)
    retry = h.retry(1)
    assert (retry.kind, retry.attempt) == ("slots", 2)
    assert retry.error == "the workspace's session account is busy"
    assert [run.issue.number for run in h.sessions.runs] == [1, 2, 3], (
        "issue 1 was dispatched again while its own account was still running"
    )


async def test_a_repaired_record_stops_blaming_the_accounts_under_another_hold(
    tmp_path: Path,
) -> None:
    """`_read_accounts` runs in `tick`, not in the candidate loop a preflight or auth hold
    skips (#121). Read there, the reason survives the repair for as long as the other hold
    lasts, and a retry firing in the meantime is requeued as `accounts` quoting a fault that
    is already fixed -- so the snapshot's `retrying` row blames the record while the worker is
    really waiting for `claude`.
    """
    h = Harness(tmp_path, max_concurrent=2)
    orchestrator = _with_pool(h)
    h.root.mkdir(parents=True, exist_ok=True)
    (h.root / ".issuebot").mkdir()
    (h.root / ".issuebot" / "accounts.json").write_text("{not json")
    await h.tick()
    hold = orchestrator.snapshot().dispatch_hold
    assert hold is not None and hold.kind == "accounts"
    # The record is repaired, and preflight fails in the same tick, so the candidate loop -- the
    # old home of the re-read -- never runs again to notice.
    (h.root / ".issuebot" / "accounts.json").unlink()
    h.which = lambda name: None if name == "claude" else f"/usr/bin/{name}"
    orchestrator._which = h.which
    h.add_issue(1, "todo")
    orchestrator._retries["1"] = replace(_retry_entry(h, 1), due_mono=h.clock.value - 1)
    await h.tick()
    hold = orchestrator.snapshot().dispatch_hold
    assert hold is not None and hold.kind == "preflight", "preflight still outranks accounts"
    await orchestrator.fire_due_retries()
    requeued = orchestrator._retries.get("1")
    assert requeued is None or requeued.kind != "accounts", requeued


async def test_an_unreadable_account_record_holds_dispatch_rather_than_idling_quietly(
    tmp_path: Path,
) -> None:
    """Without the hold, `issuebot status`, the dashboard and `/healthz` all read as a healthy
    worker while the board stops moving -- which is what `DispatchHold` (#29) exists for."""
    h = Harness(tmp_path, max_concurrent=2)
    orchestrator = _with_pool(h)
    h.root.mkdir(parents=True, exist_ok=True)
    (h.root / ".issuebot").mkdir()
    (h.root / ".issuebot" / "accounts.json").write_text("{not json")
    h.add_issue(1, "todo")
    await h.tick()
    assert not orchestrator.running
    assert h.github.issue(1).state is StateLabel.TODO
    hold = orchestrator.snapshot().dispatch_hold
    assert hold is not None and hold.kind == "accounts"
    assert "unusable" in hold.reason
    # And it holds on a tick whose only dispatchable work is a retry the candidate loop skips,
    # which is where a hold derived from that loop alone would quietly vanish.
    h.add_issue(2, "todo")
    orchestrator._retries["2"] = _retry_entry(h, 2)
    await h.tick()
    hold = orchestrator.snapshot().dispatch_hold
    assert hold is not None and hold.kind == "accounts"
    # And it lifts of its own accord once the record reads again.
    orchestrator._retries.clear()
    (h.root / ".issuebot" / "accounts.json").unlink()
    await h.tick()
    assert orchestrator.snapshot().dispatch_hold is None
    assert sorted(orchestrator.running) == ["1", "2"]


def _retry_entry(h: Harness, number: int) -> RetryEntry:
    issue = h.github.issue(number)
    return RetryEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        issue_number=number,
        issue_url=issue.url,
        title=issue.title,
        attempt=2,
        kind="failure",
        due_mono=h.clock.value + 3600,
        due_at=h.now(),
        error="boom",
    )


async def test_the_bounce_cap_holds_when_the_session_strips_the_workpad(tmp_path: Path) -> None:
    """The count is the label history, not the workpad (#104): a session that rewrites the
    body without issuebot's blocks still gets `max_conflict_reworks` bounces and no more."""
    h = Harness(tmp_path, max_conflict_reworks=2)
    h.add_conflicting_review(1, pr_number=7)
    for expected in (StateLabel.REWORK, StateLabel.REWORK, StateLabel.REVIEW):
        await h.tick()
        assert h.github.issue(1).state is expected
        if expected is StateLabel.REVIEW:
            break
        await h.tick()  # dispatched as rework
        assert list(h.orchestrator.running) == ["1"]
        # The session rewrites the workpad whole, dropping every `### Issuebot` block, and
        # returns the issue to review; the PR still conflicts after the next sibling merge.
        pad = await h.github.find_workpad_comment(1)
        assert pad is not None
        await h.github.update_comment(pad.id, f"{WORKPAD_MARKER}\n\n### Plan\n\n- [x] done\n")
        h.github.human_set_state(1, StateLabel.REVIEW)
        await h.exit(h.run_for(1), final_state=StateLabel.REVIEW, final_issue=h.github.issue(1))
        await h.fire(1.0)
        assert h.orchestrator.running == {} and h.orchestrator.retries == {}
    body = h.github.comments_for(1)[0].body
    assert body.count("### Issuebot merge conflict (") == 0
    assert body.count("### Issuebot merge conflict limit (") == 1
    assert h.calls("set_state").count((1, StateLabel.REWORK)) == 2


async def test_a_stripped_limit_note_is_rewritten_once_per_process_not_per_tick(
    tmp_path: Path,
) -> None:
    """The note's presence is the session's to erase; the orchestrator remembers it wrote it."""
    h = Harness(tmp_path, max_conflict_reworks=1)
    h.add_conflicting_review(1, pr_number=7)
    await h.tick()
    await h.tick()  # dispatched as rework
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.exit(h.run_for(1), final_state=StateLabel.REVIEW, final_issue=h.github.issue(1))
    await h.fire(1.0)
    await h.tick()  # at the limit: the note lands
    pad = await h.github.find_workpad_comment(1)
    assert pad is not None and "### Issuebot merge conflict limit (" in pad.body
    stripped = f"{WORKPAD_MARKER}\n\n### Plan\n"
    await h.github.update_comment(pad.id, stripped)
    h.github.calls.clear()
    for _ in range(3):
        await h.tick()
    assert h.github.comments_for(1)[0].body == stripped
    assert h.calls("count_own_label_additions") == []
    assert h.calls("update_comment") == []
    # A different limit is a different decision: the issue is looked at again.
    h.orchestrator._conflict_limit_noted["1"] = 5  # the memory under test
    await h.tick()
    assert h.calls("count_own_label_additions") == [(1, "issuebot/rework")]
    assert "### Issuebot merge conflict limit (" in h.github.comments_for(1)[0].body


# --- the admission gate (#112) ------------------------------------------------------------


async def fail_once(h: Harness, number: int, error: str = "boom") -> None:
    """One scripted session for ``number`` that ends as a process failure."""
    await h.exit(h.run_for(number), outcome="failed", error_category="process_exit", error=error)


def attempts_dispatched(h: Harness, number: int) -> list[int]:
    return [run.kwargs["attempt"] for run in h.sessions.runs if run.issue.number == number]


async def test_a_retry_does_not_claim_while_preflight_holds_dispatch(tmp_path: Path) -> None:
    """The half of #112 the other two holds already had: `_fire` could not see this one."""
    h = Harness(tmp_path)
    h.add_issue(1)
    await h.tick()
    await fail_once(h, 1)
    assert h.retry(1).kind == "failure"

    h.which_missing = {"claude"}
    await h.tick()
    hold = h.orchestrator.snapshot().dispatch_hold
    assert hold is not None and hold.kind == "preflight"

    await h.fire(20)
    assert h.orchestrator.running == {}
    retry = h.retry(1)
    # It waits under the hold's own name, with the hold's own words -- the same string the
    # snapshot carries, because one function composes both.
    assert retry.kind == "preflight"
    assert retry.error == hold.reason
    assert retry.attempt == 2


async def test_the_retry_resumes_when_preflight_clears(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1)
    await h.tick()
    await fail_once(h, 1)
    h.which_missing = {"claude"}
    await h.tick()
    await h.fire(20)
    assert h.orchestrator.running == {}

    h.which_missing = set()
    await h.tick()
    await h.fire(30)
    assert list(h.orchestrator.running) == ["1"]
    assert attempts_dispatched(h, 1) == [1, 2]


async def test_a_label_move_between_failures_does_not_reset_the_attempt_number(
    tmp_path: Path,
) -> None:
    """The budget is the issue's, not the label's: the chain reaches the escape."""
    h = Harness(tmp_path, max_attempts=3)
    h.add_issue(1)
    await h.tick()
    for _ in range(3):
        await fail_once(h, 1)
        if "1" not in h.orchestrator.retries:
            break
        # A collaborator, the session itself, or the `issue_moved` success path.
        h.github.human_set_state(1, StateLabel.REWORK)
        await h.fire(600)
    assert attempts_dispatched(h, 1) == [1, 2, 3]
    assert h.orchestrator.snapshot().counters.blocked == 1
    blocked = next(e for e in h.recorder.events if isinstance(e, Blocked))
    assert blocked.reason.startswith("3 consecutive worker sessions failed")


async def test_the_tick_takes_the_attempt_number_from_the_ledger_too(tmp_path: Path) -> None:
    """The sweep over the candidates is the other door, and it reads the same ledger."""
    h = Harness(tmp_path, max_attempts=3)
    h.add_issue(1)
    await h.tick()
    await fail_once(h, 1)
    # The retry is dropped and the issue relabelled, so only the tick's sweep can claim it.
    h.orchestrator._retries.clear()
    h.github.human_set_state(1, StateLabel.REWORK)
    await h.tick()
    assert attempts_dispatched(h, 1) == [1, 2]


async def test_a_run_that_succeeds_clears_the_chain(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_attempts=3)
    h.add_issue(1)
    await h.tick()
    await fail_once(h, 1)
    await h.fire(20)
    assert attempts_dispatched(h, 1) == [1, 2]
    h.github.human_set_state(1, StateLabel.REWORK)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1))
    assert h.retry(1).attempt == 1
    await h.fire(1)  # the continuation releases: rework is claimable, so it dispatches
    assert attempts_dispatched(h, 1) == [1, 2, 1]


async def test_the_escape_ends_the_chain_so_the_documented_recovery_works(
    tmp_path: Path,
) -> None:
    """README: fix the cause, then relabel. That is a label move, and it has to work."""
    h = Harness(tmp_path, max_attempts=2)
    h.add_issue(1)
    await h.tick()
    await fail_once(h, 1)
    await h.fire(20)
    await fail_once(h, 1)
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.orchestrator.snapshot().counters.blocked == 1

    h.github.human_set_state(1, StateLabel.REWORK)
    await h.tick()
    assert attempts_dispatched(h, 1) == [1, 2, 1]


async def test_a_spent_chain_refuses_the_claim_when_the_escape_never_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate is the backstop: an escape that could not be written leaves the chain spent."""
    h = Harness(tmp_path, max_attempts=1)
    h.add_issue(1)
    await h.tick()
    h.fail_on("find_workpad_comment", monkeypatch)
    await fail_once(h, 1)
    assert h.github.issue(1).state is StateLabel.IN_PROGRESS
    assert h.retry(1).kind == "escape"

    # Drop the escape retry, and let GitHub answer again: the issue is now an ordinary
    # in_progress candidate whose chain is spent and whose escape was never written.
    h.orchestrator._retries.clear()
    monkeypatch.undo()
    with capture_logs() as logs:
        await h.tick()
        await h.tick()
    assert attempts_dispatched(h, 1) == [1]
    refusals = [line for line in logs if line["event"] == "dispatch_refused"]
    assert len(refusals) == 1  # said once, not once a tick
    assert refusals[0]["refusal"] == "attempts"
    assert "agent.max_attempts is 1" in refusals[0]["reason"]
    # And the refusal is not silent: the issue is handed to a human where they can see it.
    assert h.github.issue(1).state is StateLabel.REVIEW
    body = h.github.comments_for(1)[0].body
    assert "### Issuebot budget limit (" in body
    # The chain is what ran out, and handing the issue over ends a chain, so relabelling is
    # genuinely the way back -- which is not what the spend ceiling's note says.
    assert "the next label move starts the run budget again" in body


async def test_the_cumulative_spend_ceiling_refuses_a_claim(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_issue_cost_usd=0.75)
    h.add_issue(1)
    await h.tick()
    # The scripted result reports 0.5 USD; two runs put the issue over the ceiling.
    h.github.human_set_state(1, StateLabel.REWORK)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1))
    await h.fire(1)
    assert attempts_dispatched(h, 1) == [1, 1]
    h.github.human_set_state(1, StateLabel.REWORK)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1))
    with capture_logs() as logs:
        await h.fire(1)  # the retry timer's door
        await h.tick()  # and the tick's, on the same issue
    assert attempts_dispatched(h, 1) == [1, 1]
    refusals = [line for line in logs if line["event"] == "dispatch_refused"]
    assert len(refusals) == 1  # both doors, one refusal, said once
    assert refusals[0]["refusal"] == "spend"
    assert "$1.00 over 2 runs" in refusals[0]["reason"]
    assert "agent.max_issue_cost_usd is $0.75" in refusals[0]["reason"]
    assert h.github.issue(1).state is StateLabel.REVIEW
    body = h.github.comments_for(1)[0].body
    assert "### Issuebot budget limit (" in body
    assert "agent.max_issue_cost_usd is $0.75" in body
    assert "Relabelling on its own only brings the issue back here" in body
    assert h.orchestrator.snapshot().counters.blocked == 1


async def test_the_spend_ceiling_keeps_saying_no_without_repeating_itself(
    tmp_path: Path,
) -> None:
    """A relabelled issue over the ceiling goes back to review, and the block is written once."""
    h = Harness(tmp_path, max_issue_cost_usd=0.4)
    h.add_issue(1)
    await h.tick()
    h.github.human_set_state(1, StateLabel.REWORK)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1))
    await h.fire(1)
    assert h.github.issue(1).state is StateLabel.REVIEW

    h.github.human_set_state(1, StateLabel.REWORK)
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert attempts_dispatched(h, 1) == [1]
    assert h.github.comments_for(1)[0].body.count("### Issuebot budget limit (") == 1


async def test_an_escape_github_refused_is_still_announced_by_the_tick_that_retries_it(
    tmp_path: Path,
) -> None:
    """The block landing and the `Blocked` event going out have a failure point between them.

    So the worker cannot read "already announced" off the workpad: an escape whose `set_state`
    failed has left the block there and told nobody. The ledger is marked only once the escape
    landed, which is what makes the retry announce rather than inherit the first one's silence.
    """
    h = Harness(tmp_path, max_issue_cost_usd=0.4)
    h.add_issue(1)
    await h.tick()
    h.github.human_set_state(1, StateLabel.REWORK)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1))

    ok = h.github.set_state
    calls = 0

    async def fails_once(number: int, state: StateLabel, **kwargs: object) -> None:
        """`fail_next` is positional, and the escape reads the board before it writes to it."""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GitHubError("transport", "the escape could not move the label")
        await ok(number, state, **kwargs)

    h.github.set_state = fails_once  # type: ignore[method-assign]
    await h.fire(1)
    assert h.github.issue(1).state is StateLabel.REWORK  # the label never moved
    assert h.orchestrator.snapshot().counters.blocked == 0
    assert h.recorder.of(Blocked) == []
    assert h.github.comments_for(1)[0].body.count("### Issuebot budget limit (") == 1

    h.github.set_state = ok  # type: ignore[method-assign]
    await h.tick()  # still a candidate, so the loop comes back to it
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.orchestrator.snapshot().counters.blocked == 1
    assert len(h.recorder.of(Blocked)) == 1
    assert h.github.comments_for(1)[0].body.count("### Issuebot budget limit (") == 1


async def test_an_over_budget_issue_with_a_conflicting_pr_settles(tmp_path: Path) -> None:
    """The conflict bounce and the gate disagree about one issue; the bounce limit ends it.

    The bounce is a decision about a pull request and the gate is a decision about a claim, so
    neither consults the other -- that is the whole point of the gate owning one question. The
    two do meet on an over-budget issue in `review` whose pull request conflicts: the bounce
    moves it to `rework`, the gate refuses it and hands it back. `agent.max_conflict_reworks`
    is what stops that, and this pins it -- along with the escalation staying *one* escalation
    while it lasts: the block is written once, and so are the `Blocked` event behind the Slack
    line and the count behind the dashboard's blocked tile.
    """
    h = Harness(tmp_path, max_issue_cost_usd=0.4, max_conflict_reworks=2)
    h.add_issue(1)
    await h.tick()
    h.github.open_pr(1, pr_number=2)
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1))
    h.github.set_pr_mergeable(2, "conflicting")
    for _ in range(6):
        await h.fire(60)
        await h.tick()
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert attempts_dispatched(h, 1) == [1]  # never claimed again
    body = h.github.comments_for(1)[0].body
    assert body.count("### Issuebot merge conflict (") == 2  # the bounce limit held
    assert body.count("### Issuebot budget limit (") == 1
    # The return trips are returns, not fresh escalations: one block, one event, one count.
    assert len(h.recorder.of(Blocked)) == 1
    assert h.orchestrator.snapshot().counters.blocked == 1


async def test_a_seeded_chain_at_the_ceiling_still_gets_a_run_that_can_escalate(
    tmp_path: Path,
) -> None:
    """History this process did not take is capped one short, so no issue is stranded."""
    h = Harness(
        tmp_path,
        max_attempts=3,
        initial_ledger={"repo-1": IssueLedger(failures=9, runs=9, cost_usd=4.0)},
    )
    h.add_issue(1)
    await h.tick()
    assert attempts_dispatched(h, 1) == [3]
    await fail_once(h, 1)
    # That run failed, so the chain is spent -- through the escape, where a human sees it.
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert "### Issuebot blocked (" in h.github.comments_for(1)[0].body


async def test_a_session_record_above_a_lowered_ceiling_is_capped_too(tmp_path: Path) -> None:
    """A record written before `agent.max_attempts` was lowered must not strand the orphan."""
    h = Harness(tmp_path, max_attempts=2)
    issue = h.add_issue(1, "in_progress")
    h.write_session(issue.identifier, issue_number=1, attempt=9, last_outcome=None)
    await h.tick()
    # Nine would refuse it for ever; capped at `max_attempts - 1`, it resumes on its last one.
    assert attempts_dispatched(h, 1) == [2]
    assert h.run_for(1).kwargs["resume_session_id"] == "sess-1"
    await fail_once(h, 1)
    assert h.github.issue(1).state is StateLabel.REVIEW


async def test_a_seeded_ledger_carries_the_budget_across_a_restart(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        max_attempts=3,
        initial_ledger={"repo-1": IssueLedger(failures=2, runs=2, cost_usd=1.0)},
    )
    h.add_issue(1)
    await h.tick()
    assert attempts_dispatched(h, 1) == [3]
    await fail_once(h, 1)
    # The third failure is the last one the issue had: the escape fires now, not in three more.
    assert h.github.issue(1).state is StateLabel.REVIEW


async def test_a_retry_releases_an_issue_that_is_already_running(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1)
    await h.tick()
    entry = h.entry(1)
    h.orchestrator._retries["1"] = RetryEntry(
        issue_id="1",
        identifier=entry.identifier,
        issue_number=1,
        issue_url=entry.issue.url,
        title=entry.issue.title,
        attempt=1,
        kind="failure",
        due_mono=h.clock.value,
        due_at=h.now(),
        error=None,
    )
    await h.fire(1)
    assert h.orchestrator.retries == {}
    assert len(h.sessions.runs) == 1  # no second worker for the same issue


async def test_a_reopened_issue_starts_from_a_clean_budget(tmp_path: Path) -> None:
    """A terminal finish forgets the issue, so closing and reopening really does start over."""
    h = Harness(tmp_path, max_attempts=2)
    h.add_issue(1)
    await h.tick()
    await fail_once(h, 1)
    h.github.close_issue(1)
    await h.fire(20)  # the retry finds it closed and finishes it

    h.github.reopen_issue(1)
    h.github.human_set_state(1, StateLabel.TODO)
    await h.tick()
    assert attempts_dispatched(h, 1) == [1, 1]


async def test_the_dispatch_line_carries_what_the_issue_has_cost(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1)
    await h.tick()
    h.github.human_set_state(1, StateLabel.REWORK)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1))
    with capture_logs() as logs:
        await h.fire(1)
    line = next(entry for entry in logs if entry["event"] == "dispatched")
    assert (line["issue_runs"], line["issue_cost_usd"]) == (2, 0.5)


async def test_an_accounts_hold_refuses_a_due_retry_through_the_gate(
    tmp_path: Path,
) -> None:
    """The admission gate (#112) and the account hold (#121) meet here: the gate asks
    `_current_hold()`, so the record that will not name a session's account refuses a claim at
    the retry door too, not just in the tick's candidate loop.

    Before the gate existed the retry path made this decision for itself, just before it
    dispatched. Composing it into the one hold is what keeps the snapshot the operator reads
    and the answer a caller gets from being two different claims -- and the retry has to come
    back as kind `accounts`, since requeuing it as `slots` would blame a full worker for a
    record an operator has to go and fix.
    """
    h = Harness(tmp_path, max_concurrent=2)
    orchestrator = _with_pool(h)
    h.root.mkdir(parents=True, exist_ok=True)
    (h.root / ".issuebot").mkdir()
    (h.root / ".issuebot" / "accounts.json").write_text("{not json")
    h.add_issue(1, "todo")
    await h.tick()
    assert orchestrator.snapshot().dispatch_hold is not None
    orchestrator._retries["1"] = replace(_retry_entry(h, 1), due_mono=h.clock.value - 1)
    before = len(h.calls("fetch_issues_by_ids"))
    await orchestrator.fire_due_retries()
    requeued = orchestrator._retries.get("1")
    assert requeued is not None, "the entry waits rather than being dropped"
    assert requeued.kind == "accounts"
    assert "unusable" in (requeued.error or "")
    assert not orchestrator.running, "and nothing was claimed"
    # The refusal happened *before* the refresh, which is the observable the gate adds and the
    # only one that distinguishes it from `_fire`'s own `_bind_account` fallback below: that
    # fallback reaches the same requeue with the same wording, one GitHub request later. A held
    # worker must not spend a request per due retry per poll finding out what it may not claim.
    assert len(h.calls("fetch_issues_by_ids")) == before, "the gate refused before the refresh"
