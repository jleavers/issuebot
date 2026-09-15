"""The session runs as another account (#75): the wrapper, its helper, and the seams above it.

No uid changes here -- the tests run as one user -- so ``tests/fakes/sudo`` stands in for
sudo: it takes the options issuebot passes, closes the descriptors ``-C`` names as the real one
does, and execs the command as the same account. What that proves is the plumbing: the argv,
the descriptor, the environment that comes out the far side, the kill and the removal.
"""

import contextlib
import errno
import json
import os
import pwd
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from issuebot.agent import runas as runas_module
from issuebot.agent.runas import MODULE, RunAs, RunAsError, anonymous_fd
from issuebot.agent.runner import ClaudeRunner
from issuebot.agent.workspace import SHARED_DIR_MODE, WorkspaceManager
from issuebot.config import Settings
from issuebot.config.resolve import resolve_config
from issuebot.github import Issue

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="accounts, sudo and bash")

FAKES = Path(__file__).parent / "fakes"
FAKE_SUDO = str(FAKES / "sudo")
ME = pwd.getpwuid(os.getuid()).pw_name


def fake_path() -> str:
    return f"{FAKES}{os.pathsep}{os.environ['PATH']}"


def base_env(**extra: str) -> dict[str, str]:
    return {"PATH": fake_path(), "HOME": "/elsewhere", **extra}


