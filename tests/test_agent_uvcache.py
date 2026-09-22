"""uv's cache on the workspaces volume, one directory per session account (#164)."""

import json
import os
import pwd
import shutil
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from issuebot.agent import accounts, uvcache
from issuebot.agent.accounts import (
    SEALED_DIR_MODE,
    WORKSPACE_DIR_MODE,
    AccountRegistry,
    account_gid,
)
from issuebot.agent.runner import (
    PASSTHROUGH_NAMES,
    PROTECTED_ENV_NAMES,
    ClaudeRunner,
    agent_environment,
)
from issuebot.agent.uvcache import (
    CACHE_ROOT_MODE,
    UV_CACHE_ENV,
    UV_CACHE_ROOT_NAME,
    ensure_uv_cache_dir,
    uv_cache_dir,
)
from issuebot.agent.workspace import RESERVED_ROOT_NAMES, WorkspaceManager
from issuebot.config import Settings
from issuebot.log import configure_logging

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX accounts and modes")

ME = pwd.getpwuid(os.getuid()).pw_name

# The environment for every test below whose verdict does not rest on a ``uv`` being found:
# the ones that pass a ``which`` stub, where the stub is what answers and this mapping exists
# only because ``ensure_uv_cache_dir`` reads ``PATH`` out of it to hand over, and the two that
# build a ``WorkspaceManager`` for something other than its environment and so never ask.
#
# Named for what it holds rather than for either of those uses, and empty on purpose.
# ``shutil.which`` answers ``None`` for an empty ``PATH`` whatever the host holds, so a test
# that reaches the *real* ``shutil.which`` through this constant fails on every machine rather
# than on whichever machine happens to have no ``uv`` -- which is the accident #197 was about,
# an environment named for a directory the image it was written in did have. A test at that
# altitude takes ``uv_on_path`` instead.
NO_UV_ON_PATH = {"PATH": ""}


def found(_command: str, *, path: str | None = None) -> str | None:
    """A ``shutil.which`` that answers for uv, and records nothing."""
    return "/opt/uv/bin/uv"


def missing(_command: str, *, path: str | None = None) -> str | None:
    return None


@pytest.fixture(scope="session")
def uv_on_path(tmp_path_factory: pytest.TempPathFactory) -> Mapping[str, str]:
    """An environment whose ``PATH`` really does hold a ``uv``, for the tests that reach the
    real ``shutil.which`` (#197).

    ``WorkspaceManager`` and ``ClaudeRunner`` take no ``which`` seam, deliberately: the
    question ``ensure_uv_cache_dir`` asks is about the ``PATH`` the *session* will be handed
    rather than the one this process has, which is the whole point of the seam, and widening
    two constructors to answer it in a test would be the tail wagging the dog. So a test at
    that altitude makes ``which`` answer the way the deployment does -- by putting a ``uv``
    where it will look.

    Nothing executes the file. ``shutil.which`` asks the filesystem two questions, whether the
    name is there and whether it is executable, and this answers both; it is a shell script
    that refuses so that anything which ever does run it says why rather than succeeding
    quietly.

    Outside any test's ``tmp_path``, because in this file a ``tmp_path`` *is* the workspace
    root, and what is or is not a directory beside the workspace keys is precisely what
    ``seal_idle``, ``path_for`` and the registry sweep are asked about further down.
    """
    bin_dir = tmp_path_factory.mktemp("uv-bin")
    uv = bin_dir / uvcache.UV_COMMAND
    uv.write_text("#!/bin/sh\necho 'not uv: a stand-in for shutil.which' >&2\nexit 127\n")
    uv.chmod(0o755)
    environ = {"PATH": str(bin_dir)}
    # The one host dependency left, and it is asked here rather than left to surface four tests
    # later as a missing ``UV_CACHE_DIR``: ``shutil.which`` tests for ``X_OK``, and a basetemp
    # on a ``noexec`` mount answers no however this file is written. That is #197's own fault
    # mode on a different axis, so it fails naming its cause.
    assert shutil.which(uvcache.UV_COMMAND, path=environ["PATH"]) is not None, (
        f"{uv} is not executable to shutil.which; is pytest's basetemp on a noexec mount?"
    )
    return environ


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- where it goes -------------------------------------------------------------------------


