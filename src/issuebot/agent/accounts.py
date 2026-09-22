"""Which account a workspace's session runs as, and what that buys (#121).

`agent.run_as` as a single name puts every concurrent session at one uid, which is no
boundary between them: the workspaces are siblings under a traversable root, each one is
that account's to write, and with `agent.max_concurrent_agents` above 1 a hostile issue's
session can edit an honest issue's working tree before it commits. A *pool* -- `agent.run_as`
naming more than one account -- gives each running slot a uid of its own, and this module is
the two halves of that:

- **The binding.** An account belongs to a *workspace*, not to a run, for as long as the
  workspace exists: a reworked issue is dispatched again into the same clone, and a different
  uid could not write it. `AccountRegistry` is the worker's own record of that, a file under
  the workspace root that only the worker can write, never a fact derived from the directory.
  Deriving it would hand the choice to whoever writes the issue the key is built from, which
  is exactly the account a hostile session would want to be given.
- **The wall.** A workspace is open to exactly one account, and only while that account is
  working in it -- a session running, or a removal unlinking what one left. `share_with` makes
  the directory `1770`, owner the worker and group the bound account's own, so the worker keeps
  its sticky state and the bound session works inside; `seal` puts it back to `0700` when the
  run ends, and again if a removal fails partway, so nothing but the worker can even traverse
  into an idle one. The root above it stays `0755`.

  Both halves are needed, because a workspace outlives its run: an issue sitting in `review`
  keeps its clone for days, accounts are fewer than workspaces, and without the seal a hostile
  session would eventually be handed an account that also holds an honest, idle workspace --
  and could rewrite the clone that issue's rework will commit and push. At most one workspace
  per account is open at a time, since dispatch will not claim an issue whose account is busy.

  **The seal covers the clone. Under a hardlinking uv it does not cover `.venv`,** and a
  reader of the paragraph above should not take it to. A mode on a directory bounds the paths
  that lead through it, not the inodes underneath, and since #164 uv hardlinks a package out
  of the per-account cache at `<workspace.root>/.uv-cache/<account>`, a hardlinked `.venv`
  entry *is* that cache's inode. A session working in one workspace can therefore open a file
  in its own account's cache directory -- which it is entitled to enter -- and write through
  it into the venv of every sealed, idle workspace bound to that account that installed the
  same package, for the honest session's next run to import. #176 weighed that and accepted it
  rather than closing it: both sessions are the same account at the same uid and already share
  a home that nothing sweeps a cache out of, so the channel predates the hardlink, and nothing
  in it reaches the clone the honest session commits and pushes -- only what its tests import.
  `uvcache.py` holds the reasoning, the alternatives it was chosen over and what each would
  cost. "Under a hardlinking uv" is that module's own two gates and one setting: a workspace
  whose session has no `uv` on its `PATH`, or whose cache directory could not be made, or
  whose deployment has put `UV_LINK_MODE=copy` back from a hook, has a venv of its own inodes
  and is sealed whole.

The worker must therefore be a member of every session account's group -- POSIX lets the
owner of a file change its group only to one it belongs to -- which the image arranges and
`validate` proves before a worker ever runs.
"""

import contextlib
import fcntl
import json
import os
import pwd
import stat
import tempfile
from collections.abc import Collection, Iterator, Mapping, Sequence
from pathlib import Path

from issuebot.agent.errors import AgentError
from issuebot.config import Settings
from issuebot.log import get_logger

# The worker's own record of the bindings, beside the workspaces rather than inside one: the
# account has to be known before the clone, and a clone needs an empty directory.
REGISTRY_DIR = ".issuebot"
REGISTRY_FILE = "accounts.json"
REGISTRY_LOCK = "accounts.lock"
REGISTRY_VERSION = 1
# Owner the worker (rwx, sticky), group the bound account (rwx), everyone else nothing.
WORKSPACE_DIR_MODE = 0o1770
# What an idle workspace goes back to: the worker alone, so no session can traverse into it
# however its own account is bound. A directory nobody may enter is one whose contents' own
# modes stop mattering, which is why this is a mode change and not a recursive chown.
SEALED_DIR_MODE = 0o0700
# Which environment variables carry a credential `claude` needs no file for (#121). A pool
# refuses to run without one of them: see `credential_complaint`.
ENV_CREDENTIAL_NAMES: tuple[str, ...] = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def session_account(settings: Settings) -> str | None:
    """The one account a session runs as, or ``None`` for the host route.

    Everything below the orchestrator sees a single account: the orchestrator narrows a pool
    to the workspace's bound member with `settings_with_run_as` before building a runner or a
    workspace manager. The first member is the answer for anything that skipped that step, so
    a pool never silently becomes the host route.
    """
    return settings.agent.run_as[0] if settings.agent.run_as else None


