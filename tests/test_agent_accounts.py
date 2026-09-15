"""One account per concurrent session (#121): the binding, the wall and the credential rule."""

import json
import os
import pwd
import stat
import sys
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest

from issuebot.agent.accounts import (
    SEALED_DIR_MODE,
    WORKSPACE_DIR_MODE,
    AccountRegistry,
    account_gid,
    credential_complaint,
    group_complaint,
    pool_complaint,
    seal,
    session_account,
    settings_with_run_as,
    share_with,
)
from issuebot.agent.errors import AgentError
from issuebot.config import Settings
from issuebot.config.resolve import resolve_config

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX accounts and groups")

ME = pwd.getpwuid(os.getuid()).pw_name
POOL = ("agent-1", "agent-2", "agent-3")


def settings(**agent: object) -> Settings:
    return Settings.model_validate({"github": {"repo": "o/r"}, "agent": agent})


# --- the setting ---------------------------------------------------------------------------


def test_run_as_accepts_one_account_a_list_or_a_comma_separated_variable() -> None:
    assert settings(run_as="agent").agent.run_as == ("agent",)
    assert settings(run_as=["agent-1", "agent-2"]).agent.run_as == POOL[:2]
    assert settings(run_as="agent-1, agent-2, agent-3").agent.run_as == POOL
    assert settings().agent.run_as == ()
    assert settings(run_as=POOL).agent.run_as_pooled
    assert not settings(run_as="agent").agent.run_as_pooled
    assert not settings().agent.run_as_pooled


def test_run_as_refuses_a_name_that_is_not_an_account_or_a_repeat() -> None:
    with pytest.raises(ValueError, match="account name"):
        settings(run_as=["agent-1", "-u root"])
    with pytest.raises(ValueError, match="same account twice"):
        settings(run_as=["agent-1", "agent-1"])
    with pytest.raises(ValueError, match="account name"):
        settings(run_as=[])


def test_the_image_variable_carries_a_pool_as_well_as_one_account(tmp_path: Path) -> None:
    resolved = resolve_config(
        {}, environ={"ISSUEBOT_AGENT_USER": "agent-1,agent-2"}, base_dir=tmp_path
    )
    assert Settings.model_validate({"github": {"repo": "o/r"}, **resolved}).agent.run_as == POOL[:2]


def test_settings_narrow_to_one_account_and_back_to_the_host_route() -> None:
    pooled = settings(run_as=POOL)
    assert session_account(pooled) == "agent-1"
    one = settings_with_run_as(pooled, "agent-2")
    assert one.agent.run_as == ("agent-2",) and not one.agent.run_as_pooled
    assert session_account(one) == "agent-2"
    assert settings_with_run_as(pooled, None).agent.run_as == ()
    assert session_account(settings()) is None
    # Nothing to change is nothing copied.
    assert settings_with_run_as(one, "agent-2") is one


# --- the credential rule -------------------------------------------------------------------


def test_a_pool_needs_a_credential_in_the_environment() -> None:
    pooled = settings(run_as=POOL)
    complaint = credential_complaint(pooled, {})
    assert complaint is not None
    assert "CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY" in complaint
    assert credential_complaint(pooled, {"CLAUDE_CODE_OAUTH_TOKEN": "t"}) is None
    assert credential_complaint(pooled, {"ANTHROPIC_API_KEY": "k"}) is None
    assert credential_complaint(pooled, {"CLAUDE_CODE_OAUTH_TOKEN": ""}) is not None


def test_a_single_session_account_also_needs_an_environment_credential() -> None:
    """#142: the container has no interactive login, so one account is the same rule as N."""
    complaint = credential_complaint(settings(run_as="agent"), environ={})
    assert complaint is not None
    assert "CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY" in complaint


def test_a_single_session_account_with_a_credential_is_silent() -> None:
    environ = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
    assert credential_complaint(settings(run_as="agent"), environ) is None


def test_the_host_route_needs_no_environment_credential() -> None:
    """`run_as` unset is the operator's own account, which has its own login (#142)."""
    assert credential_complaint(settings(), environ={}) is None


# --- the wall -----------------------------------------------------------------------------


