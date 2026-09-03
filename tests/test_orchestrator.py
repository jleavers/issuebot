"""Tests for the orchestrator against FakeGitHub, a scripted run_session and a fake clock."""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from issuebot.agent import ClaudeRunner, RunResult, SessionRecord, TurnEvent, WorkspaceManager
from issuebot.agent.session import run_session
from issuebot.config import Settings, load_workflow
from issuebot.events import (
    Blocked,
    Event,
    EventBus,
    IssueCancelled,
    IssueCompleted,
    PrOpened,
    StateChanged,
)
from issuebot.github import WORKPAD_MARKER, FakeGitHub, GhResult, Issue, StateLabel
from issuebot.orchestrator import orchestrator as orchestrator_module
from issuebot.orchestrator.orchestrator import (
    Orchestrator,
    OrchestratorStartupError,
    RunObserver,
    preflight,
)
from issuebot.orchestrator.state import RunningEntry

START = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
START_MONO = 1000.0
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
claude:
  command: {claude}
  turn_timeout_ms: 30000
  stall_timeout_ms: {stall_timeout_ms}
hooks:
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
        hooks: dict[str, str] | None = None,
        prompt: str = "Task {{ issue.identifier }}",
        claude: str = "claude",
        real_sessions: bool = False,
    ) -> None:
        self.tmp_path = tmp_path
        self.path = tmp_path / "WORKFLOW.md"
        self.root = tmp_path / "workspaces"
        self.claude = claude
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
        self.which_missing: set[str] = set()
        self.orchestrator = Orchestrator(
            self.workflow,
            bus=self.bus,
            adapter_factory=lambda _settings: self.github,
            workspaces_factory=self.make_workspaces,
            runner_factory=lambda settings: ClaudeRunner(settings, environ=self.environ),
            run_session=run_session if real_sessions else self.sessions,
            which=self.which,
            clock=self.clock,
            now=self.now,
            environ=self.environ,
            on_snapshot=self.snapshots.append,
        )

    # --- construction helpers ---------------------------------------------------------

    def now(self) -> datetime:
        return START + timedelta(seconds=self.clock.value - START_MONO)

    def which(self, name: str) -> str | None:
        return None if name in self.which_missing else f"/usr/bin/{name}"

    def make_workspaces(self, settings: Settings) -> WorkspaceManager:
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
            claude=self.claude,
            stall_timeout_ms=stall_timeout_ms,
            hooks=hook_lines,
            prompt=prompt,
        )
        self.path.write_text(content, encoding="utf-8")
        # Force a distinct mtime so a rewrite within the same tick is noticed.
        previous = getattr(self, "_mtime", 1_700_000_000)
        self._mtime = previous + 1
        os.utime(self.path, ns=(self._mtime * 1_000_000_000, self._mtime * 1_000_000_000))

    @property
    def labels(self) -> Any:
        return self.workflow.config.github.labels

    def add_issue(self, number: int, state: str = "todo", *, title: str | None = None) -> Issue:
        label = getattr(self.labels, state)
        return self.github.add_issue(title or f"Issue {number}", labels=(label,), number=number)

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


async def test_startup_succeeds_and_logs(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.orchestrator.startup()
    assert [name for name, _ in h.github.calls] == ["auth_status", "missing_labels"]


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


async def test_reconcile_gives_review_one_tick_of_grace(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.tick()
    entry = h.entry(1)
    assert entry.review_seen_tick == 1
    assert not entry.cancel.is_set()
    assert [event.actor for event in h.recorder.of(StateChanged)] == ["issuebot", "agent"]
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


async def test_missing_workflow_file_is_reported_not_fatal(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.path.unlink()
    await h.tick()
    assert h.snapshots[-1].config_valid is False
    assert "unreadable" in (h.snapshots[-1].config_error or "")


async def test_preflight_failure_skips_dispatch_but_reconciles(tmp_path: Path) -> None:
    h = Harness(tmp_path)
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