def test_the_location_is_derived_from_the_configured_root(tmp_path: Path) -> None:
    """Never ``/workspaces``: the root is a setting, and a deployment whose workspaces are
    elsewhere needs its caches on *that* filesystem or the hardlink cannot happen at all."""
    assert uv_cache_dir(tmp_path, "agent-2") == tmp_path / UV_CACHE_ROOT_NAME / "agent-2"
    other = Path("/srv/issuebot/ws")
    assert uv_cache_dir(other, "agent-2") == other / UV_CACHE_ROOT_NAME / "agent-2"


def test_the_host_route_has_no_session_account_and_so_no_cache(tmp_path: Path) -> None:
    """``agent.run_as`` unset is the operator's own account and their own home. Relocating a
    developer's uv cache into the workspace root would re-download their world once and
    duplicate it on disk, to fix a warning they are not getting."""
    assert uv_cache_dir(tmp_path, None) is None
    assert ensure_uv_cache_dir(tmp_path, None, NO_UV_ON_PATH, which=found) is None
    assert not (tmp_path / UV_CACHE_ROOT_NAME).exists()


def test_an_image_without_the_uv_toolchain_carries_none_of_it(tmp_path: Path) -> None:
    """``ISSUEBOT_UV_VERSION`` empty is the default, and the image then puts no ``uv`` on
    ``PATH``. Asked of the ``PATH`` the *session* will be handed, which is the worker's own,
    since that is what ``agent_environment`` passes through."""
    seen: list[str | None] = []

    def which(command: str, *, path: str | None = None) -> str | None:
        assert command == "uv"
        seen.append(path)
        return None

    assert ensure_uv_cache_dir(tmp_path, "agent-1", {"PATH": "/usr/bin"}, which=which) is None
    assert seen == ["/usr/bin"]
    assert not (tmp_path / UV_CACHE_ROOT_NAME).exists()


# --- how it is made ------------------------------------------------------------------------


def test_the_worker_creates_it_as_it_creates_a_workspace(tmp_path: Path) -> None:
    """The root is the worker's ``0755`` directory, which a session account cannot create in
    unaided, so the worker makes each cache directory the way it makes a workspace: created
    sealed and then handed to the account's own group. ``1770`` -- the worker keeps the
    directory, the account works inside it, nobody else may even enter."""
    path = ensure_uv_cache_dir(tmp_path, ME, NO_UV_ON_PATH, which=found)
    assert path == tmp_path / UV_CACHE_ROOT_NAME / ME
    assert path is not None and path.is_dir()
    assert mode(path) == WORKSPACE_DIR_MODE
    assert path.stat().st_gid == account_gid(ME)
    # The directory above it is the worker's and traversable, exactly like the workspace root:
    # every account has to reach its own directory inside it, and none can read another's.
    assert mode(path.parent) == CACHE_ROOT_MODE
    assert WORKSPACE_DIR_MODE & stat.S_IRWXO == 0


def test_the_mode_survives_a_umask_that_would_have_narrowed_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mkdir(mode=...)`` is masked, and a worker whose umask is 077 would otherwise leave a
    cache root no session account could traverse -- which is the same failure as sealing it."""
    old = os.umask(0o077)
    try:
        path = ensure_uv_cache_dir(tmp_path, ME, NO_UV_ON_PATH, which=found)
    finally:
        os.umask(old)
    assert path is not None
    assert mode(path.parent) == CACHE_ROOT_MODE


def test_it_is_idempotent_and_re_applies_the_sharing(tmp_path: Path) -> None:
    """Called on the way into every turn and every hook, so a cache removed out of band comes
    back and an account whose group moved is re-shared -- ``create_or_reuse``'s rule."""
    first = ensure_uv_cache_dir(tmp_path, ME, NO_UV_ON_PATH, which=found)
    assert first is not None
    (first / "marker").write_text("kept")
    os.chmod(first, SEALED_DIR_MODE)
    second = ensure_uv_cache_dir(tmp_path, ME, NO_UV_ON_PATH, which=found)
    assert second == first
    assert (first / "marker").read_text() == "kept"
    assert mode(first) == WORKSPACE_DIR_MODE


