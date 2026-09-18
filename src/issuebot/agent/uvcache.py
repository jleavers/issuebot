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
pool (#121) exists to prevent. Per account it is no new sharing at all -- it is exactly the
boundary the account's own home already draws, which has held npm's cache since #101 decided
what the home sweep removes and what it deliberately keeps.

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

import os
import shutil
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
    """
    path = uv_cache_dir(root, account)
    if path is None or account is None:
        return None
    if which(UV_COMMAND, path=environ.get("PATH")) is None:
        return None
    log = get_logger(__name__)
    created = False
    try:
        try:
            # ``mode=`` is masked by the umask, so the mode is set outright afterwards -- and
            # only on the directory this call created, so a root an operator narrowed by hand
            # is not re-widened on every turn.
            path.parent.mkdir(mode=CACHE_ROOT_MODE, parents=True)
        except FileExistsError:
            pass
        else:
            os.chmod(path.parent, CACHE_ROOT_MODE)
        try:
            # Created closed and opened by ``share_with``, so it is never briefly wider than it
            # ends up, exactly as a workspace is.
            path.mkdir(mode=SEALED_DIR_MODE)
        except FileExistsError:
            pass
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
