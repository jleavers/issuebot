"""Per-issue workspaces: keys, containment, clone, hooks, removal and session.json."""

import asyncio
import contextlib
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, get_args

from issuebot.agent.accounts import (
    REGISTRY_DIR,
    SEALED_DIR_MODE,
    seal,
    session_account,
    share_with,
)
from issuebot.agent.boundary import SESSION_FILE, Boundary, BoundaryError
from issuebot.agent.errors import AgentError
from issuebot.agent.runas import RunAs, Spawn
from issuebot.agent.runner import agent_environment, workspace_environment
from issuebot.agent.scrub import Scrubber
from issuebot.agent.uvcache import UV_CACHE_ROOT_NAME, ensure_uv_cache_dir
from issuebot.config import Settings
from issuebot.events.types import RunOutcome
from issuebot.github import GhRunner, GhRunnerLike, GitHubError, Issue
from issuebot.log import get_logger
from issuebot.pipes import read_capped

HookName = Literal["after_create", "before_run", "after_run", "before_remove"]
SESSION_FILE_VERSION = 1
POST_CLONE_SCRIPT = (
    "git config --local --add credential.https://github.com.helper '' && "
    "git config --local --add credential.https://github.com.helper '!gh auth git-credential' && "
    "mkdir -p .git/info && printf '.issuebot/\\n' >> .git/info/exclude"
)
# Written last into ``.issuebot`` by the worker: its presence marks a workspace whose creation
# completed, so a clone whose hooks were cut short is recreated rather than reused.
CREATED_MARKER = "created"
STATE_DIR = ".issuebot"
# Under ``agent.run_as`` (#75) the workspace directory and ``.issuebot`` are the worker's, and
# sticky: the agent creates what it likes inside them but can neither unlink nor rename the
# worker's entries, which is what keeps ``session.json`` and ``runs/`` the worker's own. What
# the worker reads back out of them is declared, and guarded, in ``issuebot.agent.boundary``.
# The mode and the group come from ``share_with`` (#121): the bound account's group and nobody
# else's, so a sibling session at another uid cannot enter the directory at all.

# Names directly under ``workspace.root`` that are not workspaces, and must not be treated as
# one. The worker's account registry (#121) and the per-account uv caches (#164): both are the
# worker's own directories beside the workspace keys, and both would be damaged by being taken
# for a clone. ``path_for`` refuses either as a key -- no identifier spells one today, since a
# key is ``<repo>-<number>`` and always ends in a digit, but neither directory is something to
# leave resting on that -- and ``seal_idle`` steps over them, which for the cache root is
# load-bearing rather than tidy: it is ``0755`` so that every session account can reach its own
# directory inside it, and sealing it to ``0700`` at each worker start would take every
# account's cache away until something re-created it.
RESERVED_ROOT_NAMES: frozenset[str] = frozenset({REGISTRY_DIR, UV_CACHE_ROOT_NAME})
_DISALLOWED = re.compile(r"[^A-Za-z0-9._-]")
_HASH_LENGTH = 16
_OUTPUT_TAIL = 2000
# What one hook, or the clone, may write to stdout, and separately to stderr, before its
# process group is killed (#139). ``hooks.timeout_ms`` bounds how long the process may run and
# never how much it may write inside that time, and ``communicate()`` buffered both pipes in
# the worker -- the process that supervises every concurrent session, so a flood there is not
# one session's. The party growing it is the one the deployment invites: ``hooks.after_create``
# is where the *target* repository's dependency install runs, and a ``postinstall`` that
# prints, or a build that warns per file over a large tree, can produce gigabytes inside sixty
# seconds. Much smaller than ``GhRunner``'s 32 MiB, because a hook's output is diagnostic
# rather than a response to parse: only ``_OUTPUT_TAIL`` of either stream survives into
# ``HookResult``, so the cap is what a chatty-but-honest install may print (a verbose
# dependency install or build log is tens of KiB, a pathological one a few MiB) and not what
# issuebot needs to keep.
MAX_HOOK_OUTPUT_BYTES = 4 * 1024 * 1024
_OUTCOMES: frozenset[str] = frozenset(get_args(RunOutcome))


def workspace_key(identifier: str) -> str:
    """Sanitise to ``[A-Za-z0-9._-]``; add a 64-bit hash suffix when that changed anything."""
    key = _DISALLOWED.sub("_", identifier)
    if key != identifier or key in ("", ".", ".."):
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
        key = f"{key}-{digest}"
    return key


