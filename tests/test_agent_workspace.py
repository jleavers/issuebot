"""Tests for workspaces: keys, containment, clone, hooks, removal and session.json."""

import asyncio
import contextlib
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from issuebot.agent import workspace as workspace_module
from issuebot.agent.errors import AgentError
from issuebot.agent.uvcache import UV_CACHE_ROOT_NAME
from issuebot.agent.workspace import (
    FINISHED_MARKER,
    MAX_HOOK_OUTPUT_BYTES,
    SessionRecord,
    WorkspaceManager,
    _remove_path,
    run_log_dir,
    session_path,
    workspace_key,
)
from issuebot.config import Settings
from issuebot.github import GhResult, GhRunner, GitHubError, Issue
from issuebot.log import configure_logging

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
    hook_shell: tuple[str, ...] = ("bash", "-c"),
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
    manager = WorkspaceManager(settings, gh=gh, environ=environ, hook_shell=hook_shell)
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
async def test_reuse_keeps_the_clones_own_git_config(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    """The decided behaviour of #180: the reused clone is handed over whole, `.git` included.

    A workspace outlives its run and the clone inside it is the session's to write (#75), so a
    `--local` key or a `.git/hooks/` script one session leaves is there for the next session on
    that issue -- and both name commands git runs. That is a channel, it is inside the session's
    own privilege domain rather than across it, and
    `docs/superpowers/specs/2026-09-22-clone-reuse-residual-design.md` records the decision not
    to bound it: the unit is the clone and not the file, and not reusing the workspace is the
    only thing that would close it. Pinned here so that bounding it later fails this test and
    has to edit the note with it.
    """
    manager, gh = make_manager(tmp_path)
    issue = make_issue(identifier="example-42")
    first = await manager.create_or_reuse(issue)
    subprocess.run(
        ["git", "-C", str(first.path), "config", "--local", "alias.st", "!printf planted"],
        check=True,
    )
    # `include.path` is the one the note's argument turns on: it puts the same keys in a second
    # file, so a reset that walked `.git/config` alone would leave this one whole.
    (first.path / ".git" / "planted-include").write_text('[alias]\n\tinc = "!printf planted"\n')
    subprocess.run(
        ["git", "-C", str(first.path), "config", "--local", "include.path", "planted-include"],
        check=True,
    )
    hook = first.path / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nprintf planted\n")
    hook.chmod(0o755)
    hook_mode = hook.stat().st_mode & 0o777

    second = await manager.create_or_reuse(issue)

    assert not second.created
    assert second.path == first.path
    assert len(gh.calls) == 1

    def config(key: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(second.path), "config", "--get", key],
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    assert config("alias.st") == "!printf planted"
    # Read without `--local`, so this is git resolving the include as it would for any command.
    assert config("alias.inc") == "!printf planted"
    # `.git/hooks/` is the same shape beside the file and has no config key at all, so the
    # script itself is read back rather than only its path: still there, still executable.
    second_hook = second.path / ".git" / "hooks" / "post-checkout"
    assert second_hook.read_text() == "#!/bin/sh\nprintf planted\n"
    assert second_hook.stat().st_mode & 0o777 == hook_mode


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
async def test_hook_sees_the_workspace_env_file(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    # `before_run` writes the DSN; `after_run` and `before_remove` want it back.
    manager, _ = make_manager(
        tmp_path,
        hooks={
            "before_run": "printf 'export DSN=postgresql://issuebot@/db\\nPATH=/hijacked\\n'"
            " > .issuebot/env",
            "after_run": "echo ${DSN:-unset}; echo $PATH",
        },
    )
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    before = await manager.run_hook("before_run", ws.path)
    assert before is not None and before.ok
    after = await manager.run_hook("after_run", ws.path)
    assert after is not None and after.ok
    seen_dsn, seen_path = after.stdout_tail.splitlines()
    assert seen_dsn == "postgresql://issuebot@/db"
    assert seen_path != "/hijacked"


@posix
async def test_a_workspace_env_line_does_not_reach_a_hooks_login_shell(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    """#179, end to end through the shell a hook actually gets: `hook_shell` is `bash -lc`, and
    `bash` sources whatever `BASH_ENV` names before the command it was given. So a
    `before_run` line naming a script the session planted would run in every later hook --
    the post-clone setup and all four of them -- ahead of what the hook itself wrote. It is
    refused where the file is merged, and the worker's log names the key it dropped, while the
    DSN beside it on the next line is handed over exactly as before."""
    plant = tmp_path / "plant.sh"
    plant.write_text("printf 'PLANTED-BASH_ENV-RAN\\n'\n")
    manager, _ = make_manager(
        tmp_path,
        hooks={
            "before_run": (
                f"printf 'BASH_ENV={plant}\\nDSN=postgresql://issuebot@/db\\n' > .issuebot/env"
            ),
            "after_run": "echo hook-ran; echo ${DSN:-unset}",
        },
        hook_shell=("bash", "-lc"),
    )
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    before = await manager.run_hook("before_run", ws.path)
    assert before is not None and before.ok
    stream = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream)
    try:
        after = await manager.run_hook("after_run", ws.path)
    finally:
        configure_logging(stream=io.StringIO())
    assert after is not None and after.ok
    # The planted script would have printed first, before the hook's own `echo`.
    assert after.stdout_tail.splitlines() == ["hook-ran", "postgresql://issuebot@/db"]
    records = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    ignored = [r["reason"] for r in records if r["event"] == "workspace_env_ignored"]
    assert ignored == ["BASH_ENV is protected"]


@posix
async def test_a_workspace_env_line_does_not_reach_a_hooks_dynamic_loader(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    """#187, end to end through the shell a hook actually gets: `hook_shell` is `bash -lc`, and
    that `bash` is a dynamically linked binary, so `ld.so` reads its own names out of the
    environment the hook is handed before the shell reaches `main`. Three of the five are
    visible without a compiler, which is what this asserts: `LD_TRACE_LOADED_OBJECTS` and
    `LD_DEBUG=help` each make the loader print to stdout and exit 0 *instead of* running the
    hook's commands, and `LD_PRELOAD` naming a file that is not an ELF object makes it complain
    on the hook's stderr. All are refused where the file is merged, the worker's log names each
    key it dropped, and the DSN beside them on the next line is handed over exactly as before."""
    plant = tmp_path / "plant.so"
    plant.write_text("not an ELF object\n")
    manager, _ = make_manager(
        tmp_path,
        hooks={
            "before_run": (
                f"printf 'LD_PRELOAD={plant}\\nLD_AUDIT={plant}\\n"
                f"LD_LIBRARY_PATH={tmp_path}/libs\\nLD_TRACE_LOADED_OBJECTS=1\\n"
                "LD_DEBUG=help\\nDSN=postgresql://issuebot@/db\\n' > .issuebot/env"
            ),
            "after_run": "echo hook-ran; echo ${DSN:-unset}",
        },
        hook_shell=("bash", "-lc"),
    )
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    before = await manager.run_hook("before_run", ws.path)
    assert before is not None and before.ok
    stream = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream)
    try:
        after = await manager.run_hook("after_run", ws.path)
    finally:
        configure_logging(stream=io.StringIO())
    assert after is not None and after.ok
    # Unprotected, `LD_TRACE_LOADED_OBJECTS` would have printed the shell's own libraries here
    # and `LD_DEBUG=help` the loader's option list, neither running either `echo`, both exiting 0.
    assert after.stdout_tail.splitlines() == ["hook-ran", "postgresql://issuebot@/db"]
    # And `LD_PRELOAD` would have put the loader's complaint about the planted file on stderr.
    assert "ld.so" not in after.stderr_tail
    assert str(plant) not in after.stderr_tail
    records = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    ignored = [r["reason"] for r in records if r["event"] == "workspace_env_ignored"]
    assert ignored == [
        "LD_PRELOAD is protected",
        "LD_AUDIT is protected",
        "LD_LIBRARY_PATH is protected",
        "LD_TRACE_LOADED_OBJECTS is protected",
        "LD_DEBUG is protected",
    ]


async def test_sweep_agent_home_is_a_no_op_on_the_host_route(tmp_path: Path) -> None:
    # agent.run_as unset (the default here): the home is the operator's own, so nothing is
    # swept and the call is a no-op that never raises.
    manager, _ = make_manager(tmp_path)
    assert manager._runas is None
    await manager.sweep_agent_home()


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


@posix
@pytest.mark.parametrize("stream", ["1", "2"])
async def test_hook_output_over_the_cap_kills_the_group(
    tmp_path: Path, make_issue: Callable[..., Issue], stream: str
) -> None:
    """A hook that floods is killed as the bytes arrive, not buffered whole first (#139).

    ``after_create`` is where the target repository's dependency install runs, so the party
    growing this is the one the deployment invites, and the process holding the buffer is the
    worker, which supervises every concurrent session.
    """
    log = io.StringIO()
    configure_logging(level="DEBUG", stream=log)
    pidfile = tmp_path / "pid"
    flood = MAX_HOOK_OUTPUT_BYTES + (1 << 20)
    # The writer is a grandchild of the hook shell, as a dependency install's own child
    # processes are: killing the shell alone would leave it holding the pipe.
    script = f"( head -c {flood} /dev/zero | tr '\\0' x >&{stream} ) & echo $! > {pidfile}; wait"
    manager, _ = make_manager(tmp_path, hooks={"before_run": script}, timeout_ms=60_000)
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))

    try:
        result = await manager.run_hook("before_run", ws.path)
    finally:
        configure_logging(stream=io.StringIO())

    assert result is not None
    assert result.overrun
    assert not result.ok
    assert not result.timed_out  # the cap cut it short, long before the timer would have
    assert result.summary == f"output exceeded {MAX_HOOK_OUTPUT_BYTES} bytes"
    await assert_gone(int(pidfile.read_text()))
    records = [json.loads(line) for line in log.getvalue().splitlines() if line]
    failure = next(r for r in records if r.get("event") == "hook_failed")
    assert failure["overrun"] is True
    assert failure["max_output_bytes"] == MAX_HOOK_OUTPUT_BYTES