def test_a_cache_that_cannot_be_made_is_a_warning_and_uvs_own_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exporting a path uv cannot write would break every ``uv`` command in the session, where
    an unset variable only costs the hardlink -- which is exactly today's behaviour."""
    configure_logging()
    (tmp_path / UV_CACHE_ROOT_NAME).write_text("not a directory")
    assert ensure_uv_cache_dir(tmp_path, ME, NO_UV_ON_PATH, which=found) is None
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    [warning] = [line for line in lines if line["event"] == "uv_cache_unavailable"]
    assert warning["level"] == "warning"
    assert warning["account"] == ME


@pytest.mark.parametrize("at", ["leaf", "root"])
@pytest.mark.parametrize("plant", ["file", "symlink", "stranger"])
def test_a_name_that_is_not_this_worker_s_directory_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    at: str,
    plant: str,
) -> None:
    """``chmod`` and ``chown`` follow a symbolic link, and so does ``Path.mkdir(exist_ok=True)``,
    whose own test is ``is_dir()``. So an entry either name already holds is checked before
    either mode is applied, or a link planted at one of them would have this function widening
    whatever it points at -- ``1770`` and an account's group at the leaf, and ``0755`` at the
    root, which is the worse of the two, since a sealed idle workspace is exactly such a target.

    Only the worker can write the ``0755`` root, so this is defence in depth of the kind
    ``boundary.py`` applies to every name the worker did not just create, rather than anything a
    session can reach. Both names, and all three of the things that are not a directory of this
    worker's.
    """
    configure_logging()
    root = tmp_path / UV_CACHE_ROOT_NAME
    name = root if at == "root" else root / ME
    if at == "leaf":
        root.mkdir(mode=CACHE_ROOT_MODE)
    victim = tmp_path / "victim"
    victim.mkdir(mode=SEALED_DIR_MODE)
    if plant == "file":
        name.write_text("not a directory")
    elif plant == "symlink":
        name.symlink_to(victim)  # a link *to* a directory is refused with the rest
    else:
        # A directory of somebody else's. A test host has one uid to offer, so what moves is the
        # worker's idea of its own -- which is the argument the production path passes, so this
        # goes through `ensure_uv_cache_dir` like the other two rather than round it.
        name.mkdir(mode=SEALED_DIR_MODE)
        stranger = os.getuid() + 1  # read before the patch, or the lambda would call itself
        monkeypatch.setattr(uvcache.os, "getuid", lambda: stranger)

    assert ensure_uv_cache_dir(tmp_path, ME, NO_UV_ON_PATH, which=found) is None
    # One of these two is the one that bites, depending on the plant: `chmod` and `chown` follow
    # a link, so an unguarded symlink case would show in the *target's* mode and leave the link
    # itself untouched, while an unguarded file or foreign directory shows in its own.
    assert mode(victim) == SEALED_DIR_MODE, "nothing a link pointed at was opened up"
    assert stat.S_IMODE(name.lstat().st_mode) not in (WORKSPACE_DIR_MODE, CACHE_ROOT_MODE), (
        "and the planted entry itself was left as it was found"
    )
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert [line["event"] for line in lines if line["level"] == "warning"] == [
        "uv_cache_unavailable"
    ], "refusal falls into the warning-and-uv's-own-default path, not a silent None"