def session_path(workspace: Path) -> Path:
    return workspace / ".issuebot" / "session.json"


def run_log_dir(workspace: Path, run_id: str) -> Path:
    return workspace / ".issuebot" / "runs" / run_id


@dataclass(frozen=True, kw_only=True, slots=True)
class Workspace:
    key: str
    path: Path
    created: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class HookResult:
    name: str
    returncode: int | None
    timed_out: bool
    duration_ms: int
    stdout_tail: str
    stderr_tail: str
    # The hook wrote more than ``MAX_HOOK_OUTPUT_BYTES`` to one of its streams, and its process
    # group was killed for it (#139) -- or the kill was tried and refused, which is
    # ``_kill_quietly``'s case. Its own exit status is therefore the kill's, or its own where it
    # exited inside the pipe buffer before the reader caught up, or the timeout's where the kill
    # did not land: which is why the overrun is a fact of its own and not read off
    # ``returncode``.
    overrun: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.overrun

    @property
    def summary(self) -> str:
        if self.overrun:
            # Before the timeout: a kill whose pipes then took the rest of the timeout to close
            # is an overrun, and the cause is what the run's error should quote, not the
            # symptom. It does not claim the kill, which a hook that exited inside the pipe
            # buffer before the reader caught up never received.
            return f"output exceeded {MAX_HOOK_OUTPUT_BYTES} bytes"
        if self.timed_out:
            return f"timed out after {self.duration_ms} ms"
        lines = [line for line in self.stderr_tail.splitlines() if line.strip()]
        detail = f": {lines[-1].strip()}" if lines else ""
        return f"exit status {self.returncode}{detail}"


@dataclass(frozen=True, kw_only=True, slots=True)
class SessionRecord:
    issue_number: int
    issue_identifier: str
    run_id: str
    session_id: str
    attempt: int
    turn_number: int
    last_outcome: RunOutcome | None
    updated_at: datetime
    # The workpad comment the session last resolved for the agent (#77): the account's own
    # marker comment, found by author; ``None`` until one exists. Written by issuebot, so it
    # is a record of what the agent was pointed at, not what a commenter said.
    workpad_comment_id: int | None = None
    version: int = SESSION_FILE_VERSION


