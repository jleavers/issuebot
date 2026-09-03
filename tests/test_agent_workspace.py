"""Tests for workspaces: keys, containment, clone, hooks, removal and session.json."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from issuebot.agent.errors import AgentError
from issuebot.agent.workspace import (
    SessionRecord,
    WorkspaceManager,
    run_log_dir,
    session_path,
    workspace_key,
)
from issuebot.config import Settings
from issuebot.github import GhResult, GhRunner, GitHubError, Issue

posix = pytest.mark.skipif(sys.platform == "win32", reason="hooks and git run through bash")
FAKE_GH = Path(__file__).parent / "fakes" / "gh"


class StubGh:
    """Records gh invocations; `repo clone` creates a real git repository at the target."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail: GitHubError | GhResult | None = None

    async def run(self, args: list[str], *, stdin: str | None = None) -> GhResult:
        argv = list(args)
        self.calls.append(argv)
        if isinstance(self.fail, GitHubError):
            raise self.fail
        if isinstance(self.fail, GhResult):
            return self.fail
        if argv[:2] == ["repo", "clone"]:
            subprocess.run(["git", "init", "-q", argv[3]], check=True)
        return GhResult(returncode=0, stdout="", stderr="")


def make_manager(
    tmp_path: Path,
    *,
    hooks: dict[str, str] | None = None,
    timeout_ms: int = 5000,
    extra_env: dict[str, str] | None = None,
) -> tuple[WorkspaceManager, StubGh]:
    settings = Settings.model_validate(
        {
            "github": {"repo": "example/repo"},
            "workspace": {"root": str(tmp_path / "workspaces")},
            "hooks": {**(hooks or {}), "timeout_ms": timeout_ms},
        }
    )
    gh = StubGh()
    environ = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        **(extra_env or {}),
    }
    manager = WorkspaceManager(settings, gh=gh, environ=environ, hook_shell=("bash", "-c"))
    return manager, gh


async def assert_gone(pid: int) -> None:
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"process {pid} is still alive")


# --- keys and containment -------------------------------------------------------------


@pytest.mark.parametrize("identifier", ["issuebot-42", "Repo.Name_1-2"])
def test_workspace_key_keeps_clean_identifiers(identifier: str) -> None:
    assert workspace_key(identifier) == identifier


def test_workspace_key_sanitises_and_suffixes() -> None:
    key = workspace_key("owner/repo#42")
    assert key.startswith("owner_repo_42-")
    suffix = key.rsplit("-", 1)[1]
    assert len(suffix) == 16
    assert all(char in "0123456789abcdef" for char in suffix)
    assert key == workspace_key("owner/repo#42")
    assert key != workspace_key("owner_repo#42")


@pytest.mark.parametrize("identifier", ["", ".", ".."])
def test_workspace_key_never_returns_dot_names(identifier: str) -> None:
    key = workspace_key(identifier)
    assert key not in ("", ".", "..")
    assert "-" in key