@posix
async def test_hook_output_at_the_cap_is_not_an_overrun(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    """A chatty-but-honest install runs to its end: the cap refuses the byte past it."""
    script = f"head -c {MAX_HOOK_OUTPUT_BYTES} /dev/zero | tr '\\0' a; echo done >&2"
    manager, _ = make_manager(tmp_path, hooks={"before_run": script}, timeout_ms=60_000)
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    result = await manager.run_hook("before_run", ws.path)
    assert result is not None
    assert not result.overrun
    assert result.ok
    assert result.returncode == 0
    assert result.stderr_tail == "done\n"


@posix
@pytest.mark.skipif(shutil.which("setsid") is None, reason="needs setsid to escape the group")
@pytest.mark.parametrize("kill_fails", [False, True])
async def test_a_flood_whose_writer_escapes_the_kill_is_still_an_overrun(
    tmp_path: Path,
    make_issue: Callable[..., Issue],
    monkeypatch: pytest.MonkeyPatch,
    kill_fails: bool,
) -> None:
    """A hook killed for flooding whose pipes then stay open past the timeout reports the
    cause and not the symptom (#139).

    The group kill is what usually ends the read, but a writer in a session of its own
    outlives it, and then the timer is what stops the hook. The bytes are bounded either
    way -- the reads have been dropping them since the cap -- so the run's error should say
    the hook flooded, not that it was slow.

    ``kill_fails`` is the same path with ``os.killpg`` refusing, which is what it does for a
    group at another uid: this is the one route that reaches ``_kill_quietly`` from the
    timeout branch, where a raised exception would take the place of the ``HookResult`` that
    reports the overrun.
    """
    log = io.StringIO()
    configure_logging(level="DEBUG", stream=log)
    pidfile = tmp_path / "pid"
    flood = MAX_HOOK_OUTPUT_BYTES + (1 << 20)
    escaped = f"echo $$ > {pidfile}; head -c {flood} /dev/zero | tr '\\0' x; sleep 20"
    manager, _ = make_manager(
        tmp_path, hooks={"before_run": f"setsid bash -c {shlex.quote(escaped)}"}, timeout_ms=3000
    )
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    if kill_fails:
        monkeypatch.setattr(
            workspace_module, "_kill_group", lambda process: _raise(PermissionError(1, "denied"))
        )

    try:
        result = await manager.run_hook("before_run", ws.path)
    finally:
        configure_logging(stream=io.StringIO())

    assert result is not None
    assert result.overrun
    assert not result.timed_out
    assert not result.ok
    assert result.summary == f"output exceeded {MAX_HOOK_OUTPUT_BYTES} bytes"
    records = [json.loads(line) for line in log.getvalue().splitlines() if line]
    failure = next(r for r in records if r.get("event") == "hook_failed")
    assert failure["overrun"] is True
    assert failure["timed_out"] is False
    assert failure["max_output_bytes"] == MAX_HOOK_OUTPUT_BYTES
    assert [r for r in records if r.get("event") == "hook_kill_failed"] or not kill_fails
    # Not the manager's to reap: it escaped the group on purpose, so the test cleans up.
    with contextlib.suppress(ProcessLookupError, ValueError):
        os.kill(int(pidfile.read_text()), signal.SIGKILL)


@posix
async def test_a_kill_that_cannot_land_is_a_warning_not_an_exception(
    tmp_path: Path, make_issue: Callable[..., Issue], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.killpg`` raises ``PermissionError`` for a group at another uid -- every hook's
    group under ``agent.run_as`` where the delegated kill did not take. From the killer task
    that exception would replace the cancellation the timeout raises, leaving ``_run_argv``
    to raise where a ``HookResult`` belongs, so it is logged instead (#139)."""
    log = io.StringIO()
    configure_logging(level="DEBUG", stream=log)
    manager, _ = make_manager(
        tmp_path,
        hooks={"before_run": f"head -c {MAX_HOOK_OUTPUT_BYTES + (1 << 20)} /dev/zero"},
        timeout_ms=10_000,
    )
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    monkeypatch.setattr(
        workspace_module, "_kill_group", lambda process: _raise(PermissionError(1, "denied"))
    )

    try:
        result = await manager.run_hook("before_run", ws.path)
    finally:
        configure_logging(stream=io.StringIO())

    assert result is not None
    assert result.overrun  # the hook still failed for the reason it failed for
    records = [json.loads(line) for line in log.getvalue().splitlines() if line]
    assert any(r.get("event") == "hook_kill_failed" for r in records)


def _raise(exc: BaseException) -> None:
    raise exc


@posix
async def test_a_flooding_hook_fails_the_run_with_the_overrun_in_the_error(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    """``after_create``'s failure is an ``AgentError`` quoting the summary, so the overrun
    reaches the run's error rather than being a log line nobody reads."""
    script = f"head -c {MAX_HOOK_OUTPUT_BYTES + (1 << 20)} /dev/zero | tr '\\0' x"
    manager, _ = make_manager(tmp_path, hooks={"after_create": script}, timeout_ms=60_000)
    with pytest.raises(AgentError) as excinfo:
        await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert str(MAX_HOOK_OUTPUT_BYTES) in excinfo.value.message
    assert "output exceeded" in excinfo.value.message


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
    assert data["workpad_comment_id"] is None
    assert manager.read_session(ws) == record
    pinned = replace(record, workpad_comment_id=5662693296)
    manager.write_session(ws, pinned)
    assert json.loads(session_path(ws).read_text())["workpad_comment_id"] == 5662693296
    assert manager.read_session(ws) == pinned
    # A file written before the field existed still reads, without it.
    del data["workpad_comment_id"]
    session_path(ws).write_text(json.dumps(data))
    assert manager.read_session(ws) == record
    data["workpad_comment_id"] = "not an id"
    session_path(ws).write_text(json.dumps(data))
    assert manager.read_session(ws) is None


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
    # `.issuebot` and its `runs/` exist before the hook (a hook may write `.issuebot/env`);
    # the `created` marker inside it is what says creation completed, and it comes after.
    script = "test -d .issuebot/runs && test ! -e .issuebot/created && touch hook-ran"
    manager, _ = make_manager(tmp_path, hooks={"after_create": script})
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert (ws.path / "hook-ran").exists()
    assert (ws.path / ".issuebot" / "created").is_file()


@posix
async def test_marker_creation_failure_is_workspace_error(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, _ = make_manager(tmp_path, hooks={"after_create": "rmdir .issuebot/runs .issuebot"})
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


@posix
def test_a_path_already_gone_is_not_a_removal_failure(tmp_path: Path) -> None:
    """The delegated pass runs before the worker's and can take the whole tree (#143), so the
    goal state is not a failure to reach it. `remove` says as much at its front door."""
    _remove_path(tmp_path / "gone", "cannot remove workspace")


@posix
def test_an_enoent_inside_a_surviving_tree_is_still_a_removal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#143's tolerance is ENOENT *and* nothing left at the path, not `ignore_errors`. An entry
    that vanished from under `rmtree` leaves the directory on disk, so the removal did not
    happen and saying it did would lose a session's files quietly."""
    remnant = tmp_path / "remnant"
    remnant.mkdir()

    def vanished_entry(path: object, *args: object, **kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", str(remnant / "inner"))

    monkeypatch.setattr(shutil, "rmtree", vanished_entry)
    with pytest.raises(AgentError) as exc:
        _remove_path(remnant, "cannot remove workspace")
    assert exc.value.category == "workspace_error"
    assert remnant.is_dir()


@posix
@pytest.mark.skipif(os.geteuid() == 0, reason="root unlinks anything")
def test_a_remnant_the_worker_cannot_unlink_is_still_a_removal_failure(tmp_path: Path) -> None:
    """The EACCES half: the failure this whole path exists to report (#143). A session's files
    the worker cannot remove must be said out loud, not tolerated along with a vanished tree."""
    closed = tmp_path / "closed"
    (closed / "inner").mkdir(parents=True)
    (closed / "inner" / "file").write_text("x")
    # Unlinking `inner` needs write on `closed`, which this does not give: rmtree empties what
    # it can and then fails, exactly as it does on another account's remnant.
    os.chmod(closed, 0o500)
    try:
        with pytest.raises(AgentError) as exc:
            _remove_path(closed, "cannot remove workspace")
        assert exc.value.category == "workspace_error"
        assert closed.is_dir()
    finally:
        os.chmod(closed, 0o700)


# --- the boundary (#104): what the worker reads back ---------------------------------------


@posix
def test_read_session_refuses_a_link_and_an_oversized_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from issuebot.agent import workspace as workspace_module
    from issuebot.agent.boundary import SESSION_FILE

    manager, _ = make_manager(tmp_path)
    ws = manager.root / "example-42"
    (ws / ".issuebot").mkdir(parents=True)
    record = SessionRecord(
        issue_number=42,
        issue_identifier="example-42",
        run_id="r1",
        session_id="s1",
        attempt=1,
        turn_number=1,
        last_outcome=None,
        updated_at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
    )
    elsewhere = tmp_path / "elsewhere.json"
    manager.write_session(tmp_path / "other", record)
    shutil.move(session_path(tmp_path / "other"), elsewhere)
    os.symlink(elsewhere, session_path(ws))
    assert manager.read_session(ws) is None
    os.unlink(session_path(ws))
    manager.write_session(ws, record)
    assert manager.read_session(ws) == record
    monkeypatch.setattr(
        workspace_module, "SESSION_FILE", replace(SESSION_FILE, limit=16), raising=True
    )
    assert manager.read_session(ws) is None


@posix
async def test_a_workspace_whose_marker_is_a_link_is_not_reused(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    from issuebot.agent.workspace import CREATED_MARKER

    manager, gh = make_manager(tmp_path)
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert ws.created
    marker = ws.path / ".issuebot" / CREATED_MARKER
    real = tmp_path / "real-marker"
    marker.rename(real)
    os.symlink(real, marker)
    calls = len(gh.calls)
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert ws.created  # recreated, not reused
    assert len(gh.calls) == calls + 1
    assert marker.is_file() and not marker.is_symlink()


@posix
async def test_a_pre_placed_marker_fails_creation_cleanly(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    from issuebot.agent.workspace import CREATED_MARKER

    manager, _ = make_manager(
        tmp_path, hooks={"after_create": f"ln -s /etc/hostname .issuebot/{CREATED_MARKER}"}
    )
    with pytest.raises(AgentError) as exc:
        await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert exc.value.category == "workspace_error"
    assert "cannot mark" in exc.value.message
    assert not (manager.root / "example-42").exists()


def test_hooks_read_the_env_file_through_the_managers_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under agent.run_as the hook that wrote `.issuebot/env` ran as the session, so the file
    is the session's; the manager's boundary, not a default one, is what admits it (#104)."""
    import stat as stat_module

    from issuebot.agent import boundary as boundary_module
    from issuebot.agent.boundary import Boundary
    from issuebot.agent.runner import agent_environment, workspace_environment

    manager, _ = make_manager(tmp_path)
    ws = manager.root / "example-42"
    (ws / ".issuebot").mkdir(parents=True)
    (ws / ".issuebot" / "env").write_text("DSN=postgresql:///x\n")
    me = os.getuid()
    real_fstat = os.fstat

    def fstat_as_session(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        if stat_module.S_ISREG(st.st_mode):
            return os.stat_result((*st[:4], me + 1, *st[5:]))
        return st

    monkeypatch.setattr(boundary_module.os, "fstat", fstat_as_session)
    split = Boundary(worker_uid=me, session_uid=me + 1)
    monkeypatch.setattr(manager, "_boundary", split)
    base = agent_environment({"PATH": "/usr/bin"}, token=None)
    # The default boundary (the worker alone) refuses the session-owned file...
    assert workspace_environment(base, ws)[1] == []
    # ...and the manager's admits it, which is what its hooks read through.
    assert workspace_environment(base, ws, boundary=manager._boundary)[1] == ["DSN"]


def test_the_managers_boundary_is_exposed_for_reads_made_outside_it(tmp_path: Path) -> None:
    """The session's instruction-file read (#107) is made by the run, not the manager, and
    it has to use the boundary that knows the session's uid (#104), so the manager hands it out."""
    from issuebot.agent.boundary import Boundary

    manager, _ = make_manager(tmp_path)
    assert isinstance(manager.boundary, Boundary)
    assert manager.boundary.worker_uid == os.getuid()
    assert manager.boundary.session_uid is None  # no agent.run_as here


# --- the removal mark and its retry (#149) ------------------------------------------------


@posix
async def test_a_failed_removal_leaves_the_mark_that_asks_for_a_retry(
    tmp_path: Path, make_issue: Callable[..., Issue], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry the terminal sweep used to buy by re-reading every completed issue (#149).

    `remove` writes `.issuebot/finished` before it unlinks anything, so a removal that failed
    leaves a workspace that says what should have happened to it. `finished_keys` is what the
    sweep asks instead of GitHub, and it is bounded by the disk.
    """
    manager, _ = make_manager(tmp_path)
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert manager.finished_keys() == []

    def refuse(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError(f"{path}: refused")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    with pytest.raises(AgentError):
        await manager.remove("example-42")

    assert (ws.path / ".issuebot" / FINISHED_MARKER).is_file()
    assert manager.finished_keys() == ["example-42"]

    monkeypatch.undo()
    assert await manager.remove_key("example-42") is True
    assert not ws.path.exists()
    assert manager.finished_keys() == []


@posix
async def test_a_successful_removal_leaves_nothing_to_retry(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, _ = make_manager(tmp_path)
    await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert await manager.remove("example-42") is True
    assert manager.finished_keys() == []


@posix
async def test_reusing_a_workspace_clears_the_removal_mark(
    tmp_path: Path, make_issue: Callable[..., Issue], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reopened issue is not one issuebot is done with, so its clone stops asking to be
    removed before the session that reuses it starts (#149)."""
    manager, _ = make_manager(tmp_path)
    issue = make_issue(identifier="example-42")
    ws = await manager.create_or_reuse(issue)

    def refuse(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError(f"{path}: refused")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    with pytest.raises(AgentError):
        await manager.remove("example-42")
    monkeypatch.undo()
    assert manager.finished_keys() == ["example-42"]

    reused = await manager.create_or_reuse(issue)

    assert reused.created is False
    assert reused.path == ws.path
    assert manager.finished_keys() == []


@posix
async def test_finished_keys_ignores_what_the_worker_did_not_write(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    """A workspace is the worker's directory and the mark is the worker's file, both reached
    without following a link (#104). Anything else asks for nothing."""
    manager, _ = make_manager(tmp_path)
    await manager.create_or_reuse(make_issue(identifier="example-42"))
    root = manager.root

    # The account registry, which is not a workspace at all (#121).
    (root / ".issuebot").mkdir(exist_ok=True)
    (root / ".issuebot" / FINISHED_MARKER).touch()
    # Nor is the per-account uv cache root (#164). Both are the worker's own directories
    # beside the workspace keys, so neither is refused by the boundary the way a session's
    # plant is: they are stepped over by name, as `seal_idle` steps over them, because a
    # retry that took one for a clone would unlink every session account's cache.
    cache_root = root / UV_CACHE_ROOT_NAME
    (cache_root / ".issuebot").mkdir(parents=True)
    (cache_root / ".issuebot" / FINISHED_MARKER).touch()
    # A plain file where a workspace would be.
    (root / "not-a-workspace").write_text("", encoding="utf-8")
    # A mark that is a symbolic link rather than a file.
    linked = root / "linked"
    (linked / ".issuebot").mkdir(parents=True)
    (linked / ".issuebot" / FINISHED_MARKER).symlink_to(tmp_path / "elsewhere")
    # A workspace directory that is a link to somewhere else entirely.
    outside = tmp_path / "outside"
    (outside / ".issuebot").mkdir(parents=True)
    (outside / ".issuebot" / FINISHED_MARKER).touch()
    (root / "escaped").symlink_to(outside)
    # And one genuine mark, so the assertion is about what is refused and not about an
    # answer that is empty whatever it is given.
    (root / "example-42" / ".issuebot" / FINISHED_MARKER).touch()

    assert manager.finished_keys() == ["example-42"]


@posix
async def test_finished_keys_survives_a_root_that_is_not_there_yet(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    assert not manager.root.exists()
    assert manager.finished_keys() == []


@posix
async def test_a_partial_removal_that_ate_the_mark_gets_it_back(
    tmp_path: Path, make_issue: Callable[..., Issue], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rmtree` is not atomic, and the mark lives inside the tree it is about to delete.

    It walks the workspace's entries in readdir order and stops at the first it cannot unlink,
    having already taken everything it reached -- `.issuebot`, mark and all, on half the
    orderings. The mark is the only thing that asks for another attempt, so `remove` puts it
    back where the directory survived (#149).
    """
    manager, _ = make_manager(tmp_path)
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    (ws.path / "keep").mkdir()

    real_rmtree = shutil.rmtree

    def half(path: object, *args: object, **kwargs: object) -> None:
        """Take `.issuebot` with the mark in it, then refuse, as an interrupted walk does."""
        real_rmtree(Path(str(path)) / ".issuebot")
        raise PermissionError(f"{path}: refused")

    monkeypatch.setattr(shutil, "rmtree", half)
    with pytest.raises(AgentError):
        await manager.remove("example-42")
    monkeypatch.undo()

    assert (ws.path / ".issuebot" / FINISHED_MARKER).is_file()
    assert manager.finished_keys() == ["example-42"]
    assert await manager.remove_key("example-42") is True
    assert not ws.path.exists()


@posix
async def test_a_mark_the_worker_did_not_write_is_taken_back(
    tmp_path: Path, make_issue: Callable[..., Issue], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.issuebot` is shared with the session under `agent.run_as` (#75), so the session can
    put the name there -- and `finished_keys` refuses a mark that is not the worker's own
    file, so leaving one would silently cost the retry. The directory is the worker's."""
    stream = io.StringIO()
    configure_logging(level="WARNING", stream=stream)
    manager, _ = make_manager(tmp_path)
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    (ws.path / ".issuebot" / FINISHED_MARKER).symlink_to(tmp_path / "nowhere")

    def refuse(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError(f"{path}: refused")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    with pytest.raises(AgentError):
        await manager.remove("example-42")

    assert (ws.path / ".issuebot" / FINISHED_MARKER).is_file()
    assert not (tmp_path / "nowhere").exists()
    assert manager.finished_keys() == ["example-42"]
    assert "workspace_mark_replaced" in stream.getvalue()


@posix
async def test_a_workspace_whose_state_directory_is_gone_is_still_removed(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    """A workspace too damaged to carry a mark is still a workspace to remove (#149): the
    state directory is recreated for it, closed, and the removal goes on either way."""
    manager, _ = make_manager(tmp_path)
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    shutil.rmtree(ws.path / ".issuebot")

    assert await manager.remove("example-42") is True
    assert not ws.path.exists()


@posix
async def test_marking_a_workspace_is_a_no_op_when_there_is_none(tmp_path: Path) -> None:
    """`finish_terminal` marks before it moves the label, and most issues have no clone."""
    manager, _ = make_manager(tmp_path)
    manager.mark_finished("example-42")
    assert manager.finished_keys() == []