class WorkspaceManager:
    """Creates, reuses and removes per-issue clones under ``workspace.root``."""

    def __init__(
        self,
        settings: Settings,
        *,
        gh: GhRunnerLike | None = None,
        environ: Mapping[str, str] | None = None,
        hook_shell: Sequence[str] = ("bash", "-lc"),
    ) -> None:
        self._settings = settings
        self.root = settings.workspace.root.resolve()
        self.hook_shell = tuple(hook_shell)
        self._gh: GhRunnerLike = gh or GhRunner(
            token=settings.github.token, timeout_ms=settings.hooks.timeout_ms
        )
        self._environ = dict(os.environ if environ is None else environ)
        # A hook runs with the token in its environment and prints what it likes on the way
        # out, and its last stderr line becomes the run's error (`HookResult.summary`), which
        # takes the same exits as a failed turn's (#91): so the tails are scrubbed here, where
        # the result is built, before the cut that keeps their end.
        self._scrubber = Scrubber.for_deployment(settings, self._environ)
        # The account the clone, the hooks and the post-clone setup run as (#75), or None.
        # One account: the orchestrator narrows a pool to this workspace's bound member before
        # it builds the manager (#121), so nothing below here has a pool to reason about.
        self._account = session_account(settings)
        self._runas = RunAs(self._account) if self._account else None
        # The worker's side of the line (#104): every read of what the session leaves in a
        # workspace goes through it, and the worker's own state is created through it. Its
        # session uid is the bound account's alone, so under a pool a workspace's boundary
        # names the one member that may have written in it (#121).
        self._boundary = Boundary.current(self._account)
        self._log = get_logger(__name__)

    @property
    def boundary(self) -> Boundary:
        """The worker's side of the line, for a read of a workspace made outside this class."""
        return self._boundary

    # --- paths --------------------------------------------------------------------

    def path_for(self, identifier: str) -> Path:
        path = (self.root / workspace_key(identifier)).resolve()
        if not self.is_contained(path):
            raise AgentError("workspace_error", f"workspace path {path} escapes {self.root}")
        if path.name in RESERVED_ROOT_NAMES:
            # The worker keeps its account bindings and its per-account uv caches there (#121,
            # #164), and a workspace is removed wholesale. No identifier reaches either today
            # -- one is `<repo>-<number>`, so a key always ends in a digit -- but neither is
            # something to leave resting on how identifiers happen to be spelled.
            raise AgentError("workspace_error", f"workspace path {path} is issuebot's own")
        return path

    def is_contained(self, path: Path) -> bool:
        resolved = path.resolve()
        return resolved != self.root and resolved.is_relative_to(self.root)

    # --- lifecycle ----------------------------------------------------------------

    async def create_or_reuse(self, issue: Issue) -> Workspace:
        path = self.path_for(issue.identifier)
        if self._is_complete(path):
            # Re-applied on reuse, not only on creation: the mode and the group are what keep a
            # sibling session out (#121), and a workspace whose bound account changed -- the
            # pool shrank, or the setting did -- would otherwise be one its own session could
            # not enter. Idempotent, and the directories are the worker's either way.
            self._share(path)
            self._share(path / ".issuebot")
            self._log.debug("workspace_reused", workspace=str(path))
            return Workspace(key=path.name, path=path, created=False)
        if path.exists():
            # A remnant is a workspace whose creation did not finish, or one whose binding
            # moved (#121): either way `_remove_tree` opens it to each account that owns
            # something in it, which a sealed or re-bound directory otherwise refuses.
            self._log.warning("workspace_remnant_removed", workspace=str(path))
            await self._remove_tree(path, "cannot remove remnant")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AgentError(
                "workspace_error", f"cannot create workspace root {self.root}: {exc}"
            ) from exc
        try:
            # The directory first, and the worker's: the clone lands inside it, so under
            # agent.run_as it is shared (sticky) rather than the agent's own (#75). Created
            # closed and opened by `_share`, so it is never briefly wider than it ends up.
            path.mkdir(mode=SEALED_DIR_MODE)
            self._share(path)
        except OSError as exc:
            raise AgentError(
                "workspace_error", f"cannot create workspace directory {path}: {exc}"
            ) from exc
        try:
            await self._clone(path)
            # Before any hook: `after_create` may write `.issuebot/env`, and the state the
            # worker keeps here (`runs/`, `session.json`) has to be its own from the start.
            self._make_state_dir(path)
            post = await self._run_script("post_clone", POST_CLONE_SCRIPT, path)
            if not post.ok:
                raise AgentError("workspace_error", f"post-clone setup failed: {post.summary}")
            hook = await self.run_hook("after_create", path)
            if hook is not None and not hook.ok:
                raise AgentError("workspace_error", f"after_create hook failed: {hook.summary}")
            # Written last: its presence marks a workspace whose creation completed.
            try:
                # Exclusive: under agent.run_as `.issuebot` is shared, and a hostile hook that
                # pre-created the sentinel would otherwise leave the worker `utime`-ing an
                # agent-owned file (#75). O_EXCL fails cleanly instead.
                self._boundary.create_marker(path, (STATE_DIR, CREATED_MARKER))
            except OSError as exc:
                raise AgentError("workspace_error", f"cannot mark {path} created: {exc}") from exc
        except AgentError:
            with contextlib.suppress(AgentError):
                await self._remove_tree(path, "cannot remove the failed workspace")
            raise
        self._log.info("workspace_created", workspace=str(path))
        return Workspace(key=path.name, path=path, created=True)

    def _is_complete(self, path: Path) -> bool:
        """A clone whose creation finished, in a workspace whose state is still the worker's.

        The clone is the session's (#75) and only has to be there -- but under a pool it has to
        be *this* workspace's bound account's (#121), since a binding that moved leaves a tree
        the new account cannot write. The state directory, the run directory and the sentinel
        are read back through the boundary (#104), so each is a directory or a regular file the
        worker owns, reached through no symbolic link.
        """
        if not (path / ".git").is_dir():
            return False
        if not (
            self._boundary.is_own_dir(path, (STATE_DIR,))
            and self._boundary.is_own_dir(path, (STATE_DIR, "runs"))
            and self._boundary.is_own_file(path, (STATE_DIR, CREATED_MARKER))
        ):
            return False
        # And the clone has to belong to the account that will work in it (#121). A binding
        # that moved -- the pool shrank, the setting changed -- leaves a tree the new account
        # cannot write, and git would fail every command rather than say so: re-clone instead.
        return _owned_by(path / ".git", self._account)

    def _share(self, path: Path) -> None:
        if self._account is not None:
            share_with(path, self._account)

    def seal(self, path: Path) -> None:
        """Close a workspace the run has finished with (#121).

        A workspace outlives its run -- an issue in ``review`` keeps its clone for days -- and
        there are fewer accounts than workspaces, so an idle one that stayed open would
        eventually sit beside a session running as the same account. Sealed, nothing but the
        worker can traverse into it, and the next dispatch opens it again for its own account.
        Never raises: this runs on the way out of a run.
        """
        if self._account is not None:
            seal(path)

    def seal_idle(self) -> None:
        """Seal every workspace under the root: what a worker does before it claims anything.

        A run's own seal is in its ``finally``, so the only way one stays open is a worker that
        was killed outright. Startup is where that is put right, since nothing is running yet.
        """
        if self._account is None:
            return
        try:
            children = list(self.root.iterdir())
        except OSError:
            return
        for child in children:
            # Through the boundary (#104): a workspace is a directory of the worker's, reached
            # without following a link, so a name a session planted here is not sealed as one.
            if child.name not in RESERVED_ROOT_NAMES and self._boundary.is_own_dir(
                self.root, (child.name,)
            ):
                seal(child)

    def _make_state_dir(self, path: Path) -> None:
        state = path / ".issuebot"
        try:
            # Under agent.run_as the clone is the agent's, so a repository that ships a
            # `.issuebot` entry has put one where the worker's state goes: refuse, rather than
            # keep state in a directory the session owns.
            state.mkdir(mode=SEALED_DIR_MODE, exist_ok=self._runas is None)
            self._share(state)
            (state / "runs").mkdir(exist_ok=self._runas is None)
        except FileExistsError as exc:
            raise AgentError(
                "workspace_error",
                f"{path} already holds {exc.filename}; .issuebot inside a workspace is issuebot's",
            ) from exc
        except OSError as exc:
            raise AgentError("workspace_error", f"cannot create {state}: {exc}") from exc

    async def _clone(self, path: Path) -> None:
        args = ["repo", "clone", self._settings.github.repo, str(path), "--", "--depth", "1"]
        if self._runas is not None:
            # As the agent, so the clone is the agent's to write: gh clones into the empty
            # directory the worker made. `_run_argv` has already reported a failure to run.
            clone = await self._run_argv("clone", ["gh", *args], self.root)
            if not clone.ok:
                raise AgentError("workspace_error", f"clone failed: {clone.summary}")
        else:
            try:
                result = await self._gh.run(args)
            except GitHubError as exc:
                raise AgentError("workspace_error", f"clone failed: {exc.message}") from exc
            if result.returncode != 0:
                lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
                detail = lines[0][:200] if lines else ""
                raise AgentError(
                    "workspace_error", f"clone exited with status {result.returncode}: {detail}"
                )
        if not (path / ".git").is_dir():
            raise AgentError("workspace_error", f"clone produced no repository at {path}")

    async def sweep_agent_home(self) -> None:
        """Clear what a prior or concurrent session may have left in the account's home for
        this one to load: the config under ``~/.claude`` (#101), the shell start-up files a
        login shell sources (#137) and the git and ssh config a tool would take a command from
        (#151). Called immediately before each of this session's turns and
        before every script it runs in a login shell (``_run_script``: the hooks and the
        post-clone setup).

        Which session that is depends on the route (#121): one account is shared by everything
        running in the container, while a pool leaves only the next session bound to this
        member -- so under a pool the sweep before the first thing this run does at that uid is
        the load-bearing one.

        Only under ``agent.run_as``: on the host route the home is the operator's own, so it is
        left untouched -- nothing removes a developer's ``.profile`` -- and the container is the
        boundary regardless. Off the event loop, since it delegates through sudo like the
        removal and the kill.
        """
        if self._runas is None:
            return
        if await asyncio.to_thread(self._runas.sweep_home):
            self._log.debug("claude_home_swept")
        else:
            # The turn still runs: the startup probe proved sudo can become the account, and a
            # sweep that failed once is retried before the next turn. But it is said, at
            # WARNING, since a control that silently never ran is no control.
            self._log.warning("claude_home_sweep_failed", user=self._runas.user)

    async def remove(self, identifier: str) -> bool:
        path = self.path_for(identifier)
        if not path.exists():
            return False
        # Open again: `before_remove` runs as the account and the delegated unlink is its own,
        # and a workspace reaching this is a sealed one nine times in ten (#121).
        with contextlib.suppress(AgentError):
            self._share(path)
        try:
            await self.run_hook("before_remove", path)
            await self._remove_tree(path, "cannot remove workspace")
        finally:
            # A removal that failed leaves the directory on disk, and an open one would be
            # readable by the next session bound to the same account (#121). The caller only
            # logs the failure, so closing it again is this method's job.
            if path.exists():
                self.seal(path)
        self._log.info("workspace_removed", workspace=str(path))
        return True

    # --- hooks --------------------------------------------------------------------

    async def run_hook(self, name: HookName, workspace: Path) -> HookResult | None:
        script = getattr(self._settings.hooks, name)
        if not script:
            return None
        return await self._run_script(name, script, workspace)

    async def _remove_tree(self, path: Path, what: str) -> None:
        """Delete a workspace: the agent's files as the agent (#75), then the worker's own.

        Under a pool the files inside may be a *previous* binding's (#121): turning a pool on
        over a live ``/workspaces``, or narrowing ``agent.run_as``, leaves a clone whose
        directories belong to an account this manager is not. Neither the new account (it owns
        nothing there) nor the worker (it owns the workspace but not the directories in the
        clone) could then
        unlink it, and the issue would fail every attempt on a remnant nothing removes. So the
        delegated remove runs as every account that owns something at the top of the tree as
        well as as this workspace's own. Only the removal is derived from the directory; the
        *binding* never is, and cannot be widened by this: the sudo rule names the pool's group
        and refuses anything else, exactly as it does today. ``remove_tree`` is uid-scoped,
        idempotent and swallows its own failures, so the extra passes cost a ``sudo`` each.

        Each pass needs the directory *open to the account it delegates to*, and a workspace
        arriving here is usually closed to all of them: sealed (``0700``) after its run, or
        after ``seal_idle``, and open to the current binding's group at best, which is the one
        account that owns nothing in a tree a previous binding made. So it is re-shared per
        pass, and sealed again if the worker's own removal then fails, so a tree that stays on
        disk is never left wider than it arrived.
        """
        for account in self._removers(path):
            with contextlib.suppress(AgentError):
                share_with(path, account)
            await asyncio.to_thread(RunAs(account).remove_tree, path)
        try:
            _remove_path(path, what)
        except AgentError:
            self.seal(path)
            raise

    def _removers(self, path: Path) -> list[str]:
        """This workspace's account first, then any other that owns an entry at the top of it.

        Empty on the host route: no account is configured, so any account to delegate to could
        only come from the directory -- and a *binding* read off the directory is the one thing
        this module refuses. (A deployment that turned ``agent.run_as`` off over workspaces an
        account already cloned has a tree the worker cannot remove, exactly as before #121; the
        answer there is to turn it back on, not to guess a uid.)
        """
        if self._account is None:
            return []
        accounts = [self._account]
        owners, unresolved = _top_level_owners(path)
        for owner in owners:
            if owner not in accounts:
                accounts.append(owner)
        if unresolved:
            # Nothing here can remove what a deleted account left: say which uid, so an
            # operator can recreate it or clear the tree, rather than leave the removal to
            # fail with a permission error naming nobody.
            self._log.warning(
                "workspace_owner_unresolved", workspace=str(path), uids=sorted(set(unresolved))
            )
        return accounts

    async def _kill_quietly(self, process: asyncio.subprocess.Process) -> None:
        """``_kill_group`` with the failure logged rather than raised (#139).

        ``os.killpg`` raises ``PermissionError`` for a group at another uid, which is every
        hook's group under ``agent.run_as`` where the delegated kill did not take. All three
        callers are places an exception must not reach: the overrun killer runs as a task,
        whose exception would surface from the shielded await in place of the read's result;
        the timeout branch is building the ``HookResult`` that reports the failure; and the
        cancellation branch is on its way to re-raising. In each the hook has already failed
        and the caller is saying so, so ``Exception`` deliberately rather than ``OSError``:
        what must not happen here is *any* exception, and the one known today is only the one
        known today. ``CancelledError`` is not one, which is what leaves the cancellation
        branch re-raising what it was given. A kill that did not land leaves
        ``hooks.timeout_ms`` to bound what the cap could not, while the reads go on dropping
        the bytes -- so the memory stays bounded whether or not the signal is deliverable.
        """
        try:
            await self._kill_group(process)
        except Exception as exc:
            self._log.warning("hook_kill_failed", pid=process.pid, error=str(exc))

    async def _kill_group(self, process: asyncio.subprocess.Process) -> None:
        # Off the event loop: the delegated kill is a sudo subprocess with its own timeout,
        # and this is the single orchestrator task supervising every concurrent session (#75).
        if self._runas is not None:
            await asyncio.to_thread(self._runas.kill_group, process.pid)
        _kill_group(process)

    async def _run_script(self, name: str, script: str, workspace: Path) -> HookResult:
        # Every script -- the four hooks and the post-clone setup -- runs under
        # ``hook_shell``, ``bash -lc``, a login shell that sources whatever shell start-up
        # files the account's home holds. So the sweep runs here as well as before each turn
        # (#137): this is the session's *first* command at that uid, long before ``_turn_loop``
        # reaches its own sweep, and a ``~/.profile`` the previous session at this account left
        # would otherwise run in it. The one seam, rather than one call per hook, because what
        # matters is the login shell and not which hook opened it; ``_run_argv``'s other caller
        # is the clone, which is ``gh`` as an argv and reads no start-up file.
        await self.sweep_agent_home()
        return await self._run_argv(name, [*self.hook_shell, script], workspace)

    def _hook_environment(self, workspace: Path) -> tuple[dict[str, str], list[str]]:
        """What every hook, and the clone, is run with, and the complaints about the env file.

        Not free of side effects, despite the name: it ensures this session account's uv cache
        directory exists first (#164), because the path it exports has to be one uv can write.

        The later hooks see what ``before_run`` wrote: ``after_run`` and ``before_remove`` tend
        to want the same DSN. ``after_create`` runs before any file can exist, which is fine.

        ``uv_cache`` is ensured here rather than at workspace creation, so that
        ``before_remove`` -- which runs for a workspace this manager never created -- is handed
        the same environment as the rest (#164). ``after_create``, where the target
        repository's ``uv sync`` runs, is the one it is for.
        """
        base = agent_environment(
            self._environ,
            token=self._settings.github.token,
            uv_cache=ensure_uv_cache_dir(self.root, self._account, self._environ),
        )
        return workspace_environment(base, workspace, boundary=self._boundary)

    async def _run_argv(self, name: str, argv: Sequence[str], workspace: Path) -> HookResult:
        timeout_s = self._settings.hooks.timeout_ms / 1000
        started = time.monotonic()
        env, _ = self._hook_environment(workspace)
        self._log.debug("hook_started", hook=name, workspace=str(workspace))
        try:
            with self._prepared(argv, env) as spawn:
                process = await asyncio.create_subprocess_exec(
                    *spawn.argv,
                    cwd=workspace,
                    env=spawn.env,
                    pass_fds=spawn.pass_fds,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
        except OSError as exc:
            result = HookResult(
                name=name,
                returncode=None,
                timed_out=False,
                duration_ms=_elapsed_ms(started),
                stdout_tail="",
                stderr_tail=self._scrubber.scrub(str(exc)),
            )
            self._log.warning("hook_failed", hook=name, error=result.stderr_tail)
            return result
        overrun = asyncio.Event()
        try:
            out, err = await asyncio.wait_for(
                self._read_output(process, overrun), timeout=timeout_s
            )
        except TimeoutError:
            await self._kill_quietly(process)
            await process.wait()
            result = HookResult(
                name=name,
                returncode=None,
                # A hook whose group was killed for flooding and whose pipes then stayed open
                # past the timeout is an overrun, and that is what the run's error quotes.
                timed_out=not overrun.is_set(),
                overrun=overrun.is_set(),
                duration_ms=_elapsed_ms(started),
                stdout_tail="",
                stderr_tail="",
            )
            self._log.warning(
                "hook_failed",
                hook=name,
                timed_out=result.timed_out,
                overrun=result.overrun,
                max_output_bytes=MAX_HOOK_OUTPUT_BYTES if result.overrun else None,
                timeout_ms=self._settings.hooks.timeout_ms,
                duration_ms=result.duration_ms,
            )
            return result
        except BaseException:
            # Quietly here too: this is on its way to re-raising something -- a cancellation,
            # most often -- and a failed kill must not replace it.
            await self._kill_quietly(process)
            with contextlib.suppress(Exception):
                await process.wait()
            raise
        result = HookResult(
            name=name,
            returncode=process.returncode,
            timed_out=False,
            overrun=overrun.is_set(),
            duration_ms=_elapsed_ms(started),
            stdout_tail=self._output_tail(out),
            stderr_tail=self._output_tail(err),
        )
        if result.ok:
            self._log.info(
                "hook_finished",
                hook=name,
                exit_code=result.returncode,
                duration_ms=result.duration_ms,
                stdout=result.stdout_tail,
                stderr=result.stderr_tail,
            )
        else:
            self._log.warning(
                "hook_failed",
                hook=name,
                exit_code=result.returncode,
                duration_ms=result.duration_ms,
                stdout=result.stdout_tail,
                stderr=result.stderr_tail,
                overrun=result.overrun,
                max_output_bytes=MAX_HOOK_OUTPUT_BYTES if result.overrun else None,
            )
        return result

    async def _read_output(
        self, process: asyncio.subprocess.Process, overrun: asyncio.Event
    ) -> tuple[bytes, bytes]:
        """The child's stdout and stderr, each bounded at ``MAX_HOOK_OUTPUT_BYTES`` as the
        bytes arrive rather than after (#139).

        ``communicate()`` buffered both pipes in the worker before anything looked at them,
        which made ``hooks.timeout_ms`` the only bound on a hook that prints -- a timer over
        the step, never a ceiling on the resource. Past the cap ``overrun`` is set and the
        process *group* is killed: the writer is as often a grandchild of the shell as the
        shell itself, and under ``agent.run_as`` it runs at a uid the worker cannot signal,
        so the kill is the delegated one. Both streams are then read to their end and the
        excess dropped, since a full pipe is what would keep the child from exiting.
        """

        async def kill_on_overrun() -> None:
            await overrun.wait()
            await self._kill_quietly(process)

        killer = asyncio.create_task(kill_on_overrun())
        try:
            out, err = await asyncio.gather(
                read_capped(process.stdout, MAX_HOOK_OUTPUT_BYTES, overrun.set),
                read_capped(process.stderr, MAX_HOOK_OUTPUT_BYTES, overrun.set),
            )
        finally:
            if overrun.is_set():
                # Shielded: the kill is what let the streams above end, and dropping it
                # half-done on a cancellation would leave the group behind.
                await asyncio.shield(killer)
            else:
                killer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await killer
        await process.wait()
        return out, err

    def _prepared(
        self, argv: Sequence[str], env: Mapping[str, str]
    ) -> contextlib.AbstractContextManager[Spawn]:
        if self._runas is not None:
            return self._runas.prepared(argv, env)
        return contextlib.nullcontext(Spawn(argv=list(argv), env=dict(env), pass_fds=()))

    def _output_tail(self, raw: bytes) -> str:
        """The end of a hook's output, scrubbed before the cut so no credential straddles it."""
        return self._scrubber.scrub(raw.decode("utf-8", errors="replace"))[-_OUTPUT_TAIL:]

    # --- session.json ---------------------------------------------------------------

    def read_session(self, workspace: Path) -> SessionRecord | None:
        path = session_path(workspace)
        try:
            # Through the boundary (#104): the directory is shared with the agent (#75), and a
            # record the worker did not write -- or a link, a FIFO, a file past any size a
            # record could have -- is the session's word about itself, so the worker resumes
            # on nothing.
            read = self._boundary.read(workspace, (STATE_DIR, "session.json"), SESSION_FILE)
        except FileNotFoundError:
            return None
        except BoundaryError as exc:
            self._log.warning("session_file_untrusted", path=str(path), reason=exc.reason)
            return None
        except OSError as exc:
            self._log.warning("session_file_unreadable", path=str(path), error=str(exc))
            return None
        if read.truncated:
            self._log.warning(
                "session_file_untrusted",
                path=str(path),
                reason=f"larger than {SESSION_FILE.limit} bytes",
            )
            return None
        try:
            data = json.loads(read.data.decode("utf-8"))
        except ValueError as exc:
            self._log.warning("session_file_unreadable", path=str(path), error=str(exc))
            return None
        try:
            return _record_from(data)
        except (KeyError, TypeError, ValueError) as exc:
            self._log.warning("session_file_invalid", path=str(path), error=str(exc))
            return None

    def write_session(self, workspace: Path, record: SessionRecord) -> None:
        path = session_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(record)
        data["updated_at"] = record.updated_at.isoformat()
        # A fresh, exclusively created name rather than a fixed one: the directory is shared
        # with the agent under agent.run_as (#75), and a fixed name is one it could pre-create.
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, prefix="session.", suffix=".tmp", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp.name, path)


