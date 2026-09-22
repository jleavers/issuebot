"""Where uv keeps its cache when the session runs as a session account (#164).

uv would rather hardlink a package out of its cache into the venv it is building, and a
hardlink cannot cross a filesystem. On a deployment shaped like this one the two are never on
the same filesystem by default: the cache is ``$HOME/.cache/uv`` and a session account's home
is in the container's own writable layer, while the venv is ``<workspace>/.venv`` on the
mounted ``workspaces`` volume. #161 measured what that costs -- a full copy of the venv per
workspace, and a cache discarded with the container, so the next session after any
``docker compose up -d worker`` re-downloads from PyPI -- and settled for saying the copy was
intended (``UV_LINK_MODE=copy``). This module is the other half: the cache moves onto the
volume, beside the venvs, where the hardlink works and the cache outlives the container.

**One directory per session account, and that is the whole difficulty.** A cache is a
directory one process writes and the next installs *from*: a package uv hardlinks out of it
lands in the venv the next session's tests import. Shared between session accounts it would be
a surface one session could write for another to execute, which is precisely what the account
pool (#121) exists to prevent. Per account it is the boundary the account's own home already
draws, which has held npm's cache and uv's own for as long as there has been one: the home
sweep (#101, #137) is a denylist of instruction surfaces and shell start-up files, and names no
cache.

**What the hardlink does change, since it is not nothing.** A hardlinked ``.venv`` entry *is*
the cache's inode, so two workspaces bound to one account now share the files their venvs were
installed from. The seal (``accounts.py``) puts an idle workspace back to ``0700`` exactly
because accounts are fewer than workspaces and a hostile session would otherwise be handed an
account that also holds an honest, idle one; a hardlink reaches past that seal into the
honest workspace's ``.venv``, which the seal used to cover. Three things bound it, and they
are why this is the shape #164 asked for rather than an argument against it. The two sessions
are the *same account* at the *same uid*, which already shares a home, and that home already
holds a per-account uv cache the home sweep does not touch -- so the channel is one uv's own
default location had too, and what the hardlink adds is that a poisoning takes effect without
waiting for the honest workspace to sync again. The clone is untouched, so nothing reaches
what the honest session commits and pushes; only what its tests import. And the alternative --
keeping ``UV_LINK_MODE=copy`` and taking the persistence half alone -- gives up the venv
sharing this was measured for. A per-*workspace* cache would close it and would also give up
the sharing, since the second workspace's venv is free only because it is the first one's
files.

**#176 weighed that residual and accepted it, and this is the record.** Nothing was landed to
close it: what is written above is the whole of what bounds it, and the reason is that the
channel is not a new one while every way of closing it pays with what #164 was asked to
deliver. The options, and why each was not taken -- the figures below are arithmetic on #164's
two measurements (78 MB of cache, 77 MB for two hardlinked venvs against 152 MB copied) and
not measurements of their own:

- *A cache per workspace, or per ``(account, workspace)``.* Correct, and it closes the channel
  outright, since no other workspace's venv shares an inode with what this session may write.
  It has to stay on the volume -- any cache a hardlink works from does -- so it outlives the
  container as today's does, for as long as its workspace does. What it gives up is not disk:
  the duplication moves from the venv to the cache, two caches of about 78 MB where there was
  one, which for two workspaces is roughly what the shared cache and its hardlinked venvs cost
  together. What it gives up is the *sharing*, and so a download: a fresh sync from PyPI for
  every new workspace, where an account's cache is a fresh sync for every new account and
  nothing at all for a reworked issue.
- *A cache the session cannot write*, populated by the worker and handed over read-only. It
  rests on uv never writing to its own cache, which uv's cache semantics do not promise, and
  the failure is total rather than graceful: a ``uv sync`` needing one package the worker did
  not pre-install fails the hook instead of falling back.
- *Sweeping the account's cache between sessions.* The only option that needs nothing new from
  uv, and it does close the channel -- a session starting from an empty cache holds no name
  for the inodes an idle workspace's venv already points at, which survive only because that
  venv still links them. But a session is dispatched to one workspace at a time, so a cache
  kept "within one session's workspaces" is shared with nothing: it gives up both halves of
  #164, the cross-workspace hardlink and the cache that outlives the container, and buys a
  PyPI sync per session for them.
- *``UV_LINK_MODE=copy`` over the cache exactly as it is.* Not one of the options #176 listed,
  and the cheapest way to get back the one property this issue is about: a copied ``.venv``
  entry is the workspace's own inode again, so the seal covers it as it did before #164, while
  the cache keeps its place on the volume and the persistence half is untouched. It costs the
  measured half -- 152 MB for two venvs rather than 77, and the 122 ms per sync #164 timed --
  and it closes less than the two above: the cache is still shared between sessions at that
  uid, so a session can still write what the *next* one installs from, which is the pre-#164
  level and the level accepted below. It is not the default because that trade is the one #164
  was asked to make, and it needs no change here either way: ``uv sync --link-mode=copy`` in
  the hook line, or ``UV_LINK_MODE=copy`` in an ``.issuebot/env`` written from ``before_run``.
- *Accept*, which is what was chosen. The two sessions are the same account at the same uid,
  and that account's home is already a surface the home sweep names no cache in -- npm's sits
  there, and uv's own default did until this module moved it -- so a session that wants to
  poison what the next session at that uid installs has had a channel since before #164; what
  the hardlink adds is that it takes effect without waiting for a re-sync. And it reaches what
  the honest session's tests *import*, never the clone it commits and pushes, so the worst of
  it is a broken change made to look green -- in front of the human review every issuebot pull
  request ends at, which is the backstop for everything else a session at that uid could have
  done to its own tree.

What this decision is not is a claim that the seal still covers the venv. Under a hardlinking
uv it does not, and ``accounts.py``'s account-pool docstring now says so, so that the next
reader of the seal is not told something that stopped being true in #164. A deployment whose
threat model differs -- a pool whose accounts are handed issues from genuinely untrusted
authors, say -- has the fourth option above for the seal alone and the first for the channel
entire, at the prices stated there.

The root is the worker's (``/workspaces``, ``0755``, ``issuebot:issuebot``), so a session
account cannot create a directory in it unaided. The worker therefore makes each one the way
it makes a workspace: created sealed and then handed to the account's own group by
``share_with``, ``1770`` with the worker as owner. Unlike a workspace it is never sealed
again -- a workspace is sealed when its run ends because it outlives the run and would
otherwise be readable by the next session bound to that account, and here that next session is
the very thing the cache is kept for.

Two gates, and a deployment that fails either carries none of this:

- a session account (``agent.run_as``). On the host route the session is the worker and the
  home is the operator's own, so uv's own default is left exactly where it is: relocating a
  developer's cache into the workspace root would re-download their world once and duplicate
  it on disk, to fix a warning they are not getting.
- ``uv`` on the ``PATH`` the session will be handed, which is the worker's own
  (``agent_environment`` passes ``PATH`` through). That is the ``ISSUEBOT_UV_VERSION`` opt-in
  as the session sees it: the image puts ``/opt/uv/bin`` on ``PATH`` inside that build
  argument's guard and nowhere else.
"""

