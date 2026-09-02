"""Tests for the gh subprocess boundary, against tests/fakes/gh."""

import json
import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from issuebot.github.errors import GitHubError
from issuebot.github.runner import GhResult, GhRunner

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
