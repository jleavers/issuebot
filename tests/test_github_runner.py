"""Tests for the gh subprocess boundary, against tests/fakes/gh."""

import asyncio
import io
import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from issuebot.github.errors import GitHubError
from issuebot.github.runner import GhResult, GhRunner
from issuebot.log import configure_logging

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="tests/fakes/gh is a POSIX shebang script"
)

FAKE_GH = Path(__file__).parent / "fakes" / "gh"


def _runner(**kwargs: object) -> GhRunner:
    environ = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    environ.update(kwargs.pop("extra_env", {}))  # type: ignore[arg-type]
    return GhRunner(command=str(FAKE_GH), environ=environ, **kwargs)  # type: ignore[arg-type]


async def test_run_passes_argv_and_captures_output() -> None:
    result = await _runner().run(["api", "user", "--jq", ".login"])
    assert isinstance(result, GhResult)
    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["argv"] == ["api", "user", "--jq", ".login"]
    assert payload["stdin"] == ""


async def test_run_writes_stdin() -> None:
    result = await _runner().run(["api", "--input", "-"], stdin='{"body": "hi"}')
    assert json.loads(result.stdout)["stdin"] == '{"body": "hi"}'


async def test_child_environment_sets_fixed_variables_and_token() -> None:
    runner = _runner(token=SecretStr("sekret"))
    env = runner.child_environment()
    assert env["GH_TOKEN"] == "sekret"
    assert env["GH_PROMPT_DISABLED"] == "1"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"
    assert env["NO_COLOR"] == "1"
    assert env["GH_PAGER"] == "cat"
    result = await runner.run(["x"])
    assert json.loads(result.stdout)["env"]["GH_TOKEN"] == "sekret"


async def test_child_environment_without_token_has_no_gh_token() -> None:
    result = await _runner().run(["x"])
    env = json.loads(result.stdout)["env"]
    assert env["GH_TOKEN"] is None
    assert env["GH_PROMPT_DISABLED"] == "1"


async def test_non_zero_exit_is_returned_not_raised() -> None:
    result = await _runner(extra_env={"FAKE_GH_SCENARIO": "fail"}).run(["api", "x"])
    assert result.returncode == 1
    assert result.stdout == ""
    assert "HTTP 404" in result.stderr


async def test_timeout_kills_and_raises_transport() -> None:
    runner = _runner(timeout_ms=1000, extra_env={"FAKE_GH_SCENARIO": "sleep"})
    with pytest.raises(GitHubError) as exc:
        await runner.run(["api", "slow"])
    assert exc.value.category == "transport"
    assert exc.value.retryable
    assert "timed out" in exc.value.message


async def test_missing_executable_raises_config() -> None:
    runner = GhRunner(command="/nonexistent/gh", environ={"PATH": "/nonexistent"})
    with pytest.raises(GitHubError) as exc:
        await runner.run(["--version"])
    assert exc.value.category == "config"
    assert not exc.value.retryable


async def test_non_executable_command_raises_config(tmp_path: Path) -> None:
    path = tmp_path / "gh"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o644)
    runner = GhRunner(command=str(path), environ={"PATH": "/nonexistent"})
    with pytest.raises(GitHubError) as exc:
        await runner.run(["--version"])
    assert exc.value.category == "config"
    assert not exc.value.retryable


async def test_timeout_logs_debug_line() -> None:
    stream = io.StringIO()
    configure_logging(level="DEBUG", stream=stream)
    runner = _runner(
        timeout_ms=1000, token=SecretStr("sekret"), extra_env={"FAKE_GH_SCENARIO": "sleep"}
    )
    with pytest.raises(GitHubError) as exc:
        await runner.run(["api", "slow"])
    assert exc.value.category == "transport"

    # Parse the captured JSON lines
    output = stream.getvalue()
    found = False
    for line in output.split("\n"):
        if not line:
            continue
        record = json.loads(line)
        if record.get("event") == "gh_invocation":
            assert record["exit_code"] is None
            assert record.get("timed_out") is True
            assert "env" not in record
            assert "sekret" not in output
            found = True
            break
    assert found, "No gh_invocation record found"


async def test_external_cancellation_kills_and_reaps(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    runner = _runner(
        timeout_ms=30000,
        extra_env={
            "FAKE_GH_SCENARIO": "sleep",
            "FAKE_GH_PIDFILE": str(pidfile),
        },
    )

    # Start the run task and wrap with outer timeout to cancel from outside
    task = asyncio.create_task(runner.run(["api", "slow"]))

    # Wait for the pidfile to be created (poll for up to 2 seconds)
    for _ in range(20):
        if pidfile.exists():
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("Pidfile was not created")

    # Cancel the task after 1 second
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(task, timeout=1.0)

    # Check that the process was killed and reaped
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