def test_a_workspace_is_given_to_its_accounts_group_and_nobody_else(tmp_path: Path) -> None:
    path = tmp_path / "ws"
    path.mkdir(mode=0o755)
    share_with(path, ME)
    assert stat.S_IMODE(path.stat().st_mode) == WORKSPACE_DIR_MODE
    assert path.stat().st_gid == account_gid(ME)
    # Sticky, so the session can write inside but cannot unlink the worker's own entries.
    assert WORKSPACE_DIR_MODE & stat.S_ISVTX
    assert not WORKSPACE_DIR_MODE & (stat.S_IRWXO)


def test_an_unknown_account_is_named_rather_than_guessed(tmp_path: Path) -> None:
    with pytest.raises(AgentError, match="no account named"):
        account_gid("no-such-account-x")
    assert group_complaint("no-such-account-x") == "no account named 'no-such-account-x'"
    assert group_complaint(ME) is None


# --- the registry --------------------------------------------------------------------------


def registry(tmp_path: Path, accounts: tuple[str, ...] = POOL) -> AccountRegistry:
    return AccountRegistry(tmp_path, accounts)


def test_allocation_spreads_over_the_pool_and_a_workspace_keeps_its_account(
    tmp_path: Path,
) -> None:
    pool = registry(tmp_path)
    assert [pool.allocate(key) for key in ("a", "b", "c")] == list(POOL)
    # The fourth shares with the least loaded, which is the first in pool order.
    assert pool.allocate("d") == "agent-1"
    # Idempotent: a rework is dispatched into the same clone, so it needs the same uid.
    assert pool.allocate("a") == "agent-1"
    assert pool.bound("a") == "agent-1"
    assert pool.bound("never-seen") is None


def test_allocation_leaves_a_candidate_whose_pool_is_busy_for_a_later_tick(
    tmp_path: Path,
) -> None:
    pool = registry(tmp_path)
    assert pool.allocate("a", busy={"agent-1"}) == "agent-2"
    assert pool.allocate("b", busy=set(POOL)) is None
    assert pool.bound("b") is None
    # An account already bound is still the answer, busy or not: the caller decides to wait.
    assert pool.allocate("a", busy=set(POOL)) == "agent-2"


