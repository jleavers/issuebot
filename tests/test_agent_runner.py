"""Tests for the claude -p runner."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from issuebot.agent.runner import (
    MIN_CLAUDE_VERSION,
    ClaudeRunner,
    StreamParser,
    TurnEvent,
    TurnResult,
    agent_environment,
    classify_result,
    parse_claude_version,
)
from issuebot.config import Settings

FAKE_CLAUDE = Path(__file__).parent / "fakes" / "claude"
FIXTURES = Path(__file__).parent / "fixtures" / "claude"
SESSION_ID = "11111111-2222-4333-8444-555555555555"
RECORDED_SESSION_ID = "00000000-0000-4000-8000-000000000000"


def settings(root: Path, **claude: object) -> Settings:
    return Settings.model_validate(
        {
            "github": {"repo": "example/repo"},
            "workspace": {"root": str(root)},
            "claude": {"command": str(FAKE_CLAUDE), **claude},
        }
    )


# --- argv and environment --------------------------------------------------------------


def test_build_argv_fresh_session_has_fixed_flags(tmp_path: Path) -> None:
    runner = ClaudeRunner(settings(tmp_path), environ={})
    assert runner.build_argv(session_id=SESSION_ID, resume=False) == [
        str(FAKE_CLAUDE),
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "auto",
        "--permission-prompts",
        "none",
        "--max-budget-usd",
        "5.0",
        "--session-id",
        SESSION_ID,
    ]


def test_build_argv_resume_and_every_optional_flag(tmp_path: Path) -> None:
    runner = ClaudeRunner(
        settings(
            tmp_path,
            model="opus",
            permission_mode="bypassPermissions",
            max_budget_usd=2.5,
            setting_sources=["project", "local"],
            append_system_prompt="Be terse.",
            allowed_tools=["Read", "Bash(git *)"],
            disallowed_tools=["WebFetch"],
        ),
        environ={},
    )
    argv = runner.build_argv(session_id=SESSION_ID, resume=True)
    assert argv[6] == "bypassPermissions"
    assert argv[10] == "2.5"
    assert argv[11:13] == ["--resume", SESSION_ID]
    assert argv[13:] == [
        "--model",
        "opus",
        "--setting-sources",
        "project,local",
        "--append-system-prompt",
        "Be terse.",
        "--allowedTools",
        "Read",
        "Bash(git *)",
        "--disallowedTools",
        "WebFetch",
    ]
    assert "--session-id" not in argv


def test_agent_environment_passes_only_the_allowed_names() -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "ANTHROPIC_API_KEY": "sk-1",
        "CLAUDE_CONFIG_DIR": "/cfg",
        "GIT_AUTHOR_NAME": "issuebot",
        "GIT_COMMITTER_EMAIL": "bot@example.com",
        "GH_TOKEN": "parent-token",
        "AWS_SECRET_ACCESS_KEY": "nope",
        "SSH_AUTH_SOCK": "/tmp/sock",
        "GIT_DIR": "/elsewhere",
    }
    assert agent_environment(parent, token=None) == {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "ANTHROPIC_API_KEY": "sk-1",
        "CLAUDE_CONFIG_DIR": "/cfg",
        "GIT_AUTHOR_NAME": "issuebot",
        "GIT_COMMITTER_EMAIL": "bot@example.com",
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "NO_COLOR": "1",
        "GH_PAGER": "cat",
        "DISABLE_AUTOUPDATER": "1",
    }


def test_agent_environment_adds_the_configured_token() -> None:
    env = agent_environment({"PATH": "/usr/bin"}, token=SecretStr("sekret"))
    assert env["GH_TOKEN"] == "sekret"


def test_child_environment_uses_the_settings_token(tmp_path: Path) -> None:
    cfg = Settings.model_validate(
        {
            "github": {"repo": "example/repo", "token": "from-settings"},
            "workspace": {"root": str(tmp_path)},
        }
    )
    runner = ClaudeRunner(cfg, environ={"PATH": "/usr/bin", "GH_TOKEN": "from-parent"})
    assert runner.child_environment()["GH_TOKEN"] == "from-settings"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2.1.259 (Claude Code)", (2, 1, 259)),
        ("v2.2.0\n", (2, 2, 0)),
        ("", None),
        (None, None),
        ("Claude Code", None),
    ],
)
def test_parse_claude_version(text: str | None, expected: tuple[int, int, int] | None) -> None:
    assert parse_claude_version(text) == expected


def test_minimum_version_is_the_permission_prompts_release() -> None:
    assert MIN_CLAUDE_VERSION == (2, 1, 259)


# --- stream parsing --------------------------------------------------------------------


def _lines(name: str) -> list[str]:
    return (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()


def test_parser_reads_init_activity_and_result() -> None:
    parser = StreamParser(turn_number=1, expected_session_id=RECORDED_SESSION_ID)
    events: list[TurnEvent] = []
    for line in _lines("success"):
        events.extend(parser.feed(line))
    assert [event.kind for event in events] == [
        "session_started",
        "turn_activity",
        "turn_activity",
        "turn_activity",
        "turn_activity",
    ]
    assert events[0].session_id == RECORDED_SESSION_ID
    assert events[0].detail == "claude-opus-5[1m]"
    assert [event.message_type for event in events[1:]] == [
        "rate_limit_event",
        "assistant",
        "user",
        "assistant",
    ]
    assert events[2].tool_name == "Read"
    assert all(event.turn_number == 1 for event in events)
    assert parser.model == "claude-opus-5[1m]"
    assert parser.api_key_source == "none"
    assert parser.result is not None
    assert parser.result["subtype"] == "success"
    assert parser.unparseable == 0


def test_parser_tolerates_blank_and_unparseable_lines() -> None:
    parser = StreamParser(turn_number=2, expected_session_id="x")
    assert parser.feed("") == []
    assert parser.feed("   \n") == []
    [event] = parser.feed("not json at all")
    assert event.kind == "turn_activity"
    assert event.message_type == "unparseable"
    [event] = parser.feed('["a", "list"]')
    assert event.message_type == "unparseable"
    [event] = parser.feed('{"type": "future_kind"}')
    assert event.message_type == "future_kind"
    [event] = parser.feed('{"no": "type"}')
    assert event.message_type == "unknown"
    assert parser.overrun().message_type == "unparseable"
    assert parser.unparseable == 3


def test_parser_reports_one_activity_per_tool_use_block() -> None:
    parser = StreamParser(turn_number=1, expected_session_id="x")
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "tool_use", "name": "Bash", "input": {}},
                    {"type": "tool_use", "name": "Edit", "input": {}},
                ]
            },
        }
    )
    assert [event.tool_name for event in parser.feed(line)] == ["Bash", "Edit"]
    [event] = parser.feed('{"type": "assistant", "message": {"content": "text only"}}')
    assert event.tool_name is None


@pytest.mark.parametrize(
    ("result", "exit_code", "category", "needle"),
    [
        ({"subtype": "success", "is_error": False}, 0, None, None),
        (None, 2, "process_exit", "status 2"),
        (
            {"subtype": "error_max_budget_usd", "is_error": True, "result": "over"},
            1,
            "budget_exceeded",
            "over",
        ),
        ({"subtype": "error_max_budget_usd", "is_error": True}, 1, "budget_exceeded", "cap"),
        (
            {"subtype": "error_during_execution", "is_error": True, "errors": ["a", "b"]},
            1,
            "turn_failed",
            "a; b",
        ),
        ({"subtype": "success", "is_error": True, "result": "bad"}, 0, "turn_failed", "bad"),
        ({"subtype": "success", "is_error": False}, 1, "process_exit", "reported success"),
        ({}, 0, "turn_failed", "unknown subtype"),
    ],
)
def test_classify_result(
    result: dict[str, object] | None, exit_code: int, category: str | None, needle: str | None
) -> None:
    got_category, message = classify_result(result, exit_code, "last stderr line")
    assert got_category == category
    if needle is None:
        assert message is None
    else:
        assert message is not None
        assert needle in message


# --- run_turn against the fake claude ---------------------------------------------------

posix = pytest.mark.skipif(
    sys.platform == "win32", reason="tests/fakes/claude is a POSIX shebang script"
)


class Recorder:
    def __init__(self) -> None:
        self.events: list[TurnEvent] = []

    def on_turn_event(self, event: TurnEvent) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspaces" / "example-42"
    path.mkdir(parents=True)
    return path


def runner_for(
    workspace: Path,
    *,
    scenario: str = "success",
    turn_timeout_ms: int = 30_000,
    extra_env: dict[str, str] | None = None,
    token: str | None = None,
    command: str | None = None,
) -> ClaudeRunner:
    github: dict[str, object] = {"repo": "example/repo"}
    if token is not None:
        github["token"] = token
    cfg = Settings.model_validate(
        {
            "github": github,
            "workspace": {"root": str(workspace.parent)},
            "claude": {"command": command or str(FAKE_CLAUDE), "turn_timeout_ms": turn_timeout_ms},
        }
    )
    environ = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "CLAUDE_FAKE_SCENARIO": scenario,
        **(extra_env or {}),
    }
    return ClaudeRunner(cfg, environ=environ)


async def run(
    runner: ClaudeRunner,
    workspace: Path,
    *,
    turn_number: int = 1,
    resume: bool = False,
    observer: Recorder | None = None,
    cancel: asyncio.Event | None = None,
    log_dir: Path | None = None,
) -> TurnResult:
    return await runner.run_turn(
        prompt="Do the thing",
        workspace=workspace,
        session_id=SESSION_ID,
        resume=resume,
        turn_number=turn_number,
        log_dir=log_dir or workspace / ".issuebot" / "runs" / "run-1",
        observer=observer,
        cancel=cancel,
    )


async def wait_for_file(path: Path) -> int:
    for _ in range(50):
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        await asyncio.sleep(0.1)
    raise AssertionError(f"{path} was not written")


async def assert_gone(pid: int) -> None:
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"process {pid} is still alive")


@posix
async def test_run_turn_success_parses_everything(workspace: Path, tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    runner = runner_for(
        workspace,
        extra_env={"CLAUDE_FAKE_RECORD": str(record), "SSH_AUTH_SOCK": "/leak"},
        token="sekret",
    )
    recorder = Recorder()
    log_dir = workspace / ".issuebot" / "runs" / "run-1"
    turn = await run(runner, workspace, observer=recorder, log_dir=log_dir)
    assert turn.ok
    assert turn.error_category is None
    assert turn.error is None
    assert turn.exit_code == 0
    assert turn.session_id == SESSION_ID
    assert turn.model == "claude-opus-5[1m]"
    assert turn.api_key_source == "none"
    assert turn.subtype == "success"
    assert turn.is_error is False
    assert turn.num_turns == 2
    assert (turn.input_tokens, turn.cache_creation_input_tokens) == (4, 6681)
    assert (turn.cache_read_input_tokens, turn.output_tokens) == (26750, 123)
    assert turn.total_input_tokens == 4 + 6681 + 26750
    assert turn.cost_usd == pytest.approx(0.084256)
    assert turn.duration_ms == 3660
    assert turn.permission_denials == 0
    assert turn.result_text == "hello issuebot"
    recorded = json.loads(record.read_text())
    assert recorded["stdin"] == "Do the thing"
    assert recorded["cwd"] == str(workspace.resolve())
    assert recorded["argv"][:2] == ["-p", "--output-format"]
    assert recorded["argv"][-2:] == ["--session-id", SESSION_ID]
    assert recorded["env"]["GH_TOKEN"] == "sekret"
    assert recorded["env"]["NO_COLOR"] == "1"
    assert recorded["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert recorded["env"]["SSH_AUTH_SOCK"] is None
    assert recorder.kinds == [
        "session_started",
        "turn_activity",
        "turn_activity",
        "turn_activity",
        "turn_activity",
        "process_exit",
        "turn_completed",
    ]
    assert recorder.events[2].tool_name == "Read"
    assert recorder.events[-2].detail == "0"
    assert turn.stdout_path == log_dir / "turn-1.jsonl"
    assert turn.stderr_path == log_dir / "turn-1.stderr.log"
    assert len(turn.stdout_path.read_text().splitlines()) == 6
    assert SESSION_ID in turn.stdout_path.read_text()
    assert turn.stderr_path.read_text() == ""
    assert (log_dir / "turn-1.prompt.md").read_text() == "Do the thing"


@posix
async def test_run_turn_resume_passes_the_resume_flag(workspace: Path, tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    runner = runner_for(workspace, extra_env={"CLAUDE_FAKE_RECORD": str(record)})
    turn = await run(runner, workspace, turn_number=2, resume=True)
    argv = json.loads(record.read_text())["argv"]
    assert argv[-2:] == ["--resume", SESSION_ID]
    assert "--session-id" not in argv
    assert turn.stdout_path.name == "turn-2.jsonl"


@posix
async def test_error_result_is_turn_failed(workspace: Path) -> None:
    recorder = Recorder()
    turn = await run(runner_for(workspace, scenario="error_result"), workspace, observer=recorder)
    assert turn.error_category == "turn_failed"
    assert turn.error is not None
    assert "error_during_execution" in turn.error
    assert "tool execution failed" in turn.error
    assert turn.exit_code == 1
    assert turn.session_id == SESSION_ID
    assert turn.cost_usd == pytest.approx(0.0412)
    assert recorder.kinds[-2:] == ["process_exit", "turn_failed"]


@posix
async def test_budget_result_is_budget_exceeded(workspace: Path) -> None:
    turn = await run(runner_for(workspace, scenario="budget"), workspace)
    assert turn.error_category == "budget_exceeded"
    assert turn.error is not None
    assert "Budget limit" in turn.error
    assert turn.cost_usd == pytest.approx(5.02)


@posix
async def test_crash_after_init_is_process_exit_with_stderr(workspace: Path) -> None:
    turn = await run(runner_for(workspace, scenario="crash_after_init"), workspace)
    assert turn.error_category == "process_exit"
    assert turn.exit_code == 2
    assert turn.error is not None
    assert "status 2" in turn.error
    assert "fake claude crashed" in turn.error
    assert turn.session_id == SESSION_ID
    assert turn.result_text is None


@posix
async def test_no_init_is_process_exit_without_a_session(workspace: Path) -> None:
    recorder = Recorder()
    turn = await run(runner_for(workspace, scenario="no_init"), workspace, observer=recorder)
    assert turn.error_category == "process_exit"
    assert turn.session_id is None
    assert turn.exit_code == 1
    assert turn.error is not None
    assert "No conversation found" in turn.error
    assert recorder.kinds == ["turn_activity", "process_exit", "turn_failed"]
    assert recorder.events[0].message_type == "unparseable"


@posix
async def test_silence_times_out_and_kills(workspace: Path, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    runner = runner_for(
        workspace,
        scenario="silent",
        turn_timeout_ms=500,
        extra_env={"CLAUDE_FAKE_PIDFILE": str(pidfile)},
    )
    recorder = Recorder()
    turn = await run(runner, workspace, observer=recorder)
    assert turn.error_category == "turn_timeout"
    assert turn.session_id == SESSION_ID
    assert turn.exit_code == -signal.SIGTERM
    await assert_gone(int(pidfile.read_text()))
    assert recorder.kinds[-2:] == ["process_exit", "turn_timeout"]


@posix
async def test_stubborn_child_is_killed_after_the_grace_period(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("issuebot.agent.runner.TERMINATE_GRACE_S", 0.5)
    pidfile = tmp_path / "pid"
    runner = runner_for(
        workspace,
        scenario="stubborn",
        turn_timeout_ms=500,
        extra_env={"CLAUDE_FAKE_PIDFILE": str(pidfile)},
    )
    turn = await run(runner, workspace)
    assert turn.error_category == "turn_timeout"
    assert turn.exit_code == -signal.SIGKILL
    await assert_gone(int(pidfile.read_text()))


@posix
async def test_cancel_event_stops_the_turn(workspace: Path, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    runner = runner_for(workspace, scenario="slow", extra_env={"CLAUDE_FAKE_PIDFILE": str(pidfile)})
    cancel = asyncio.Event()
    recorder = Recorder()
    task = asyncio.create_task(run(runner, workspace, observer=recorder, cancel=cancel))
    pid = await wait_for_file(pidfile)
    cancel.set()
    turn = await task
    assert turn.error_category == "cancelled"
    assert turn.exit_code == -signal.SIGTERM
    await assert_gone(pid)
    assert recorder.kinds[-2:] == ["process_exit", "turn_failed"]


@posix
async def test_task_cancellation_kills_and_reaps(workspace: Path, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    runner = runner_for(
        workspace, scenario="silent", extra_env={"CLAUDE_FAKE_PIDFILE": str(pidfile)}
    )
    recorder = Recorder()
    task = asyncio.create_task(run(runner, workspace, observer=recorder))
    pid = await wait_for_file(pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await assert_gone(pid)
    assert recorder.kinds[-1] == "process_exit"
    assert recorder.events[-1].detail == str(-signal.SIGTERM)


@posix
async def test_long_lines_are_parsed(workspace: Path) -> None:
    recorder = Recorder()
    turn = await run(runner_for(workspace, scenario="long_line"), workspace, observer=recorder)
    assert turn.ok
    assert len(turn.stdout_path.read_text().splitlines()) == 7
    assert [e.message_type for e in recorder.events].count("user") == 2


@posix
async def test_workspace_outside_the_root_is_rejected_without_spawning(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    runner = ClaudeRunner(settings(root), environ={"PATH": os.environ["PATH"]})
    log_dir = tmp_path / "logs"
    turn = await run(runner, outside, log_dir=log_dir)
    assert turn.error_category == "invalid_workspace_cwd"
    assert not log_dir.exists()
    turn = await run(runner, root, log_dir=log_dir)
    assert turn.error_category == "invalid_workspace_cwd"
    turn = await run(runner, root / "missing", log_dir=log_dir)
    assert turn.error_category == "invalid_workspace_cwd"


@posix
async def test_missing_executable_is_claude_not_found(workspace: Path) -> None:
    runner = runner_for(workspace, command="/nonexistent/claude")
    turn = await run(runner, workspace)
    assert turn.error_category == "claude_not_found"
    assert turn.error is not None
    assert "/nonexistent/claude" in turn.error
    assert turn.exit_code is None
    assert (workspace / ".issuebot" / "runs" / "run-1" / "turn-1.prompt.md").exists()