def settings_with_run_as(settings: Settings, account: str | None) -> Settings:
    """``settings`` with the session account narrowed to ``account``; the same object when it
    already is that, so the common single-account deployment copies nothing."""
    pool = () if account is None else (account,)
    if settings.agent.run_as == pool:
        return settings
    agent = settings.agent.model_copy(update={"run_as": pool})
    return settings.model_copy(update={"agent": agent})


def credential_complaint(settings: Settings, environ: Mapping[str, str]) -> str | None:
    """Why a session account could not authenticate under this environment, or ``None``.

    A session runs as an account nobody logs into: the container has had no interactive login
    since #142, and a pool never had one, since N accounts are N homes and sharing one OAuth
    login between them is a refresh race nobody has established is safe. So the credential is
    the one with no file to share -- one the deployment puts in the environment, which
    `agent_environment` already passes through to every account. The host route (`run_as`
    unset) is the operator's own account, with its own login, and is unaffected.
    """
    if not settings.agent.run_as:
        return None
    if any(environ.get(name) for name in ENV_CREDENTIAL_NAMES):
        return None
    return (
        "a session account has a home nobody logs into, so its credential comes from the "
        f"environment: set {' or '.join(ENV_CREDENTIAL_NAMES)}"
    )


def account_gid(account: str) -> int:
    """The account's own primary group, which a workspace bound to it is chgrp'd to."""
    try:
        return pwd.getpwnam(account).pw_gid
    except KeyError:
        raise AgentError("workspace_error", f"no account named {account!r}") from None


def group_complaint(account: str) -> str | None:
    """Why this process could not give a workspace to ``account``'s group, or ``None``.

    POSIX lets the owner of a file change its group only to one it belongs to, so the worker
    has to be a member of every session account's group for `share_with` to work at all. A
    pure check, so `validate` reports it before a worker ever claims an issue rather than
    leaving it to the first workspace creation.

    The oracle is `os.getgroups()`, this process's own supplementary groups, because those are
    what the kernel authorises the `chgrp` by -- not `/etc/group`, which would say the
    membership exists while the running process still lacked it. That is also why the
    complaint names a restart: supplementary groups are set when a process is exec'd, so a
    `usermod --append` on the host does not reach a worker already running, however plainly
    `id` in a new shell says otherwise.
    """
    try:
        gid = account_gid(account)
    except AgentError as exc:
        return exc.message
    if os.getuid() == 0 or gid == os.getegid() or gid in os.getgroups():
        return None
    return (
        f"this process is not a member of {account}'s group (gid {gid}), "
        "so it cannot give a workspace to it; add it with usermod --append and restart, "
        "since a process's supplementary groups are set when it starts"
    )


def share_with(path: Path, account: str) -> None:
    """Make ``path`` the worker's sticky directory, enterable by ``account`` and nobody else.

    The group first and the mode second, so between the two calls the directory is narrower
    than it ends up rather than wider -- which holds only because a workspace is created at
    `SEALED_DIR_MODE` and opened from there. A worker that is not a member of the account's
    group cannot do this at all, and the complaint says so rather than leaving the directory
    open.
    """
    gid = account_gid(account)
    try:
        os.chown(path, -1, gid)
        os.chmod(path, WORKSPACE_DIR_MODE)
    except OSError as exc:
        raise AgentError(
            "workspace_error",
            f"cannot give {path} to group {gid} for {account!r} ({exc}); "
            f"the worker must be a member of that account's group",
        ) from exc


def seal(path: Path) -> None:
    """Close ``path`` to every account but the worker's. Never raises: the caller is on its way
    out of a run, and a directory that cannot be sealed is one the worker already cannot read,
    which the next dispatch reports for itself."""
    try:
        os.chmod(path, SEALED_DIR_MODE)
    except OSError as exc:
        get_logger(__name__).warning("workspace_seal_failed", workspace=str(path), error=str(exc))


def pool_complaint(accounts: Sequence[str]) -> str | None:
    """Why this pool would not separate its sessions, or ``None``.

    Two accounts sharing a primary group share every workspace bound to either of them, since
    the group is what `share_with` opens a directory to. `useradd -g agents` is an easy way to
    build exactly that, and it would leave a pool that validates, runs, and walls nothing off.
    """
    gids: dict[int, str] = {}
    for account in accounts:
        gid = account_gid(account)
        first = gids.setdefault(gid, account)
        if first != account:
            return (
                f"{first} and {account} share a primary group (gid {gid}), so each could enter "
                "the other's workspaces: give every session account a group of its own"
            )
    return None


