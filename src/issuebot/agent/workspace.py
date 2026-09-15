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
from issuebot.config import Settings
from issuebot.events.types import RunOutcome
from issuebot.github import GhRunner, GhRunnerLike, GitHubError, Issue
from issuebot.log import get_logger

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
_DISALLOWED = re.compile(r"[^A-Za-z0-9._-]")
_HASH_LENGTH = 16
_OUTPUT_TAIL = 2000
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

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def summary(self) -> str:
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

    # --- paths --------------------------------------------------------------------

    def path_for(self, identifier: str) -> Path:
        path = (self.root / workspace_key(identifier)).resolve()
        if not self.is_contained(path):
            raise AgentError("workspace_error", f"workspace path {path} escapes {self.root}")
        if path.name == REGISTRY_DIR:
            # The worker keeps its account bindings there (#121), and a workspace is
            # removed wholesale. No identifier reaches it today -- one is `<repo>-<number>`,
            # so a key always ends in a digit -- but the record is not something to leave
            # resting on how identifiers happen to be spelled.
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
            if child.name != REGISTRY_DIR and self._boundary.is_own_dir(self.root, (child.name,)):
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

        Empty on the host route, where there is no delegation and the files are the worker's.
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

    async def _kill_group(self, process: asyncio.subprocess.Process) -> None:
        # Off the event loop: the delegated kill is a sudo subprocess with its own timeout,
        # and this is the single orchestrator task supervising every concurrent session (#75).
        if self._runas is not None:
            await asyncio.to_thread(self._runas.kill_group, process.pid)
        _kill_group(process)

    async def _run_script(self, name: str, script: str, workspace: Path) -> HookResult:
        return await self._run_argv(name, [*self.hook_shell, script], workspace)

    async def _run_argv(self, name: str, argv: Sequence[str], workspace: Path) -> HookResult:
        timeout_s = self._settings.hooks.timeout_ms / 1000
        started = time.monotonic()
        # The later hooks see what `before_run` wrote: `after_run` and `before_remove` tend to
        # want the same DSN. `after_create` runs before any file can exist, which is fine.
        base = agent_environment(self._environ, token=self._settings.github.token)
        env, _ = workspace_environment(base, workspace, boundary=self._boundary)
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
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        except TimeoutError:
            await self._kill_group(process)
            await process.wait()
            result = HookResult(
                name=name,
                returncode=None,
                timed_out=True,
                duration_ms=_elapsed_ms(started),
                stdout_tail="",
                stderr_tail="",
            )
            self._log.warning(
                "hook_failed",
                hook=name,
                timed_out=True,
                timeout_ms=self._settings.hooks.timeout_ms,
                duration_ms=result.duration_ms,
            )
            return result
        except BaseException:
            await self._kill_group(process)
            with contextlib.suppress(Exception):
                await process.wait()
            raise
        result = HookResult(
            name=name,
            returncode=process.returncode,
            timed_out=False,
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
            )
        return result

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
    """Delete a directory tree or a plain file; every OSError becomes a workspace_error."""
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        raise AgentError("workspace_error", f"{what} {path}: {exc}") from exc


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _kill_group(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
