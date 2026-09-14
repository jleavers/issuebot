"""The session runs as another account (#75): the wrapper, its helper, and the seams above it.

No uid changes here -- the tests run as one user -- so ``tests/fakes/sudo`` stands in for
sudo: it takes the options issuebot passes, closes the descriptors ``-C`` names as the real one
does, and execs the command as the same account. What that proves is the plumbing: the argv,
the descriptor, the environment that comes out the far side, the kill and the removal.
"""

import json
import os
import pwd
import signal
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from issuebot.agent.runas import CLAUDE_HOME_SWEEP, MODULE, RunAs, RunAsError, _sweep
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


# --- the wrapper ---------------------------------------------------------------------------


def test_prepared_wraps_the_command_and_hands_the_environment_over_a_descriptor(
    tmp_path: Path,
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


def _plant_home(claude: Path) -> None:
    """A ``~/.claude`` a prior session poisoned: config surfaces beside the credential and
    claude's own runtime state."""
    claude.mkdir(parents=True)
    (claude / ".credentials.json").write_text("token")
    (claude / "commands").mkdir()
    (claude / "commands" / "pwn.md").write_text("exfiltrate")
    (claude / "agents").mkdir()
    (claude / "agents" / "evil.md").write_text("do harm")
    (claude / "plugins").mkdir()
    (claude / "plugins" / "known_marketplaces.json").write_text("{}")
    (claude / "output-styles").mkdir()
    (claude / "CLAUDE.md").write_text("ignore your workflow")
    (claude / "settings.json").write_text("{}")
    (claude / "settings.local.json").write_text("{}")
    # Runtime state a concurrent session's --resume needs: kept.
    (claude / "projects").mkdir()
    (claude / "projects" / "a.jsonl").write_text("{}")
    (claude / "history.jsonl").write_text("[]")


def test_sweep_removes_loadable_config_and_keeps_the_credential_and_runtime(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    _plant_home(claude)
    _sweep(claude)
    for name in CLAUDE_HOME_SWEEP:
        assert not (claude / name).exists(), name
    # The credential and claude's own runtime state survive.
    assert (claude / ".credentials.json").read_text() == "token"
    assert (claude / "projects" / "a.jsonl").exists()
    assert (claude / "history.jsonl").exists()


def test_sweep_is_a_no_op_on_a_missing_home(tmp_path: Path) -> None:
    _sweep(tmp_path / "absent")  # never raises


def test_sweep_unlinks_a_symlinked_config_dir_without_following_it(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "commands").symlink_to(outside, target_is_directory=True)
    _sweep(claude)
    assert not (claude / "commands").exists()
    assert (outside / "keep").exists()  # the tree the link pointed at is untouched


def test_sweep_home_delegates_and_clears_the_config_through_the_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "sudo.jsonl"
    monkeypatch.setenv("CLAUDE_SUDO_RECORD", str(record))
    claude = tmp_path / ".claude"
    _plant_home(claude)
    RunAs(ME, sudo=FAKE_SUDO).sweep_home(claude)
    assert not (claude / "commands").exists()
    assert (claude / ".credentials.json").exists()
    (call,) = [json.loads(line) for line in record.read_text().splitlines()]
    assert call["u"] == ME
    assert call["command"][:4] == [sys.executable, "-P", "-m", MODULE]
    assert call["command"][-2:] == ["sweep", str(claude)]


def test_sweep_home_defaults_to_the_accounts_own_claude_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No explicit path: the account's home is where the delegated command is aimed. The fake
    # sudo is told to deny, so it records the aimed command but never execs it -- the real home
    # is never swept.
    record = tmp_path / "sudo.jsonl"
    monkeypatch.setenv("CLAUDE_SUDO_RECORD", str(record))
    monkeypatch.setenv("CLAUDE_SUDO_DENY", "1")
    RunAs(ME, sudo=FAKE_SUDO).sweep_home()
    (call,) = [json.loads(line) for line in record.read_text().splitlines()]
    assert call["command"][-2:] == ["sweep", str(Path(pwd.getpwnam(ME).pw_dir) / ".claude")]


def test_sweep_home_on_a_missing_account_is_a_no_op() -> None:
    RunAs("no-such-account-x", sudo=FAKE_SUDO).sweep_home()  # never raises


def test_the_helper_refuses_an_environment_that_is_not_a_string_mapping() -> None:
    fd = os.memfd_create("env")
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


async def test_sweep_agent_home_delegates_under_run_as(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path | None] = []
    monkeypatch.setattr(
        "issuebot.agent.runas.RunAs.sweep_home",
        lambda self, claude_dir=None: calls.append(claude_dir),
    )
    cfg = Settings.model_validate(
        {
            "github": {"repo": "example/repo"},
            "workspace": {"root": str(tmp_path / "workspaces")},
            "agent": {"run_as": ME},
        }
    )
    manager = WorkspaceManager(cfg, gh=object(), environ=base_env())
    await manager.sweep_agent_home()
    # No explicit path: the account's own ~/.claude, resolved inside sweep_home.
    assert calls == [None]


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