class AccountRegistry:
    """The worker's record of which pool account each workspace belongs to.

    One JSON file under the workspace root, written by the worker alone. Read fresh on every
    call: the bindings are few, a restart must see them, and nothing else writes the file.
    """

    def __init__(self, root: Path, accounts: Sequence[str]) -> None:
        self._root = Path(root)
        self._accounts = tuple(accounts)
        self._log = get_logger(__name__)

    @property
    def accounts(self) -> tuple[str, ...]:
        return self._accounts

    @property
    def path(self) -> Path:
        return self._root / REGISTRY_DIR / REGISTRY_FILE

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the registry's lock for a read-modify-write.

        The worker is normally the only writer, but `run-once` is the operator's debugging tool
        and may be run beside a live worker: without this the two would race and one binding
        would be lost. An advisory lock on a file of its own, so the record itself is never the
        thing being opened for write while another process reads it.
        """
        path = self.path.parent / REGISTRY_LOCK
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise AgentError("workspace_error", f"cannot lock {path}: {exc}") from exc
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError as exc:
            os.close(handle)
            raise AgentError("workspace_error", f"cannot lock {path}: {exc}") from exc
        try:
            yield
        finally:
            os.close(handle)

    def bindings(self) -> dict[str, str]:
        """Every recorded binding whose account is still in the pool."""
        return self._load()

    def bound(self, key: str) -> str | None:
        """The account ``key`` is bound to, without binding one.

        A binding outlives the run that made it: the same workspace answers the same account
        until it is released, which is what lets a rework session write the clone the first
        session made.
        """
        return self._load().get(key)

    def allocate(self, key: str, *, busy: Collection[str] = ()) -> str | None:
        """Bind ``key`` to the least-loaded account no session is running as, and record it.

        ``None`` when every account is busy: the caller leaves the candidate for a later tick
        rather than putting two concurrent sessions back at one uid. An already-bound ``key``
        keeps its account, busy or not -- that is `bound`'s answer, and the caller checks it.
        """
        with self._locked():
            bindings = self._load()
            existing = bindings.get(key)
            if existing is not None:
                return existing
            load = dict.fromkeys(self._accounts, 0)
            for account in bindings.values():
                load[account] += 1
            free = [name for name in self._accounts if name not in busy]
            if not free:
                return None
            chosen = min(free, key=lambda name: (load[name], self._accounts.index(name)))
            bindings[key] = chosen
            self._store(bindings)
        self._log.info("account_bound", workspace_key=key, account=chosen)
        return chosen

    def busy_accounts(self) -> set[str]:
        """The accounts with a workspace open, readable from outside the worker's memory.

        A workspace is open exactly while a session is running in it, so its mode is the one
        cross-process signal there is that an account is in use. `run-once` is the caller that
        needs it -- the operator may run it beside a live worker, and the flock keeps the two
        from losing a binding but says nothing about which account is busy right now.
        """
        return {account for key, account in self._load().items() if _is_open(self._root / key)}

    def prune(self, keep: Collection[str]) -> None:
        """Drop bindings whose workspace is gone, so an out-of-band removal cannot skew the
        load for ever. ``keep`` is the keys with a session running, whose workspace may not
        exist yet: never derived from the directory, only expired by it.
        """
        with self._locked():
            bindings = self._load()
            live = {
                key: account
                for key, account in bindings.items()
                if key in keep or (self._root / key).is_dir()
            }
            if live == bindings:
                return
            self._store(live)
        self._log.info("accounts_pruned", dropped=sorted(set(bindings) - set(live)))

    # --- the file ---------------------------------------------------------------------

    def _load(self) -> dict[str, str]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise AgentError("workspace_error", f"cannot read {self.path}: {exc}") from exc
        try:
            bindings = _bindings_from(json.loads(raw))
        except (TypeError, ValueError) as exc:
            raise AgentError("workspace_error", f"{self.path} is unusable: {exc}") from exc
        # An account that has left the pool takes its bindings with it: the workspaces it held
        # are re-bound on their next dispatch, since nothing else could enter them now.
        return {key: account for key, account in bindings.items() if account in self._accounts}

    def _store(self, bindings: Mapping[str, str]) -> None:
        directory = self.path.parent
        document = {"version": REGISTRY_VERSION, "bindings": dict(sorted(bindings.items()))}
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise AgentError("workspace_error", f"cannot create {directory}: {exc}") from exc
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=directory,
                prefix="accounts.",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as handle:
                tmp = Path(handle.name)
                handle.write(json.dumps(document, indent=2) + "\n")
            tmp.chmod(0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    tmp.unlink()
            raise AgentError("workspace_error", f"cannot write {self.path}: {exc}") from exc


def _is_open(path: Path) -> bool:
    """True when ``path`` is a workspace `share_with` has opened and `seal` has not closed."""
    try:
        return stat.S_IMODE(path.stat().st_mode) == WORKSPACE_DIR_MODE
    except OSError:
        return False


def _bindings_from(document: object) -> dict[str, str]:
    if not isinstance(document, dict):
        raise TypeError("not an object")
    if document.get("version") != REGISTRY_VERSION:
        raise ValueError(f"unsupported version {document.get('version')!r}")
    bindings = document.get("bindings")
    if not isinstance(bindings, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in bindings.items()
    ):
        raise TypeError("bindings is not a mapping of strings")
    return dict(bindings)