def test_path_for_is_inside_root(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    path = manager.path_for("issuebot-42")
    assert path == (tmp_path / "workspaces" / "issuebot-42").resolve()
    assert manager.is_contained(path)
    assert not manager.is_contained(manager.root)
    assert not manager.is_contained(tmp_path)
    assert manager.hook_shell == ("bash", "-c")


def test_default_hook_shell_is_a_login_bash(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {"github": {"repo": "example/repo"}, "workspace": {"root": str(tmp_path)}}
    )
    assert WorkspaceManager(settings, gh=StubGh(), environ={}).hook_shell == ("bash", "-lc")


def test_run_log_dir_and_session_path_layout() -> None:
    assert run_log_dir(Path("/w/x"), "r1") == Path("/w/x/.issuebot/runs/r1")
    assert session_path(Path("/w/x")) == Path("/w/x/.issuebot/session.json")


# --- create, reuse, remove --------------------------------------------------------------


@posix
async def test_create_clones_and_prepares_the_repository(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    hook = "mkdir -p .issuebot && echo created > .issuebot/hook.txt"
    manager, gh = make_manager(tmp_path, hooks={"after_create": hook})
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert ws.created
    assert ws.key == "example-42"
    assert ws.path == manager.root / "example-42"
    assert gh.calls == [["repo", "clone", "example/repo", str(ws.path), "--", "--depth", "1"]]
    assert (ws.path / ".git").is_dir()
    assert (ws.path / ".issuebot").is_dir()
    helpers = subprocess.run(
        ["git", "config", "--local", "--get-all", "credential.https://github.com.helper"],
        cwd=ws.path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert helpers == ["", "!gh auth git-credential"]
    exclude = (ws.path / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".issuebot/" in exclude
    assert (ws.path / ".issuebot" / "hook.txt").read_text() == "created\n"


@posix
async def test_reuse_skips_clone_and_hooks(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, gh = make_manager(tmp_path, hooks={"after_create": "touch hooked"})
    issue = make_issue(identifier="example-42")
    first = await manager.create_or_reuse(issue)
    (first.path / "hooked").unlink()
    second = await manager.create_or_reuse(issue)
    assert not second.created
    assert second.path == first.path
    assert len(gh.calls) == 1
    assert not (first.path / "hooked").exists()


@posix
async def test_remnant_without_git_is_recreated(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, _ = make_manager(tmp_path)
    remnant = manager.root / "example-42"
    remnant.mkdir(parents=True)
    (remnant / "junk").write_text("x")
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert ws.created
    assert not (ws.path / "junk").exists()
    assert (ws.path / ".git").is_dir()


@posix
async def test_remnant_file_is_replaced(tmp_path: Path, make_issue: Callable[..., Issue]) -> None:
    manager, _ = make_manager(tmp_path)
    manager.root.mkdir(parents=True, exist_ok=True)
    (manager.root / "example-42").write_text("not a directory")
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert ws.created
    assert (ws.path / ".git").is_dir()


@posix
async def test_clone_failure_removes_directory_and_raises(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, gh = make_manager(tmp_path)
    gh.fail = GhResult(returncode=128, stdout="", stderr="fatal: repository not found\n")
    with pytest.raises(AgentError) as exc:
        await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert exc.value.category == "workspace_error"
    assert "repository not found" in exc.value.message
    assert not (manager.root / "example-42").exists()


@posix
async def test_clone_transport_error_is_workspace_error(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, gh = make_manager(tmp_path)
    gh.fail = GitHubError("transport", "gh timed out after 60s")
    with pytest.raises(AgentError) as exc:
        await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert exc.value.category == "workspace_error"
    assert "gh timed out" in exc.value.message


@posix
async def test_after_create_failure_removes_directory(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, _ = make_manager(tmp_path, hooks={"after_create": "echo nope >&2; exit 3"})
    with pytest.raises(AgentError) as exc:
        await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert exc.value.category == "workspace_error"
    assert "after_create" in exc.value.message
    assert "nope" in exc.value.message
    assert not (manager.root / "example-42").exists()


@posix
async def test_remove_runs_before_remove_and_deletes(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    marker = tmp_path / "removed"
    manager, _ = make_manager(tmp_path, hooks={"before_remove": f"touch {marker}"})
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert await manager.remove("example-42") is True
    assert marker.exists()
    assert not ws.path.exists()
    assert await manager.remove("example-42") is False


@posix
async def test_remove_runs_before_remove_on_a_remnant(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    marker = tmp_path / "removed"
    manager, _ = make_manager(tmp_path, hooks={"before_remove": f"touch {marker}"})
    remnant = manager.root / "example-42"
    remnant.mkdir(parents=True)
    assert await manager.remove("example-42") is True
    assert marker.exists()
    assert not remnant.exists()


@posix
async def test_fake_gh_clone_creates_a_repository(tmp_path: Path) -> None:
    runner = GhRunner(command=str(FAKE_GH), environ={"PATH": os.environ["PATH"]})
    target = tmp_path / "ws"
    result = await runner.run(["repo", "clone", "o/r", str(target), "--", "--depth", "1"])
    assert result.returncode == 0
    assert (target / ".git").is_dir()


# --- hooks ------------------------------------------------------------------------------


@posix
async def test_hook_runs_in_workspace_with_agent_environment(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    script = "pwd; echo $GH_PROMPT_DISABLED; echo ${SSH_AUTH_SOCK:-unset}"
    manager, _ = make_manager(
        tmp_path, hooks={"before_run": script}, extra_env={"SSH_AUTH_SOCK": "/leak"}
    )
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    result = await manager.run_hook("before_run", ws.path)
    assert result is not None
    assert result.ok
    assert result.returncode == 0
    assert result.stdout_tail.splitlines() == [str(ws.path), "1", "unset"]
    assert result.summary == "exit status 0"


@posix
async def test_unconfigured_hook_returns_none(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, _ = make_manager(tmp_path)
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert await manager.run_hook("after_run", ws.path) is None


@posix
async def test_hook_failure_is_reported_not_raised(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, _ = make_manager(tmp_path, hooks={"before_run": "echo bad >&2; exit 2"})
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    result = await manager.run_hook("before_run", ws.path)
    assert result is not None
    assert not result.ok
    assert result.returncode == 2
    assert result.stderr_tail == "bad\n"
    assert result.summary == "exit status 2: bad"


@posix
async def test_hook_timeout_kills_the_process_group(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    pidfile = tmp_path / "pid"
    script = f"sleep 30 & echo $! > {pidfile}; wait"
    manager, _ = make_manager(tmp_path, hooks={"before_run": script}, timeout_ms=1500)
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    result = await manager.run_hook("before_run", ws.path)
    assert result is not None
    assert result.timed_out
    assert not result.ok
    assert result.returncode is None
    assert result.summary.startswith("timed out after")
    await assert_gone(int(pidfile.read_text()))


@posix
async def test_hook_output_is_truncated(tmp_path: Path, make_issue: Callable[..., Issue]) -> None:
    script = "head -c 5000 /dev/zero | tr '\\0' a"
    manager, _ = make_manager(tmp_path, hooks={"before_run": script})
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    result = await manager.run_hook("before_run", ws.path)
    assert result is not None
    assert len(result.stdout_tail) == 2000


# --- session.json -----------------------------------------------------------------------


def test_session_record_round_trip(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    ws = manager.root / "example-42"
    ws.mkdir(parents=True)
    record = SessionRecord(
        issue_number=42,
        issue_identifier="example-42",
        run_id="r1",
        session_id="s1",
        attempt=2,
        turn_number=3,
        last_outcome="succeeded",
        updated_at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
    )
    manager.write_session(ws, record)
    assert not (ws / ".issuebot" / "session.json.tmp").exists()
    data = json.loads(session_path(ws).read_text())
    assert data["version"] == 1
    assert data["updated_at"] == "2026-09-03T08:00:00+00:00"
    assert manager.read_session(ws) == record


def test_read_session_returns_none_for_missing_or_bad_files(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    ws = manager.root / "example-42"
    assert manager.read_session(ws) is None
    session_path(ws).parent.mkdir(parents=True)
    session_path(ws).write_text("{not json")
    assert manager.read_session(ws) is None
    session_path(ws).write_text(json.dumps({"version": 99}))
    assert manager.read_session(ws) is None
    session_path(ws).write_text(json.dumps({"version": 1, "issue_number": "x"}))
    assert manager.read_session(ws) is None
    good = {
        "version": 1,
        "issue_number": 42,
        "issue_identifier": "example-42",
        "run_id": "r1",
        "session_id": "s1",
        "attempt": 1,
        "turn_number": 0,
        "last_outcome": "bogus",
        "updated_at": "2026-09-03T08:00:00+00:00",
    }
    session_path(ws).write_text(json.dumps(good))
    assert manager.read_session(ws) is None
    good["last_outcome"] = None
    session_path(ws).write_text(json.dumps(good))
    record = manager.read_session(ws)
    assert record is not None
    assert record.last_outcome is None


# --- Phase 4 hardening: reuse marker and OSError conversion ---------------------------


@posix
async def test_git_without_issuebot_marker_is_recreated(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, gh = make_manager(tmp_path)
    issue = make_issue(identifier="example-42")
    first = await manager.create_or_reuse(issue)
    shutil.rmtree(first.path / ".issuebot")
    (first.path / "stale").write_text("x")
    second = await manager.create_or_reuse(issue)
    assert second.created
    assert len(gh.calls) == 2
    assert not (second.path / "stale").exists()
    assert (second.path / ".issuebot").is_dir()


@posix
async def test_issuebot_marker_is_created_after_the_after_create_hook(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, _ = make_manager(
        tmp_path, hooks={"after_create": "test ! -e .issuebot && touch hook-ran"}
    )
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert (ws.path / "hook-ran").exists()
    assert (ws.path / ".issuebot").is_dir()


@posix
async def test_marker_creation_failure_is_workspace_error(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, _ = make_manager(tmp_path, hooks={"after_create": "touch .issuebot"})
    with pytest.raises(AgentError) as exc:
        await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert exc.value.category == "workspace_error"
    assert ".issuebot" in exc.value.message
    assert not (manager.root / "example-42").exists()


async def test_root_creation_failure_is_workspace_error(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, gh = make_manager(tmp_path)
    manager.root.parent.mkdir(parents=True, exist_ok=True)
    manager.root.write_text("not a directory")
    with pytest.raises(AgentError) as exc:
        await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert exc.value.category == "workspace_error"
    assert "workspace root" in exc.value.message
    assert gh.calls == []


@posix
async def test_remove_failure_is_workspace_error(
    tmp_path: Path, make_issue: Callable[..., Issue], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = make_manager(tmp_path)
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))

    def refuse(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError(f"{path}: refused")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    with pytest.raises(AgentError) as exc:
        await manager.remove("example-42")
    assert exc.value.category == "workspace_error"
    assert "refused" in exc.value.message
    assert ws.path.is_dir()
