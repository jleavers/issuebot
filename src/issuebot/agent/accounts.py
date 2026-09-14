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
- **The wall.** `share_with` makes a workspace directory `1770`, owner the worker and group
  that account's own, so the worker keeps its sticky state, the bound session works inside,
  and a sibling session's uid cannot so much as enter. The root above it stays `0755`.

The worker must therefore be a member of every session account's group -- POSIX lets the
owner of a file change its group only to one it belongs to -- which the image arranges and
`validate` proves before a worker ever runs.
"""

import contextlib
import json
import os
import pwd
import tempfile
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

from issuebot.agent.errors import AgentError
from issuebot.config import Settings
from issuebot.log import get_logger

# The worker's own record of the bindings, beside the workspaces rather than inside one: the
# account has to be known before the clone, and a clone needs an empty directory.
REGISTRY_DIR = ".issuebot"
REGISTRY_FILE = "accounts.json"
REGISTRY_VERSION = 1
# Owner the worker (rwx, sticky), group the bound account (rwx), everyone else nothing.
WORKSPACE_DIR_MODE = 0o1770
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
    """Why a pool cannot run under this environment, or ``None`` when it can.

    A pool means N accounts and N homes, and `claude` reads its login from `$HOME`. Sharing
    one OAuth login between them -- copied in, or pointed at through `CLAUDE_CONFIG_DIR` --
    makes every account refresh the same credential independently, which nobody has
    established is safe and no session can test. So the pool takes the credential that has no
    file to share: one the deployment puts in the environment, which `agent_environment`
    already passes through to every account. One account keeps its own login and is unaffected.
    """
    if not settings.agent.run_as_pooled:
        return None
    if any(environ.get(name) for name in ENV_CREDENTIAL_NAMES):
        return None
    return (
        "a pool of session accounts needs a credential in the environment, since each account "
        f"has its own home and no login is shared between them: set one of "
        f"{' or '.join(ENV_CREDENTIAL_NAMES)}, or name a single account"
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
    """
    try:
        gid = account_gid(account)
    except AgentError as exc:
        return exc.message
    if os.getuid() == 0 or gid == os.getgid() or gid in os.getgroups():
        return None
    return (
        f"this process is not a member of {account}'s group (gid {gid}), "
        "so it cannot give a workspace to it"
    )


def share_with(path: Path, account: str) -> None:
    """Make ``path`` the worker's sticky directory, enterable by ``account`` and nobody else.

    The group first and the mode second: between the two calls the directory is narrower than
    it ends up, never wider. A worker that is not a member of the account's group cannot do
    this at all, and the complaint says so rather than leaving the directory world-writable.
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

    def release(self, key: str) -> None:
        """Forget ``key``'s binding, which a removed workspace no longer needs."""
        bindings = self._load()
        account = bindings.pop(key, None)
        if account is None:
            return
        self._store(bindings)
        self._log.info("account_released", workspace_key=key, account=account)

    def prune(self, keep: Collection[str]) -> None:
        """Drop bindings whose workspace is gone, so an out-of-band removal cannot skew the
        load for ever. ``keep`` is the keys with a session running, whose workspace may not
        exist yet: never derived from the directory, only expired by it.
        """
        bindings = self._load()
        live = {
            key: account
            for key, account in bindings.items()
            if key in keep or (self._root / key).is_dir()
        }
        if live != bindings:
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
