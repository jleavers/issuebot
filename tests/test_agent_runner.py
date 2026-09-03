"""Tests for the claude -p runner."""

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from issuebot.agent.runner import (
    MIN_CLAUDE_VERSION,
    ClaudeRunner,
    StreamParser,
    TurnEvent,
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
