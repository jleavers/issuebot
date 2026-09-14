"""One account per concurrent session (#121): the binding, the wall and the credential rule."""

import json
import os
import pwd
import stat
import sys
from pathlib import Path

import pytest

from issuebot.agent.accounts import (
    WORKSPACE_DIR_MODE,
    AccountRegistry,
    account_gid,
    credential_complaint,
    group_complaint,
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


def test_a_pool_needs_a_credential_in_the_environment_and_one_account_does_not() -> None:
    pooled = settings(run_as=POOL)
    complaint = credential_complaint(pooled, {})
    assert complaint is not None
    assert "CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY" in complaint
    assert credential_complaint(pooled, {"CLAUDE_CODE_OAUTH_TOKEN": "t"}) is None
    assert credential_complaint(pooled, {"ANTHROPIC_API_KEY": "k"}) is None
    assert credential_complaint(pooled, {"CLAUDE_CODE_OAUTH_TOKEN": ""}) is not None
    assert credential_complaint(settings(run_as="agent"), {}) is None
    assert credential_complaint(settings(), {}) is None


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