def _record_from(data: object) -> SessionRecord:
    if not isinstance(data, dict):
        raise TypeError("session file is not an object")
    if data.get("version") != SESSION_FILE_VERSION:
        raise ValueError(f"unsupported session file version {data.get('version')!r}")
    outcome = data.get("last_outcome")
    if outcome is not None and outcome not in _OUTCOMES:
        raise ValueError(f"unknown last_outcome {outcome!r}")
    return SessionRecord(
        issue_number=_as_int(data["issue_number"]),
        issue_identifier=str(data["issue_identifier"]),
        run_id=str(data["run_id"]),
        session_id=str(data["session_id"]),
        attempt=_as_int(data["attempt"]),
        turn_number=_as_int(data["turn_number"]),
        last_outcome=outcome,
        updated_at=datetime.fromisoformat(str(data["updated_at"])),
        workpad_comment_id=_optional_int(data.get("workpad_comment_id")),
    )


def _optional_int(value: object) -> int | None:
    return None if value is None else _as_int(value)


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected an integer, got {value!r}")
    return value


def _top_level_owners(path: Path) -> tuple[list[str], list[int]]:
    """The accounts owning ``path`` and its direct children, the worker's own uid aside, and
    the uids among them that resolve to no account at all.

    A clone made by another account is ``.git`` and a working tree that account owns, so one
    level is enough to name it; deeper entries cannot belong to a uid this misses, since a
    session can only create files as itself. The answer is a list of extra removal passes,
    never a decision, so an owner that will not resolve is skipped rather than raised on -- but
    it is reported, because it is the one shape of this that nothing can put right. A uid with
    no passwd entry is an account that has been *deleted* (lowering ``ISSUEBOT_AGENT_POOL_SIZE``
    and rebuilding the image does exactly that), and nothing short of root can then unlink what
    it left; the uid is what an operator needs to recreate the account or clear the tree by
    hand, and without it the removal would just fail with a permission error naming nobody.
    """
    me = os.getuid()
    owners: list[str] = []
    unresolved: list[int] = []
    entries = [path]
    with contextlib.suppress(OSError):
        entries.extend(sorted(path.iterdir()))
    for entry in entries:
        try:
            uid = entry.lstat().st_uid
        except OSError:
            continue
        if uid == me:
            continue
        try:
            owners.append(pwd.getpwuid(uid).pw_name)
        except KeyError:
            unresolved.append(uid)
    return owners, unresolved