import errno
import os
import shutil
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

from issuebot.agent.accounts import SEALED_DIR_MODE, share_with
from issuebot.agent.errors import AgentError
from issuebot.log import get_logger

# The directory under ``workspace.root`` that holds them, one per account inside it. Beside the
# workspace keys rather than inside a workspace, because the cache belongs to the account and
# outlives every workspace bound to it -- which is the point: it is what survives a workspace
# removal, and what survives the container. Dotted and reserved, so no workspace key can be it
# (``workspace.py``'s ``RESERVED_ROOT_NAMES``).
UV_CACHE_ROOT_NAME = ".uv-cache"
# The worker's, and traversable by every account, since each has to reach its own directory
# inside it: the same mode the workspace root above it carries, and for the same reason. What
# it does *not* grant is reading anyone's cache -- each account's own directory is ``1770``,
# open to that account's group and nobody else's.
CACHE_ROOT_MODE = 0o0755
# The name uv reads its cache location from, and what the worker puts in the session's and
# every hook's environment. Not in ``PASSTHROUGH_NAMES`` -- nothing the worker inherits should
# decide this -- and deliberately not in ``PROTECTED_ENV_NAMES`` either: a deployment that
# wants uv's cache somewhere else says so from a hook's ``.issuebot/env``, exactly as it can
# for ``UV_LINK_MODE``, and a session that re-points its own cache has re-pointed its own cache.
UV_CACHE_ENV = "UV_CACHE_DIR"
UV_COMMAND = "uv"

# ``shutil.which``'s shape, as a seam a test can substitute.
Which = Callable[..., str | None]


def uv_cache_dir(root: Path, account: str | None) -> Path | None:
    """Where ``account``'s cache goes under ``root``, or ``None`` on the host route.

    Pure, and derived from ``workspace.root`` rather than from ``/workspaces``: the setting is
    the deployment's, and a worker whose workspaces are somewhere else must keep its caches on
    that filesystem or the hardlink this exists for cannot happen.
    """
    if account is None:
        return None
    return root / UV_CACHE_ROOT_NAME / account