def test_a_binding_survives_a_restart_and_is_read_from_the_record(tmp_path: Path) -> None:
    assert registry(tmp_path).allocate("a") == "agent-1"
    assert registry(tmp_path).bound("a") == "agent-1"
    document = json.loads((tmp_path / ".issuebot" / "accounts.json").read_text())
    assert document == {"version": 1, "bindings": {"a": "agent-1"}}
    # The worker's alone: neither the record nor the directory holding it is another's to edit.
    assert stat.S_IMODE((tmp_path / ".issuebot" / "accounts.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / ".issuebot").stat().st_mode) == 0o700


def test_an_account_that_leaves_the_pool_takes_its_bindings_with_it(tmp_path: Path) -> None:
    registry(tmp_path).allocate("a")
    assert registry(tmp_path, ("agent-2", "agent-3")).bound("a") is None


def test_pruning_forgets_a_removed_workspace_but_never_a_live_one(tmp_path: Path) -> None:
    pool = registry(tmp_path)
    for key in ("gone", "on-disk", "starting"):
        pool.allocate(key)
    (tmp_path / "on-disk").mkdir()
    pool.prune(keep={"starting"})
    assert sorted(pool.bindings()) == ["on-disk", "starting"]


def test_a_record_that_will_not_read_is_reported_rather_than_ignored(tmp_path: Path) -> None:
    (tmp_path / ".issuebot").mkdir()
    (tmp_path / ".issuebot" / "accounts.json").write_text("{not json")
    with pytest.raises(AgentError, match="unusable"):
        registry(tmp_path).bound("a")
    (tmp_path / ".issuebot" / "accounts.json").write_text(json.dumps({"version": 99}))
    with pytest.raises(AgentError, match="unsupported version"):
        registry(tmp_path).bound("a")
    (tmp_path / ".issuebot" / "accounts.json").write_text(json.dumps({"version": 1}))
    with pytest.raises(AgentError, match="not a mapping"):
        registry(tmp_path).bound("a")


def _two_accounts_sharing_a_group() -> tuple[str, str] | None:
    """Two real accounts with one primary group, or None on a host that has no such pair."""
    by_gid: dict[int, str] = {}
    for entry in pwd.getpwall():
        first = by_gid.setdefault(entry.pw_gid, entry.pw_name)
        if first != entry.pw_name:
            return first, entry.pw_name
    return None


def test_a_pool_whose_accounts_share_a_primary_group_walls_nothing_off() -> None:
    """`useradd -g agents` for every member is an easy way to build a pool that separates
    nothing, since the group is what `share_with` opens a directory to (#121)."""
    assert pool_complaint([ME]) is None
    with pytest.raises(AgentError, match="no account named"):
        pool_complaint([ME, "no-such-account-x"])
    pair = _two_accounts_sharing_a_group()
    if pair is None:  # pragma: no cover - every distribution this runs on has one
        pytest.skip("no two accounts on this host share a primary group")
    complaint = pool_complaint(list(pair))
    assert complaint is not None and "share a primary group" in complaint


def test_an_idle_workspace_is_closed_to_every_account(tmp_path: Path) -> None:
    """A workspace outlives its run, and there are fewer accounts than workspaces, so an open
    idle one would eventually sit beside a session running as the same account (#121)."""
    path = tmp_path / "ws"
    path.mkdir()
    share_with(path, ME)
    assert stat.S_IMODE(path.stat().st_mode) == WORKSPACE_DIR_MODE
    seal(path)
    assert stat.S_IMODE(path.stat().st_mode) == SEALED_DIR_MODE
    assert not SEALED_DIR_MODE & (stat.S_IRWXG | stat.S_IRWXO)
    # Never raises: this runs on the way out of a run.
    seal(tmp_path / "gone")


def test_two_writers_do_not_lose_a_binding(tmp_path: Path) -> None:
    """`run-once` is the operator's debugging tool and may run beside a live worker, so the
    read-modify-write is locked rather than racy."""
    first, second = registry(tmp_path), registry(tmp_path)
    assert first.allocate("a") == "agent-1"
    assert second.allocate("b") == "agent-2"
    assert first.bindings() == {"a": "agent-1", "b": "agent-2"}
    assert (tmp_path / ".issuebot" / "accounts.lock").is_file()


def test_a_busy_account_is_one_with_a_workspace_open(tmp_path: Path) -> None:
    """The mode is the one signal another process has that an account is in use: a workspace
    is open exactly while a session is running in it (#121)."""
    pool = registry(tmp_path)
    for key in ("running", "idle"):
        pool.allocate(key)
        (tmp_path / key).mkdir()
    share_with(tmp_path / "running", ME)
    share_with(tmp_path / "idle", ME)
    seal(tmp_path / "idle")
    assert pool.busy_accounts() == {"agent-1"}
    seal(tmp_path / "running")
    assert pool.busy_accounts() == set()


def test_the_lock_serialises_two_writers(tmp_path: Path) -> None:
    """`flock` is per open file description, so two threads in one process contend exactly as
    two processes would -- which is what `run-once` beside a live worker is."""
    pool = registry(tmp_path)
    order: list[str] = []
    started = threading.Event()
    release = threading.Event()
    real_store = pool._store

    def slow_store(bindings: Mapping[str, str]) -> None:
        order.append("first-writes")
        started.set()
        release.wait(5)
        real_store(bindings)

    def first() -> None:
        pool._store = slow_store  # type: ignore[method-assign]
        pool.allocate("a")

    thread = threading.Thread(target=first)
    thread.start()
    assert started.wait(5)
    pool._store = real_store  # type: ignore[method-assign]
    second = threading.Thread(target=lambda: order.append(registry(tmp_path).allocate("b") or ""))
    second.start()
    second.join(0.2)
    assert order == ["first-writes"], "the second writer entered the critical section"
    release.set()
    thread.join(5)
    second.join(5)
    assert registry(tmp_path).bindings() == {"a": "agent-1", "b": "agent-2"}