@pytest.fixture(params=["memfd", "unlinked-file"])
def descriptor_branch(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Both branches of ``anonymous_fd``, whichever one this interpreter would take (#115).

    ``uv`` installs a CPython configured against a glibc older than ``memfd_create``, so a
    developer's run and the image's take different branches, and a test that exercised only
    the local one would leave the other to production.
    """
    if request.param == "memfd" and not hasattr(os, "memfd_create"):
        pytest.skip("this interpreter has no os.memfd_create")
    if request.param == "unlinked-file":
        monkeypatch.delattr(os, "memfd_create", raising=False)
    return request.param


# --- the wrapper ---------------------------------------------------------------------------


def test_prepared_wraps_the_command_and_hands_the_environment_over_a_descriptor(
    tmp_path: Path, descriptor_branch: str
) -> None:
    record = tmp_path / "sudo.jsonl"
    runas = RunAs(ME, sudo=FAKE_SUDO)
    env = base_env(CLAUDE_SUDO_RECORD=str(record), GH_TOKEN="t", NOT_FOR_SUDO="x")
    completed = runas.run(["env", "-0"], env, timeout=10)
    assert completed.returncode == 0, completed.stderr
    seen = dict(item.split("=", 1) for item in completed.stdout.split("\0") if item)
    # Exactly what the worker built, with the account's identity: nothing sudo would add.
    assert seen == {**env, "HOME": pwd.getpwnam(ME).pw_dir, "USER": ME, "LOGNAME": ME}
    (call,) = [json.loads(line) for line in record.read_text().splitlines()]
    assert call["n"] is True and call["u"] == ME
    fd = int(call["command"][call["command"].index("--env-fd") + 1])
    assert call["C"] == fd + 1
    assert call["command"][:4] == [sys.executable, "-P", "-m", MODULE]
    assert call["command"][-3:] == ["--", "env", "-0"]


def test_anonymous_fd_hands_back_an_unnamed_descriptor_on_either_branch(
    descriptor_branch: str,
) -> None:
    fd = anonymous_fd("issuebot-test-env")
    try:
        assert os.write(fd, b"payload") == 7
        assert os.lseek(fd, 0, os.SEEK_SET) == 0
        assert os.read(fd, 16) == b"payload"
        # No directory entry names it, so nothing else can open what it holds.
        assert os.fstat(fd).st_nlink == 0
        # And it is the branch the fixture asked for, not whichever one this interpreter
        # would have taken anyway: a memfd's link is /memfd:<label>, the fallback's the
        # path it was unlinked from. Only Linux has the /proc to read that from, and only
        # Linux has memfd_create for the fallback to be a fallback from.
        if sys.platform.startswith("linux"):
            target = os.readlink(f"/proc/self/fd/{fd}")
            if descriptor_branch == "memfd":
                assert target.startswith("/memfd:"), target
            else:
                assert target.endswith(" (deleted)"), target
    finally:
        os.close(fd)


def test_anonymous_fd_prefers_memfd_create_wherever_the_interpreter_has_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatch itself, on an interpreter of either kind.

    CI installs the ``uv`` CPython, which has no ``memfd_create``, so the branch production
    takes would otherwise be proved only by hand on the image's Python (#115).
    """
    calls: list[str] = []

    def spy(name: str) -> int:
        calls.append(name)
        return os.open(os.devnull, os.O_RDWR)

    monkeypatch.setattr(os, "memfd_create", spy, raising=False)
    fd = anonymous_fd("issuebot-test-env")
    os.close(fd)
    assert calls == ["issuebot-test-env"]


def test_anonymous_fd_falls_back_when_the_call_is_there_but_the_kernel_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seccomp profile or an old kernel answers ``ENOSYS``; the fallback needs neither."""

    def refuse(name: str) -> int:
        raise OSError(errno.ENOSYS, "Function not implemented")

    monkeypatch.setattr(os, "memfd_create", refuse, raising=False)
    fd = anonymous_fd("issuebot-test-env")
    try:
        assert os.fstat(fd).st_nlink == 0
    finally:
        os.close(fd)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/self/fd")
def test_the_fallback_prefers_a_tmpfs_and_falls_through_when_there_is_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Where the file lands, which is the whole point of preferring one (#115).

    The descriptor carries the session's environment -- GH_TOKEN, the Anthropic credential,
    a DSN a ``before_run`` hook wrote -- so a fallback that quietly stopped preferring a
    tmpfs would put all of it on a disk-backed filesystem's freed blocks.
    """
    monkeypatch.delattr(os, "memfd_create", raising=False)
    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(runas_module, "SHM_DIR", str(shm))
    fd = anonymous_fd("issuebot-test-env")
    try:
        assert os.readlink(f"/proc/self/fd/{fd}").startswith(f"{shm}/")
    finally:
        os.close(fd)

    monkeypatch.setattr(runas_module, "SHM_DIR", str(tmp_path / "no-such-tmpfs"))
    fd = anonymous_fd("issuebot-test-env")
    try:
        target = os.readlink(f"/proc/self/fd/{fd}")
        assert target.startswith(f"{tempfile.gettempdir()}/"), target
        assert os.fstat(fd).st_nlink == 0
    finally:
        os.close(fd)


def test_anonymous_fd_reports_a_failed_fallback_as_an_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The spawn sites catch ``OSError``; the fallback must not invent another failure."""
    monkeypatch.delattr(os, "memfd_create", raising=False)
    monkeypatch.setattr(runas_module, "SHM_DIR", str(tmp_path / "no-such-tmpfs"))
    # ``tempfile.tempdir``, not ``TMPDIR``: ``gettempdir`` caches its answer in that global.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "no-such-directory"))
    with pytest.raises(FileNotFoundError):  # an OSError, which is the contract
        anonymous_fd("issuebot-test-env")


def test_anonymous_fd_closes_the_descriptor_when_the_unlink_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one path that could leak a descriptor holding the session's whole environment."""
    monkeypatch.delattr(os, "memfd_create", raising=False)
    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(runas_module, "SHM_DIR", str(shm))
    opened: list[tuple[int, str]] = []
    real_mkstemp = tempfile.mkstemp
    real_unlink = os.unlink

    def record(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, path = real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]
        opened.append((fd, path))
        return fd, path

    def deny(*_: object) -> None:
        raise OSError("denied")

    monkeypatch.setattr(tempfile, "mkstemp", record)
    monkeypatch.setattr(os, "unlink", deny)
    try:
        with pytest.raises(OSError):
            anonymous_fd("issuebot-test-env")
        # One attempt per candidate directory, and not one of them still holds a descriptor.
        assert [path for _, path in opened] == [
            path for _, path in opened if path.startswith((str(shm), tempfile.gettempdir()))
        ]
        assert len(opened) == 2
        for fd, _ in opened:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        # The unlink this test denied is the one that would have cleaned up after it.
        for _, path in opened:
            with contextlib.suppress(OSError):
                real_unlink(path)


def test_the_environment_arrives_whole_when_the_descriptor_takes_it_a_piece_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A memfd never wrote short; a file on a filesystem that fills mid-write can, and a
    truncated environment would reach the helper as unparseable JSON."""
    real_write = os.write

    def one_byte(fd: int, data: object) -> int:
        return real_write(fd, bytes(memoryview(data)[:1]))  # type: ignore[arg-type]

    monkeypatch.setattr(os, "write", one_byte)
    completed = RunAs(ME, sudo=FAKE_SUDO).run(["env", "-0"], base_env(GH_TOKEN="t"), timeout=10)
    monkeypatch.undo()
    assert completed.returncode == 0, completed.stderr
    seen = dict(item.split("=", 1) for item in completed.stdout.split("\0") if item)
    assert seen["GH_TOKEN"] == "t" and seen["USER"] == ME


def test_probe_answers_none_when_the_account_answers_and_names_the_refusal_otherwise() -> None:
    assert RunAs(ME, sudo=FAKE_SUDO).probe(base_env()) is None
    refused = RunAs(ME, sudo=FAKE_SUDO).probe(base_env(CLAUDE_SUDO_DENY="1"))
    assert refused == f"cannot run as {ME!r}: sudo: a password is required"
    assert RunAs("no-such-account-x", sudo=FAKE_SUDO).probe(base_env()) == (
        "agent.run_as: no account named 'no-such-account-x'"
    )
    missing = RunAs(ME, sudo="/nonexistent/sudo").probe(base_env())
    assert missing is not None and missing.startswith("cannot run '/nonexistent/sudo'")


def test_a_missing_account_is_an_oserror_for_the_spawn_sites() -> None:
    with pytest.raises(RunAsError), RunAs("no-such-account-x").prepared(["true"], base_env()):
        pass
    assert issubclass(RunAsError, OSError)


def test_kill_group_reaches_the_process_group_through_the_helper() -> None:
    process = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        RunAs(ME, sudo=FAKE_SUDO).kill_group(process.pid)
        assert process.wait(timeout=10) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()


def test_remove_tree_removes_what_the_account_owns_including_closed_directories(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "ws"
    (tree / "closed" / "inner").mkdir(parents=True)
    (tree / "closed" / "inner" / "file").write_text("x")
    os.chmod(tree / "closed", 0o500)
    try:
        RunAs(ME, sudo=FAKE_SUDO).remove_tree(tree)
        assert not tree.exists()
    finally:
        if tree.exists():
            os.chmod(tree / "closed", 0o700)


def test_the_helper_refuses_an_environment_that_is_not_a_string_mapping() -> None:
    fd = anonymous_fd("env")
    os.write(fd, b"[1, 2]")
    os.lseek(fd, 0, os.SEEK_SET)
    completed = subprocess.run(
        [sys.executable, "-m", MODULE, "exec", "--env-fd", str(fd), "--", "true"],
        pass_fds=(fd,),
        capture_output=True,
        text=True,
        check=False,
    )
    os.close(fd)
    assert completed.returncode != 0
    assert "not a mapping of strings" in completed.stderr


# --- the setting ----------------------------------------------------------------------------


def test_run_as_setting_accepts_an_account_name_and_nothing_else() -> None:
    cfg = Settings.model_validate({"github": {"repo": "o/r"}, "agent": {"run_as": "agent"}})
    assert cfg.agent.run_as == "agent"
    assert Settings.model_validate({"github": {"repo": "o/r"}}).agent.run_as is None
    with pytest.raises(ValueError, match="account name"):
        Settings.model_validate({"github": {"repo": "o/r"}, "agent": {"run_as": "-u root"}})


def test_run_as_falls_back_to_the_image_variable_unless_the_workflow_says(tmp_path: Path) -> None:
    resolved = resolve_config({}, environ={"ISSUEBOT_AGENT_USER": "agent"}, base_dir=tmp_path)
    assert resolved["agent"]["run_as"] == "agent"
    assert "agent" not in resolve_config({}, environ={}, base_dir=tmp_path)
    explicit = resolve_config(
        {"agent": {"run_as": "other"}}, environ={"ISSUEBOT_AGENT_USER": "agent"}, base_dir=tmp_path
    )
    assert explicit["agent"]["run_as"] == "other"


# --- through the runner and the workspace manager ------------------------------------------


async def test_a_turn_runs_through_the_wrapper(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    workspace = root / "ws"
    workspace.mkdir(parents=True)
    record = tmp_path / "sudo.jsonl"
    cfg = Settings.model_validate(
        {
            "github": {"repo": "example/repo"},
            "workspace": {"root": str(root)},
            "agent": {"run_as": ME},
            "claude": {"command": str(FAKES / "claude")},
        }
    )
    runner = ClaudeRunner(
        cfg,
        environ=base_env(
            HOME=str(tmp_path), CLAUDE_FAKE_SCENARIO="success", CLAUDE_SUDO_RECORD=str(record)
        ),
    )
    result = await runner.run_turn(
        prompt="hello",
        workspace=workspace,
        session_id="11111111-2222-4333-8444-555555555555",
        resume=False,
        turn_number=1,
        log_dir=workspace / ".issuebot" / "runs" / "r1",
    )
    assert result.error is None, result.error
    assert result.exit_code == 0
    (call,) = [json.loads(line) for line in record.read_text().splitlines()]
    assert call["u"] == ME
    assert call["command"][call["command"].index("--") + 1] == str(FAKES / "claude")


async def test_workspace_creation_and_removal_run_as_the_account(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    root = tmp_path / "workspaces"
    cfg = Settings.model_validate(
        {
            "github": {"repo": "example/repo"},
            "workspace": {"root": str(root)},
            "agent": {"run_as": ME},
            "hooks": {"after_create": "echo $USER > .issuebot/who; test -d .issuebot/runs"},
        }
    )
    record = tmp_path / "sudo.jsonl"
    manager = WorkspaceManager(
        cfg,
        gh=object(),  # never used: the clone goes through the account, not the worker's gh
        environ=base_env(HOME=str(tmp_path), CLAUDE_SUDO_RECORD=str(record)),
        hook_shell=("bash", "-c"),
    )
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert ws.created and (ws.path / ".git").is_dir()
    state = ws.path / ".issuebot"
    assert (state / "who").read_text().strip() == ME
    assert (state / "created").is_file() and (state / "runs").is_dir()
    for shared in (ws.path, state):
        assert stat.S_IMODE(shared.stat().st_mode) == SHARED_DIR_MODE
    calls = [json.loads(line) for line in record.read_text().splitlines()]
    commands = [c["command"][c["command"].index("--") + 1 :] for c in calls]
    assert commands[0][:3] == ["gh", "repo", "clone"], commands
    assert all(c["u"] == ME for c in calls)
    # Reuse, then removal: the account's files go through the helper, the worker's own after.
    again = await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert not again.created
    assert await manager.remove("example-42") is True
    assert not ws.path.exists()
    assert [c["command"][-2] for c in calls[len(commands) :]] == [] or any(
        "remove" in c["command"]
        for c in [json.loads(line) for line in record.read_text().splitlines()]
    )
