"""Per-issue workspaces: keys, containment, clone, hooks, removal and session.json."""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, get_args

from issuebot.agent.errors import AgentError
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
        self._log = get_logger(__name__)

    # --- paths --------------------------------------------------------------------

    def path_for(self, identifier: str) -> Path:
        path = (self.root / workspace_key(identifier)).resolve()
        if not self.is_contained(path):
            raise AgentError("workspace_error", f"workspace path {path} escapes {self.root}")
        return path

    def is_contained(self, path: Path) -> bool:
        resolved = path.resolve()
        return resolved != self.root and resolved.is_relative_to(self.root)

    # --- lifecycle ----------------------------------------------------------------

    async def create_or_reuse(self, issue: Issue) -> Workspace:
        path = self.path_for(issue.identifier)
        if (path / ".git").is_dir() and (path / ".issuebot").is_dir():
            self._log.debug("workspace_reused", workspace=str(path))
            return Workspace(key=path.name, path=path, created=False)
        if path.exists():
            self._log.warning("workspace_remnant_removed", workspace=str(path))
            _remove_path(path, "cannot remove remnant")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AgentError(
                "workspace_error", f"cannot create workspace root {self.root}: {exc}"
            ) from exc
        try:
            await self._clone(path)
            post = await self._run_script("post_clone", POST_CLONE_SCRIPT, path)
            if not post.ok:
                raise AgentError("workspace_error", f"post-clone setup failed: {post.summary}")
            hook = await self.run_hook("after_create", path)
            if hook is not None and not hook.ok:
                raise AgentError("workspace_error", f"after_create hook failed: {hook.summary}")
            # Created last: its presence marks a workspace whose creation completed.
            try:
                (path / ".issuebot").mkdir(exist_ok=True)
            except OSError as exc:
                raise AgentError(
                    "workspace_error", f"cannot create {path / '.issuebot'}: {exc}"
                ) from exc
        except AgentError:
            shutil.rmtree(path, ignore_errors=True)
            raise
        self._log.info("workspace_created", workspace=str(path))
        return Workspace(key=path.name, path=path, created=True)

    async def _clone(self, path: Path) -> None:
        args = ["repo", "clone", self._settings.github.repo, str(path), "--", "--depth", "1"]
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
        await self.run_hook("before_remove", path)
        _remove_path(path, "cannot remove workspace")
        self._log.info("workspace_removed", workspace=str(path))
        return True

    # --- hooks --------------------------------------------------------------------

    async def run_hook(self, name: HookName, workspace: Path) -> HookResult | None:
        script = getattr(self._settings.hooks, name)
        if not script:
            return None
        return await self._run_script(name, script, workspace)

    async def _run_script(self, name: str, script: str, workspace: Path) -> HookResult:
        timeout_s = self._settings.hooks.timeout_ms / 1000
        started = time.monotonic()
        # The later hooks see what `before_run` wrote: `after_run` and `before_remove` tend to
        # want the same DSN. `after_create` runs before any file can exist, which is fine.
        base = agent_environment(self._environ, token=self._settings.github.token)
        env, _ = workspace_environment(base, workspace)
        self._log.debug("hook_started", hook=name, workspace=str(workspace))
        try:
            process = await asyncio.create_subprocess_exec(
                *self.hook_shell,
                script,
                cwd=workspace,
                env=env,
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
            _kill_group(process)
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
            _kill_group(process)
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

    def _output_tail(self, raw: bytes) -> str:
        """The end of a hook's output, scrubbed before the cut so no credential straddles it."""
        return self._scrubber.scrub(raw.decode("utf-8", errors="replace"))[-_OUTPUT_TAIL:]

    # --- session.json ---------------------------------------------------------------

    def read_session(self, workspace: Path) -> SessionRecord | None:
        path = session_path(workspace)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
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
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)


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
