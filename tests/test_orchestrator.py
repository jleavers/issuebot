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
    Event,
    EventBus,
    IssueCompleted,
)
from issuebot.github import FakeGitHub, GhResult, Issue, StateLabel
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