def test_an_unknown_account_is_refused_rather_than_left_world_readable(tmp_path: Path) -> None:
    """``share_with`` is what closes the directory, so a name with no account behind it must
    leave nothing usable behind: the directory stays at the sealed mode it was created with,
    which is the worker's alone, and no variable is exported."""
    assert ensure_uv_cache_dir(tmp_path, "no-such-account-x", NO_UV_ON_PATH, which=found) is None
    stranded = tmp_path / UV_CACHE_ROOT_NAME / "no-such-account-x"
    assert mode(stranded) == SEALED_DIR_MODE, "created sealed, and never opened to anyone"


def test_each_account_gets_its_own_directory_opened_to_its_own_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared cache is the failure mode, not the goal: it is a directory one session writes
    and the next installs *from*, which is what the account pool (#121) exists to prevent.

    The gid oracle is faked because a test host has one account it may chgrp to, not two -- so
    what is proved here is that each directory is opened to *its own* account's group and to
    nobody else, which with ``pool_complaint``'s rule that no two pool accounts share a primary
    group is the whole of "closed to the others".
    """
    asked: list[str] = []

    def gid(account: str) -> int:
        asked.append(account)
        return account_gid(ME)  # the real one, so the chgrp this test makes is permitted

    monkeypatch.setattr(accounts, "account_gid", gid)
    first = ensure_uv_cache_dir(tmp_path, "agent-1", NO_UV_ON_PATH, which=found)
    second = ensure_uv_cache_dir(tmp_path, "agent-2", NO_UV_ON_PATH, which=found)
    assert first is not None and second is not None
    assert first != second and first.parent == second.parent
    assert asked == ["agent-1", "agent-2"], "each is opened to its own account's group"
    for path in (first, second):
        assert mode(path) == WORKSPACE_DIR_MODE
        # Group, and nothing for everyone else: an account reaches its own directory through
        # the `0755` root above and is refused at every sibling's door.
        assert mode(path) & stat.S_IRWXO == 0


def test_a_cache_root_narrowed_out_from_under_the_accounts_is_put_back(tmp_path: Path) -> None:
    """The failure this one guards against is worse than the one the warning covers: a `0700`
    cache root is still the worker's, so the directory is made, ``share_with`` succeeds and
    ``UV_CACHE_DIR`` is exported -- naming a path no session account can traverse, which fails
    every ``uv`` command in the session rather than costing a hardlink. It does not heal
    itself, so the mode is re-applied on every call rather than trusted."""
    first = ensure_uv_cache_dir(tmp_path, ME, NO_UV_ON_PATH, which=found)
    assert first is not None
    os.chmod(first.parent, 0o0700)
    assert ensure_uv_cache_dir(tmp_path, ME, NO_UV_ON_PATH, which=found) == first
    assert mode(first.parent) == CACHE_ROOT_MODE


# --- how it reaches the session ------------------------------------------------------------


def test_the_variable_reaches_the_session_and_every_hook(tmp_path: Path) -> None:
    """``agent_environment`` is an allow-list and no ``UV_`` name is on it, which is the route
    #164 needed: the cache is per account and derived from ``workspace.root``, so it is the
    worker's to compute and it joins the environment the way ``GH_TOKEN`` does."""
    assert not any(name.startswith("UV_") for name in PASSTHROUGH_NAMES)
    parent = {"PATH": "/usr/bin", "UV_CACHE_DIR": "/somewhere/else"}
    assert UV_CACHE_ENV not in agent_environment(parent, token=None)
    env = agent_environment(parent, token=None, uv_cache=tmp_path / "cache")
    assert env[UV_CACHE_ENV] == str(tmp_path / "cache")


def test_a_hook_can_point_uv_somewhere_else_from_the_workspace_env_file(
    tmp_path: Path, uv_on_path: Mapping[str, str]
) -> None:
    """The override is new for ``UV_CACHE_DIR`` and works differently from ``UV_LINK_MODE``'s
    old one: the worker now *computes* a value into the base environment, so the file has to
    layer **over** it rather than fill a gap. That is ``merge_workspace_env``'s rule for
    anything not on the protected list, and it is what four paragraphs of documentation rest
    on, so it is pinned here rather than inferred.

    Through a real ``uv`` on the ``PATH`` (#197), since the "over rather than under" is only a
    claim at all when the worker computed a value for the file to cover."""
    workspaces = manager(tmp_path, ME, uv_on_path)
    workspace = tmp_path / "example_repo-1"
    (workspace / ".issuebot").mkdir(parents=True)
    (workspace / ".issuebot" / "env").write_text("UV_CACHE_DIR=/tmp/elsewhere\n")
    env, applied = workspaces._hook_environment(workspace)
    assert applied == [UV_CACHE_ENV], "the file layered over the worker's value, not under it"
    assert env[UV_CACHE_ENV] == "/tmp/elsewhere"


def test_a_deployment_can_still_point_uv_somewhere_else(tmp_path: Path) -> None:
    """Neither ``UV_CACHE_DIR`` nor ``UV_LINK_MODE`` is protected, so an ``.issuebot/env``
    written from ``before_run`` is the override -- the same route #161 left for the link mode,
    and the reason the image now states neither."""
    assert UV_CACHE_ENV not in PROTECTED_ENV_NAMES
    assert "UV_LINK_MODE" not in PROTECTED_ENV_NAMES


def manager(root: Path, account: str | None, environ: Mapping[str, str]) -> WorkspaceManager:
    """A manager over ``root``. ``environ`` is explicit because it decides whether this one
    finds a ``uv``: ``_hook_environment`` goes through the real ``shutil.which``, so a caller
    that asserts on ``UV_CACHE_DIR`` passes ``uv_on_path`` and one that does not says so by
    passing ``NO_UV_ON_PATH``."""
    agent: dict[str, object] = {"run_as": account} if account else {}
    settings = Settings.model_validate(
        {"github": {"repo": "example/repo"}, "workspace": {"root": str(root)}, "agent": agent}
    )
    return WorkspaceManager(settings, gh=None, environ=dict(environ))


def test_every_hook_is_handed_the_cache_directory(
    tmp_path: Path, uv_on_path: Mapping[str, str]
) -> None:
    """``after_create`` is where the target repository's ``uv sync`` runs, and it is the first
    hook of every session -- but ``before_remove`` runs for a workspace this manager never
    created, so the directory is ensured where the hook environment is built rather than at
    workspace creation, and every hook gets the same one.

    One of the pair #197 named: ``WorkspaceManager`` takes no ``which`` seam to stub, so the
    ``uv`` this asks about is one ``uv_on_path`` really put there."""
    workspaces = manager(tmp_path, ME, uv_on_path)
    env, applied = workspaces._hook_environment(tmp_path / "example_repo-1")
    assert applied == []
    assert env[UV_CACHE_ENV] == str(tmp_path / UV_CACHE_ROOT_NAME / ME)
    assert (tmp_path / UV_CACHE_ROOT_NAME / ME).is_dir()


def test_every_turn_is_handed_the_same_cache_directory(
    tmp_path: Path, uv_on_path: Mapping[str, str]
) -> None:
    """The turn and the hooks have to agree, or ``uv`` in the session's own Bash tool would
    build a second cache in the account's home and the hardlink would be lost again.

    The other half of the pair above, and the reason both are worth their altitude: a unit test
    over ``ensure_uv_cache_dir`` cannot catch the two call sites disagreeing."""
    settings = Settings.model_validate(
        {
            "github": {"repo": "example/repo"},
            "workspace": {"root": str(tmp_path)},
            "agent": {"run_as": ME},
        }
    )
    runner = ClaudeRunner(settings, environ=dict(uv_on_path))
    assert runner.child_environment()[UV_CACHE_ENV] == str(tmp_path / UV_CACHE_ROOT_NAME / ME)


def test_the_host_routes_turns_and_hooks_carry_no_cache_variable(
    tmp_path: Path, uv_on_path: Mapping[str, str]
) -> None:
    """With a real ``uv`` on the ``PATH``, so that what is proved is the gate this test is
    named for (#197). The two gates are independent and either alone answers ``None``, so an
    environment carrying no ``uv`` would pass this whatever ``agent.run_as`` did."""
    settings = Settings.model_validate(
        {"github": {"repo": "example/repo"}, "workspace": {"root": str(tmp_path)}}
    )
    runner = ClaudeRunner(settings, environ=dict(uv_on_path))
    assert UV_CACHE_ENV not in runner.child_environment()
    env, _ = manager(tmp_path, None, uv_on_path)._hook_environment(tmp_path / "example_repo-1")
    assert UV_CACHE_ENV not in env
    assert not (tmp_path / UV_CACHE_ROOT_NAME).exists()


# --- beside the workspace keys -------------------------------------------------------------


def test_the_cache_root_is_not_a_workspace_key(tmp_path: Path) -> None:
    """A workspace is removed wholesale, so a key that resolved onto the cache root would take
    every account's cache with it. No identifier spells one -- a key is ``<repo>-<number>`` and
    always ends in a digit -- but neither this nor the account registry beside it is something
    to leave resting on that."""
    assert UV_CACHE_ROOT_NAME in RESERVED_ROOT_NAMES
    with pytest.raises(Exception, match="issuebot's own"):
        manager(tmp_path, "agent-1", NO_UV_ON_PATH).path_for(UV_CACHE_ROOT_NAME)


def test_seal_idle_steps_over_the_cache_root(tmp_path: Path) -> None:
    """The load-bearing half of "a non-workspace directory beside the keys upsets nothing".

    ``seal_idle`` runs at every worker start and chmods each worker-owned directory under the
    root to ``0700``, so that a workspace left open by a worker that was killed outright cannot
    be entered by the next session bound to its account. The cache root is worker-owned too,
    and is ``0755`` on purpose: sealed, every session account would lose its cache until
    something re-created the directory.
    """
    workspaces = manager(tmp_path, ME, NO_UV_ON_PATH)
    cache = ensure_uv_cache_dir(tmp_path, ME, NO_UV_ON_PATH, which=found)
    assert cache is not None
    idle = tmp_path / "example_repo-7"
    idle.mkdir(mode=WORKSPACE_DIR_MODE)
    workspaces.seal_idle()
    assert mode(idle) == SEALED_DIR_MODE, "a real workspace is still sealed"
    assert mode(cache.parent) == CACHE_ROOT_MODE
    assert mode(cache) == WORKSPACE_DIR_MODE


def test_prune_does_not_see_the_cache_directory(tmp_path: Path) -> None:
    """``AccountRegistry.prune`` expires a binding by asking whether its workspace is still
    there; it never *lists* the root, so a directory beside the keys is invisible to it. Proved
    rather than assumed: #161 believed it and did not show it."""
    registry = AccountRegistry(tmp_path, ("agent-1", "agent-2"))
    assert registry.allocate("example_repo-1") == "agent-1"
    assert registry.allocate("example_repo-2") == "agent-2"
    (tmp_path / "example_repo-1").mkdir()
    ensure_uv_cache_dir(tmp_path, ME, NO_UV_ON_PATH, which=found)
    registry.prune(keep=())
    assert registry.bindings() == {"example_repo-1": "agent-1"}
    assert (tmp_path / UV_CACHE_ROOT_NAME).is_dir()
    # And it never becomes a binding of its own: the record is written by the worker from issue
    # identifiers, and the sweep only ever expires what is in it.
    assert UV_CACHE_ROOT_NAME not in registry.bindings()


def test_busy_accounts_does_not_read_the_cache_directory_as_an_open_workspace(
    tmp_path: Path,
) -> None:
    """``busy_accounts`` reads a *mode* -- ``1770`` is a workspace with a session in it -- and
    the cache directories carry that mode for ever by design. It is keyed by the registry's
    own keys, so the cache is never one of the paths it stats; if it were, every account would
    read as permanently busy and dispatch would stop."""
    registry = AccountRegistry(tmp_path, (ME,))
    assert registry.allocate("example_repo-1") == ME
    ensure_uv_cache_dir(tmp_path, ME, NO_UV_ON_PATH, which=found)
    assert registry.busy_accounts() == set()


# --- the residual #176 accepted ------------------------------------------------------------


def test_a_hardlinked_venv_entry_reaches_past_the_seal(tmp_path: Path) -> None:
    """The residual #176 decided to accept, as a fact rather than an assumption.

    ``seal`` puts an idle workspace back to ``0700`` so that the next session bound to its
    account cannot reach the clone inside it. A mode on a directory bounds the paths that lead
    through it and not the inodes underneath, so a ``.venv`` entry uv hardlinked out of the
    per-account cache is still reachable -- by its other name, in a directory that session is
    entitled to enter -- and a write through that name lands in the sealed workspace.

    Written and read as one account throughout, which is what the real pair of sessions is:
    the residual is inode aliasing and not a mode being bypassed, so the sealed path is read
    back here only to show which bytes the alias put there. That sameness of uid is half of
    why the residual was accepted. The assertion is deliberately the *weakness*, so that a
    future per-workspace cache has to come back here and delete it rather than leave a
    docstring claiming the seal covers the venv.
    """
    cache = ensure_uv_cache_dir(tmp_path, ME, HAS_UV, which=found)
    assert cache is not None
    cached = cache / "pkg.py"
    cached.write_text("honest\n")

    idle = tmp_path / "example_repo-7"
    (idle / ".venv").mkdir(parents=True)
    installed = idle / ".venv" / "pkg.py"
    os.link(cached, installed)  # what ``uv sync`` does with the cache on this filesystem
    accounts.seal(idle)
    assert mode(idle) == SEALED_DIR_MODE, "the workspace is sealed, as an idle one always is"

    cached.write_text("poisoned\n")

    assert installed.stat().st_ino == cached.stat().st_ino, "one inode, two names"
    assert installed.read_text() == "poisoned\n", "the seal does not cover .venv"


def test_a_copied_venv_entry_and_the_clone_do_not(tmp_path: Path) -> None:
    """What the trade actually was, as the contrast that names it.

    Under ``UV_LINK_MODE=copy`` -- #161's shape, which this module exists to undo, and which
    a deployment can still put back from a hook -- a venv entry is that workspace's own inode,
    so the sealed directory is again the only path to it, as it is the only path to the clone.
    That is the property #164 spent and #176 decided not to buy back, and its absence is also
    why the residual stops where it does: the clone is the session's own files, written into
    the workspace, and the cache holds no second name for any of them.

    The mode is asserted so that this stays a statement about a *sealed* workspace, but what
    the writes prove is the inode arithmetic either side of it -- which is the whole of the
    difference between the two link modes.
    """
    cache = ensure_uv_cache_dir(tmp_path, ME, HAS_UV, which=found)
    assert cache is not None
    cached = cache / "pkg.py"
    cached.write_text("honest\n")

    idle = tmp_path / "example_repo-7"
    (idle / ".venv").mkdir(parents=True)
    copied = idle / ".venv" / "pkg.py"
    shutil.copy(cached, copied)  # what ``uv sync --link-mode=copy`` does instead
    tracked = idle / "repo" / "module.py"
    tracked.parent.mkdir()
    tracked.write_text("honest\n")
    accounts.seal(idle)
    assert mode(idle) == SEALED_DIR_MODE, "the workspace is sealed, as an idle one always is"

    cached.write_text("poisoned\n")

    assert copied.stat().st_ino != cached.stat().st_ino, "its own inode, not the cache's"
    assert copied.stat().st_nlink == 1, "one name, and it leads through the sealed directory"
    assert copied.read_text() == "honest\n", "so the write through the cache did not reach it"
    assert tracked.stat().st_nlink == 1, "as the clone has, which the cache never names"
