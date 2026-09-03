"""Tests for run_session against FakeGitHub, a scripted runner and a real workspace manager."""

import asyncio
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog

from issuebot.agent.runner import TurnObserver, TurnResult
from issuebot.agent.session import RunResult, new_run_id, run_session
from issuebot.agent.workspace import WorkspaceManager
from issuebot.config import Settings, Workflow
from issuebot.events import Event, EventBus, RunEnded, RunStarted
from issuebot.github import FakeGitHub, GhResult, GitHubError, StateLabel

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="workspaces need bash and git")

TEMPLATE = "Task {{ issue.identifier }} turn {{ turn_number }} attempt {{ attempt }}"


class StubGh:
    def __init__(self) -> None:
        self.fail: GhResult | None = None

    async def run(self, args: list[str], *, stdin: str | None = None) -> GhResult:
        if self.fail is not None:
            return self.fail
        if list(args)[:2] == ["repo", "clone"]:
            subprocess.run(["git", "init", "-q", list(args)[3]], check=True)
        return GhResult(returncode=0, stdout="", stderr="")


class ScriptedRunner:
    """Returns one scripted TurnResult per call ("ok" or an error category) and records calls."""

    def __init__(self, *outcomes: str, on_turn: Callable[[int], None] | None = None) -> None:
        self.script = list(outcomes)
        self.on_turn = on_turn
        self.calls: list[dict[str, object]] = []

    async def run_turn(
        self,
        *,
        prompt: str,
        workspace: Path,
        session_id: str,
        resume: bool,
        turn_number: int,
        log_dir: Path,
        observer: TurnObserver | None = None,
        cancel: asyncio.Event | None = None,
    ) -> TurnResult:
        self.calls.append(
            {
                "prompt": prompt,
                "workspace": workspace,
                "session_id": session_id,
                "resume": resume,
                "turn_number": turn_number,
                "log_dir": log_dir,
                "context": dict(structlog.contextvars.get_contextvars()),
            }
        )
        if self.on_turn is not None:
            self.on_turn(turn_number)
        category = self.script.pop(0) if self.script else "ok"
        failed = category != "ok"
        log_dir.mkdir(parents=True, exist_ok=True)
        return TurnResult(
            turn_number=turn_number,
            session_id=session_id,
            model="claude-opus-5",
            api_key_source="none",
            exit_code=1 if failed else 0,
            subtype="error" if failed else "success",
            is_error=failed,
            num_turns=2,
            input_tokens=10,
            cache_creation_input_tokens=20,
            cache_read_input_tokens=30,
            output_tokens=5,
            cost_usd=0.25,
            duration_ms=1000,
            permission_denials=0,
            result_text="done",
            error_category=category if failed else None,  # type: ignore[arg-type]
            error=f"injected {category}" if failed else None,
            stdout_path=log_dir / f"turn-{turn_number}.jsonl",
            stderr_path=log_dir / f"turn-{turn_number}.stderr.log",
        )


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        max_turns: int = 3,
        template: str = TEMPLATE,
        hooks: dict[str, str] | None = None,
    ) -> None:
        self.settings = Settings.model_validate(
            {
                "github": {"repo": "example/repo"},
                "workspace": {"root": str(tmp_path / "workspaces")},
                "agent": {"max_turns": max_turns},
                "hooks": hooks or {},
            }
        )
        self.workflow = Workflow(
            path=tmp_path / "WORKFLOW.md",
            config=self.settings,
            prompt_template=template,
            raw_config={},
            source_mtime_ns=0,
        )
        self.github = FakeGitHub(self.settings.github)
        self.issue = self.github.add_issue(
            "Add retry backoff", labels=("issuebot/in-progress",), number=42
        )
        self.gh = StubGh()
        environ = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
        self.workspaces = WorkspaceManager(
            self.settings, gh=self.gh, environ=environ, hook_shell=("bash", "-c")
        )
        self.recorder = Recorder()
        self.bus = EventBus([self.recorder])

    async def run(self, runner: ScriptedRunner, **kwargs: object) -> RunResult:
        return await run_session(
            self.issue,
            self.workflow,
            self.github,
            self.bus,
            workspaces=self.workspaces,
            runner=runner,
            **kwargs,  # type: ignore[arg-type]
        )

    @property
    def workspace(self) -> Path:
        return self.workspaces.root / "repo-42"

    def kinds(self) -> list[str]:
        return [event.kind for event in self.recorder.events]