def _owned_by(path: Path, account: str | None) -> bool:
    """True when ``path`` belongs to ``account``; False when either cannot be resolved."""
    if account is None:
        return True
    try:
        return path.lstat().st_uid == pwd.getpwnam(account).pw_uid
    except OSError, KeyError:
        return False


def _remove_path(path: Path, what: str) -> None:
    """Delete a tree or a plain file; an OSError is a workspace_error unless it is already gone.

    A path that is *already gone* is the state this was asked to reach, so it is
    not an error, exactly as it is not one at ``remove``'s front door (#143). ``_remove_tree``
    delegates a best-effort ``RunAs(account).remove_tree(path)`` per removing account before
    this runs, and the accounts' pass can take the whole tree -- an operator clearing
    ``/workspaces``, a second remover, a ``run-once`` beside a live worker, or any deployment
    whose workspace directory is not the worker's own, where the delegated ``shutil.rmtree``
    can unlink the directory as well as empty it. Failing there would fail the removal that
    asked for it.

    The tolerance is deliberately narrow, and is not ``ignore_errors``: it is ENOENT *and*
    nothing at the path afterwards. An ENOENT raised for an entry inside the tree leaves the
    directory on disk and still fails, as does every other ``OSError`` -- an ``EACCES`` on a
    remnant the worker cannot unlink is the failure this whole path exists to report, and
    swallowing it would leave a session's files behind silently. ``lexists``, not ``exists``:
    the guard is only ever read after something else raced this removal, and the conservative
    reading of whatever is at the name by then -- a dangling symlink included -- is a remnant.
    """
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        if isinstance(exc, FileNotFoundError) and not os.path.lexists(path):
            return
        raise AgentError("workspace_error", f"{what} {path}: {exc}") from exc


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _kill_group(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