def _require_own_directory(path: Path, uid: int) -> None:
    """Refuse anything at ``path`` that is not a real directory owned by ``uid``.

    Both of the modes this module applies go on with ``chmod``, and ``chmod`` and ``chown``
    follow a symbolic link -- as does ``Path.mkdir(exist_ok=True)``, whose own test is
    ``is_dir()``. So an entry the worker did not just create is checked before either is
    applied, or a link planted at one of these two names would have this function widening
    whatever it points at: ``1770`` and an account's group at the leaf, and ``0755`` at the
    root, which is the worse of the two -- a sealed idle workspace is exactly such a target.

    ``lstat``, so a link *to* a directory is refused with a link to anything else, and the
    owner, so a directory somebody else left here is refused too. The same pair of questions
    ``boundary.py`` asks, for the same reason -- though not with its rigour: that one asks them
    of a descriptor opened ``O_NOFOLLOW|O_DIRECTORY`` and keeps it, where this asks them of a
    path and then re-resolves it, leaving a window between the check and the ``chmod``. The
    window is unreachable, since only the worker can write the ``0755`` root, which is also why
    the whole check is defence in depth rather than a session's reach -- and it costs one
    ``lstat``.
    """
    entry = os.lstat(path)
    if not stat.S_ISDIR(entry.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(path)) from None
    if entry.st_uid != uid:
        raise PermissionError(
            errno.EPERM, f"owned by uid {entry.st_uid}, not this worker's {uid}", str(path)
        ) from None


def ensure_uv_cache_dir(
    root: Path,
    account: str | None,
    environ: Mapping[str, str],
    *,
    which: Which = shutil.which,
) -> Path | None:
    """``account``'s cache directory, created if it is not there, or ``None``.

    ``None`` for the host route, for an image carrying no uv, and for a directory that cannot
    be made -- the last of those logged at WARNING and then left alone, because the fallback is
    uv's own default and that is exactly today's behaviour. Exporting a path uv cannot write
    would be worse than not exporting one: every ``uv`` command in the session would fail,
    where an unset variable only costs the hardlink.

    Idempotent, and called on the way into every turn and every hook rather than once, so that
    a cache removed out of band comes back and an account whose group moved is re-shared. That
    is ``create_or_reuse``'s rule for a workspace directory, for the same reason.

    A permanent failure here -- no such account, the worker not in its group -- is one
    ``probe_run_as`` and ``credential_complaint`` already refuse to start a worker on, so the
    warning it would otherwise repeat per hook and per turn is mostly a state this deployment is
    not supposed to reach. The one exception is a cache directory left by a *different* uid,
    which the volume can outlive an image rebuild holding: that warns on every turn and every
    hook until an operator clears it, and says which uid owns it, which is the answer.

    Synchronous, on the event loop, unlike ``sweep_agent_home``: a ``PATH`` scan, half a dozen
    metadata syscalls on a path this process just resolved, and the ``getpwnam`` inside
    ``share_with`` -- which is the only one of them that can block for a noticeable time, on a
    deployment whose accounts come from a directory service -- beside the env-file read both
    call sites already make here. ``sweep_agent_home`` goes through a thread because it is a
    ``sudo`` to another uid, which is a different order of thing.
    """
    path = uv_cache_dir(root, account)
    if path is None or account is None:
        return None
    # The empty string rather than ``None``: ``shutil.which(path=None)`` falls back to this
    # *process's* ``PATH``, and the question is about the one the session will be handed.
    if which(UV_COMMAND, path=environ.get("PATH", "")) is None:
        return None
    log = get_logger(__name__)
    worker = os.getuid()
    created = False
    try:
        try:
            # ``parents=True`` only ever finds ``workspace.root``: ``create_or_reuse`` makes it
            # before the clone, and nothing calls this before a workspace has been acquired.
            # ``exist_ok`` is *not* passed, so an entry already at the name goes through
            # ``_require_own_directory`` rather than through ``mkdir``'s own ``is_dir()``, which
            # follows a symbolic link and would admit one here.
            path.parent.mkdir(mode=CACHE_ROOT_MODE, parents=True)
        except FileExistsError:
            _require_own_directory(path.parent, worker)
        # Unconditionally, and not only on the directory this call created: ``mode=`` is masked
        # by the umask, and a cache root that ended up narrower than this is one no session
        # account can traverse -- so ``UV_CACHE_DIR`` would name a directory uv cannot reach and
        # every ``uv`` command in the session would fail with the variable set. That is worse
        # than the failure this function does catch, and it does not heal itself, so the mode is
        # re-applied rather than trusted. A root an operator narrowed by hand is not a
        # configuration: it is this directory, made for this, and the accounts have to enter it.
        # Guarded by the check above, since this is the wider of the two modes this function
        # applies and a link here would be the worse of the two to follow.
        os.chmod(path.parent, CACHE_ROOT_MODE)
        try:
            # Created closed and opened by ``share_with``, so it is never briefly wider than it
            # ends up, exactly as a workspace is.
            path.mkdir(mode=SEALED_DIR_MODE)
        except FileExistsError:
            _require_own_directory(path, worker)
        else:
            created = True
        share_with(path, account)
    except (OSError, AgentError) as exc:
        log.warning(
            "uv_cache_unavailable",
            path=str(path),
            account=account,
            error=exc.message if isinstance(exc, AgentError) else str(exc),
        )
        return None
    if created:
        log.info("uv_cache_created", path=str(path), account=account)
    return path