def test_new_run_id_is_sortable_and_unique() -> None:
    fixed = new_run_id(datetime(2026, 9, 3, 8, 12, 0, tzinfo=UTC))
    assert fixed.startswith("20260903T081200Z-")
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", fixed)
    assert new_run_id() != new_run_id()


async def test_stops_when_the_agent_moves_the_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    runner = ScriptedRunner(on_turn=lambda _: h.github.human_set_state(42, StateLabel.REVIEW))
    result = await h.run(runner, run_id="run-1")
    assert result.outcome == "succeeded"
    assert result.stop_reason == "issue_moved"
    assert result.error_category is None
    assert result.turns == 1
    assert result.attempt == 1
    assert result.run_id == "run-1"
    assert result.final_state is StateLabel.REVIEW
    assert result.final_issue is not None
    assert (result.input_tokens, result.output_tokens, result.cost_usd) == (60, 5, 0.25)
    assert result.workspace_path == h.workspace
    assert result.log_dir == h.workspace / ".issuebot" / "runs" / "run-1"
    assert runner.calls[0]["prompt"] == "Task repo-42 turn 1 attempt 1"
    assert runner.calls[0]["resume"] is False
    assert runner.calls[0]["session_id"] == result.session_id
    assert runner.calls[0]["workspace"] == h.workspace
    assert h.kinds() == ["run_started", "run_ended"]
    started = h.recorder.events[0]
    assert isinstance(started, RunStarted)
    assert started.session_id == result.session_id
    assert started.workspace_path == str(h.workspace)
    assert started.attempt == 1
    ended = h.recorder.events[1]
    assert isinstance(ended, RunEnded)
    assert (ended.outcome, ended.error, ended.turns) == ("succeeded", None, 1)
    assert (ended.input_tokens, ended.output_tokens, ended.cost_usd) == (60, 5, 0.25)
    record = h.workspaces.read_session(h.workspace)
    assert record is not None
    assert (record.turn_number, record.last_outcome, record.attempt) == (1, "succeeded", 1)
    assert record.session_id == result.session_id
    assert record.run_id == "run-1"


async def test_runs_until_max_turns_with_continuation_prompts(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_turns=3)
    runner = ScriptedRunner()
    result = await h.run(runner)
    assert result.stop_reason == "max_turns"
    assert result.outcome == "succeeded"
    assert result.turns == 3
    assert result.final_state is StateLabel.IN_PROGRESS
    assert [call["turn_number"] for call in runner.calls] == [1, 2, 3]
    assert [call["resume"] for call in runner.calls] == [False, True, True]
    assert str(runner.calls[1]["prompt"]).startswith("Continuation guidance:")
    assert "continuation turn 2 of 3" in str(runner.calls[1]["prompt"])
    assert (result.input_tokens, result.cost_usd) == (180, 0.75)
    ended = h.recorder.events[-1]
    assert isinstance(ended, RunEnded)
    assert ended.turns == 3


async def test_closed_issue_stops_as_moved(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    runner = ScriptedRunner(on_turn=lambda _: h.github.close_issue(42))
    result = await h.run(runner)
    assert result.stop_reason == "issue_moved"
    assert result.turns == 1


async def test_deleted_issue_stops_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)

    async def nothing(ids: object) -> list[object]:
        return []

    monkeypatch.setattr(h.github, "fetch_issues_by_ids", nothing)
    result = await h.run(ScriptedRunner())
    assert result.stop_reason == "issue_missing"
    assert result.outcome == "succeeded"
    assert result.final_issue is None
    assert result.final_state is StateLabel.IN_PROGRESS


async def test_resume_session_id_uses_continuation_on_turn_one(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_turns=1)
    runner = ScriptedRunner()
    result = await h.run(runner, resume_session_id="abc", attempt=2)
    assert result.session_id == "abc"
    assert result.attempt == 2
    assert runner.calls[0]["resume"] is True
    assert runner.calls[0]["session_id"] == "abc"
    assert str(runner.calls[0]["prompt"]).startswith("Continuation guidance:")
    assert "(attempt 2)" in str(runner.calls[0]["prompt"])


async def test_rework_flag_reaches_the_prompt(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_turns=1, template="rework={{ rework }}")
    runner = ScriptedRunner()
    await h.run(runner, rework=True)
    assert runner.calls[0]["prompt"] == "rework=True"


async def test_failed_turn_fails_the_run_and_still_runs_after_run(tmp_path: Path) -> None:
    h = Harness(tmp_path, hooks={"after_run": "touch after_run_ran"})
    runner = ScriptedRunner("turn_failed")
    result = await h.run(runner)
    assert result.outcome == "failed"
    assert result.stop_reason == "failure"
    assert result.error_category == "turn_failed"
    assert result.error == "injected turn_failed"
    assert result.turns == 1
    assert (h.workspace / "after_run_ran").exists()
    ended = h.recorder.events[-1]
    assert isinstance(ended, RunEnded)
    assert ended.outcome == "failed"
    assert ended.error == "turn_failed: injected turn_failed"
    record = h.workspaces.read_session(h.workspace)
    assert record is not None
    assert record.last_outcome == "failed"


@pytest.mark.parametrize(
    ("category", "outcome", "stop_reason"),
    [
        ("turn_timeout", "timed_out", "failure"),
        ("cancelled", "cancelled", "cancelled"),
        ("budget_exceeded", "failed", "failure"),
    ],
)
async def test_turn_categories_map_to_outcomes(
    tmp_path: Path, category: str, outcome: str, stop_reason: str
) -> None:
    h = Harness(tmp_path)
    result = await h.run(ScriptedRunner(category))
    assert result.outcome == outcome
    assert result.stop_reason == stop_reason
    assert result.error_category == category


async def test_refresh_failure_is_github_error(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.fail_next("transport")
    result = await h.run(ScriptedRunner())
    assert result.outcome == "failed"
    assert result.error_category == "github_error"
    assert result.error is not None
    assert "transport" in result.error
    assert result.turns == 1


async def test_prompt_error_fails_before_any_turn(tmp_path: Path) -> None:
    h = Harness(tmp_path, template="{{ nope }}")
    runner = ScriptedRunner()
    result = await h.run(runner)
    assert result.error_category == "prompt_error"
    assert result.turns == 0
    assert runner.calls == []
    assert h.kinds() == ["run_started", "run_ended"]


async def test_before_run_failure_is_hook_error(tmp_path: Path) -> None:
    h = Harness(tmp_path, hooks={"before_run": "exit 4"})
    runner = ScriptedRunner()
    result = await h.run(runner)
    assert result.error_category == "hook_error"
    assert result.error is not None
    assert "exit status 4" in result.error
    assert runner.calls == []


async def test_workspace_failure_fails_before_any_turn(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.gh.fail = GhResult(returncode=128, stdout="", stderr="fatal: nope\n")
    runner = ScriptedRunner()
    result = await h.run(runner)
    assert result.error_category == "workspace_error"
    assert result.turns == 0
    assert runner.calls == []
    assert result.workspace_path == h.workspace
    started = h.recorder.events[0]
    assert isinstance(started, RunStarted)
    assert started.workspace_path == str(h.workspace)
    assert h.kinds() == ["run_started", "run_ended"]


async def test_cancel_event_between_turns(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    cancel = asyncio.Event()
    runner = ScriptedRunner(on_turn=lambda _: cancel.set())
    result = await h.run(runner, cancel=cancel)
    assert result.outcome == "cancelled"
    assert result.stop_reason == "cancelled"
    assert result.turns == 1


async def test_log_context_is_bound_during_and_cleared_after(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_turns=1)
    runner = ScriptedRunner()
    result = await h.run(runner)
    context = runner.calls[0]["context"]
    assert isinstance(context, dict)
    assert context["issue_number"] == 42
    assert context["issue_identifier"] == "repo-42"
    assert context["session_id"] == result.session_id
    assert structlog.contextvars.get_contextvars() == {}


def test_github_error_message_is_used(tmp_path: Path) -> None:
    error = GitHubError("transport", "boom")
    assert "boom" in str(error)
