"""The claude -p subprocess boundary: argv, environment, stream-json parsing and timeouts."""

import asyncio
import contextlib
import json
import os
import re
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import SecretStr

from issuebot.agent.errors import AgentErrorCategory
from issuebot.config import Settings
from issuebot.log import get_logger

MIN_CLAUDE_VERSION: tuple[int, int, int] = (2, 1, 259)
STREAM_LINE_LIMIT = 10 * 1024 * 1024
TERMINATE_GRACE_S = 10.0
PASSTHROUGH_NAMES: frozenset[str] = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "TMPDIR", "TERM"}
)
PASSTHROUGH_PREFIXES: tuple[str, ...] = ("ANTHROPIC_", "CLAUDE_", "GIT_AUTHOR_", "GIT_COMMITTER_")
FIXED_ENVIRONMENT: dict[str, str] = {
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "NO_COLOR": "1",
    "GH_PAGER": "cat",
    "DISABLE_AUTOUPDATER": "1",
}
_MESSAGE_LIMIT = 500
_LOGGED_ARG_LENGTH = 120
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

TurnEventKind = Literal[
    "session_started",
    "turn_activity",
    "turn_completed",
    "turn_failed",
    "turn_timeout",
    "process_exit",
]


def agent_environment(environ: Mapping[str, str], *, token: SecretStr | None) -> dict[str, str]:
    """The minimal environment the agent child and every hook see."""
    env = {
        name: value
        for name, value in environ.items()
        if name in PASSTHROUGH_NAMES or name.startswith(PASSTHROUGH_PREFIXES)
    }
    env.update(FIXED_ENVIRONMENT)
    if token is not None:
        env["GH_TOKEN"] = token.get_secret_value()
    return env


def parse_claude_version(text: str | None) -> tuple[int, int, int] | None:
    """``2.1.259 (Claude Code)`` -> ``(2, 1, 259)``; ``None`` when nothing parses."""
    if not text:
        return None
    match = _VERSION.search(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True, slots=True)
class TurnEvent:
    """A runtime event of one turn, reported to the observer and the log, never to the bus."""

    kind: TurnEventKind
    turn_number: int
    at: datetime = field(default_factory=_utcnow)
    session_id: str | None = None
    message_type: str | None = None
    tool_name: str | None = None
    detail: str | None = None


class TurnObserver(Protocol):
    def on_turn_event(self, event: TurnEvent) -> None: ...


@dataclass(frozen=True, kw_only=True, slots=True)
class TurnResult:
    turn_number: int
    session_id: str | None
    model: str | None
    api_key_source: str | None
    exit_code: int | None
    subtype: str | None
    is_error: bool
    num_turns: int
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int
    permission_denials: int
    result_text: str | None
    error_category: AgentErrorCategory | None
    error: str | None
    stdout_path: Path
    stderr_path: Path

    @property
    def ok(self) -> bool:
        return self.error_category is None

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


class TurnRunner(Protocol):
    """What the session needs from a runner; ``ClaudeRunner`` satisfies it, tests stub it."""

    async def run_turn(
        self,
        *,
        prompt: str,
        workspace: Path,
        session_id: str,
        resume: bool,
        turn_number: int,
        log_dir: Path,
        observer: TurnObserver | None = None,
        cancel: asyncio.Event | None = None,
    ) -> TurnResult: ...


class StreamParser:
    """Consumes stream-json lines, remembers init and result, and reports activity."""

    def __init__(self, *, turn_number: int, expected_session_id: str) -> None:
        self.turn_number = turn_number
        self.expected_session_id = expected_session_id
        self.session_id: str | None = None
        self.model: str | None = None
        self.api_key_source: str | None = None
        self.result: dict[str, Any] | None = None
        self.unparseable = 0
        self._log = get_logger(__name__)

    def feed(self, line: str) -> list[TurnEvent]:
        text = line.strip()
        if not text:
            return []
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            message = None
        if not isinstance(message, dict):
            self.unparseable += 1
            self._log.warning(
                "claude_stream_unparseable", turn_number=self.turn_number, length=len(text)
            )
            return [self._activity("unparseable")]
        kind = message.get("type")
        if kind == "system" and message.get("subtype") == "init":
            return [self._init(message)]
        if kind == "assistant":
            return self._assistant(message)
        if kind == "result":
            self.result = message
            return []
        return [self._activity(str(kind) if kind is not None else "unknown")]

    def overrun(self) -> TurnEvent:
        """Account for a line the stream reader dropped because it exceeded the limit."""
        self.unparseable += 1
        return self._activity("unparseable")

    def _init(self, message: dict[str, Any]) -> TurnEvent:
        self.session_id = _string(message.get("session_id"))
        self.model = _string(message.get("model"))
        self.api_key_source = _string(message.get("apiKeySource"))
        if self.session_id != self.expected_session_id:
            self._log.warning(
                "claude_session_id_mismatch",
                expected=self.expected_session_id,
                actual=self.session_id,
            )
        return TurnEvent(
            kind="session_started",
            turn_number=self.turn_number,
            session_id=self.session_id,
            detail=self.model,
        )

    def _assistant(self, message: dict[str, Any]) -> list[TurnEvent]:
        inner = message.get("message")
        content = inner.get("content") if isinstance(inner, dict) else None
        blocks = content if isinstance(content, list) else []
        tools = [
            _string(block.get("name"))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if not tools:
            return [self._activity("assistant")]
        return [self._activity("assistant", tool_name=name) for name in tools]

    def _activity(self, message_type: str, *, tool_name: str | None = None) -> TurnEvent:
        return TurnEvent(
            kind="turn_activity",
            turn_number=self.turn_number,
            session_id=self.session_id,
            message_type=message_type,
            tool_name=tool_name,
        )


def classify_result(
    result: dict[str, Any] | None, exit_code: int | None, stderr_tail: str
) -> tuple[AgentErrorCategory | None, str | None]:
    """Map the final result (or its absence) and the exit code to a failure category."""
    if result is None:
        message = f"claude exited with status {exit_code} before reporting a result"
        return "process_exit", _with_tail(message, stderr_tail)
    subtype = _string(result.get("subtype")) or ""
    is_error = bool(result.get("is_error"))
    text = _result_text(result)
    if subtype == "error_max_budget_usd":
        return "budget_exceeded", text or "claude stopped at the --max-budget-usd cap"
    if is_error or subtype != "success":
        return "turn_failed", _with_tail(subtype or "unknown subtype", text)
    if exit_code != 0:
        message = f"claude reported success but exited with status {exit_code}"
        return "process_exit", _with_tail(message, stderr_tail)
    return None, None


def _with_tail(message: str, tail: str) -> str:
    return f"{message}: {tail}" if tail else message


def _result_text(result: dict[str, Any]) -> str:
    text = _string(result.get("result"))
    if not text:
        errors = result.get("errors")
        if isinstance(errors, list):
            text = "; ".join(str(item) for item in errors)
    return (text or "")[:_MESSAGE_LIMIT]


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


class ClaudeRunner:
    """Builds and runs one ``claude -p`` process per turn."""

    def __init__(self, settings: Settings, *, environ: Mapping[str, str] | None = None) -> None:
        self._claude = settings.claude
        self._token = settings.github.token
        self._root = settings.workspace.root.resolve()
        self._environ = dict(os.environ if environ is None else environ)
        self._timeout_s = settings.claude.turn_timeout_ms / 1000
        self._log = get_logger(__name__)

    def build_argv(self, *, session_id: str, resume: bool) -> list[str]:
        cfg = self._claude
        argv = [
            cfg.command,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            cfg.permission_mode,
            "--permission-prompts",
            "none",
            "--max-budget-usd",
            str(cfg.max_budget_usd),
        ]
        argv += ["--resume", session_id] if resume else ["--session-id", session_id]
        if cfg.model:
            argv += ["--model", cfg.model]
        if cfg.setting_sources:
            argv += ["--setting-sources", ",".join(cfg.setting_sources)]
        if cfg.append_system_prompt:
            argv += ["--append-system-prompt", cfg.append_system_prompt]
        if cfg.allowed_tools:
            argv += ["--allowedTools", *cfg.allowed_tools]
        if cfg.disallowed_tools:
            argv += ["--disallowedTools", *cfg.disallowed_tools]
        return argv

    def child_environment(self) -> dict[str, str]:
        return agent_environment(self._environ, token=self._token)

    async def run_turn(
        self,
        *,
        prompt: str,
        workspace: Path,
        session_id: str,
        resume: bool,
        turn_number: int,
        log_dir: Path,
        observer: TurnObserver | None = None,
        cancel: asyncio.Event | None = None,
    ) -> TurnResult:
        """Run one turn; every failure is reported in the result, only cancellation propagates."""
        stdout_path = log_dir / f"turn-{turn_number}.jsonl"
        stderr_path = log_dir / f"turn-{turn_number}.stderr.log"
        parser = StreamParser(turn_number=turn_number, expected_session_id=session_id)
        emit = _Emitter(observer, self._log)
        started = time.monotonic()

        def finish(
            category: AgentErrorCategory | None, error: str | None, exit_code: int | None
        ) -> TurnResult:
            result = parser.result or {}
            usage = result.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            turn = TurnResult(
                turn_number=turn_number,
                session_id=parser.session_id,
                model=parser.model,
                api_key_source=parser.api_key_source,
                exit_code=exit_code,
                subtype=_string(result.get("subtype")),
                is_error=bool(result.get("is_error")),
                num_turns=_int(result.get("num_turns")),
                input_tokens=_int(usage.get("input_tokens")),
                cache_creation_input_tokens=_int(usage.get("cache_creation_input_tokens")),
                cache_read_input_tokens=_int(usage.get("cache_read_input_tokens")),
                output_tokens=_int(usage.get("output_tokens")),
                cost_usd=_float(result.get("total_cost_usd")),
                duration_ms=_int(result.get("duration_ms")),
                permission_denials=len(result.get("permission_denials") or []),
                result_text=_string(result.get("result")),
                error_category=category,
                error=error,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            self._log.info(
                "claude_turn_finished",
                turn_number=turn_number,
                exit_code=exit_code,
                error_category=category,
                error=error,
                cost_usd=turn.cost_usd,
                input_tokens=turn.total_input_tokens,
                output_tokens=turn.output_tokens,
                num_turns=turn.num_turns,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return turn

        resolved = workspace.resolve()
        inside = resolved != self._root and resolved.is_relative_to(self._root)
        if not (resolved.is_dir() and inside):
            message = f"{workspace} is not a directory inside {self._root}"
            return finish("invalid_workspace_cwd", message, None)
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"turn-{turn_number}.prompt.md").write_text(prompt, encoding="utf-8")
        argv = self.build_argv(session_id=session_id, resume=resume)
        self._log.info(
            "claude_turn_started",
            turn_number=turn_number,
            argv=[arg[:_LOGGED_ARG_LENGTH] for arg in argv],
            workspace=str(resolved),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        category: AgentErrorCategory | None = None
        error: str | None = None
        with stderr_path.open("wb") as stderr_file:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=resolved,
                    env=self.child_environment(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr_file,
                    start_new_session=True,
                    limit=STREAM_LINE_LIMIT,
                )
            except OSError as exc:
                return finish("claude_not_found", f"cannot run {argv[0]!r}: {exc}", None)

            writer = asyncio.create_task(_feed_stdin(process, prompt))
            reader = asyncio.create_task(self._read_stream(process, parser, emit, stdout_path))
            waiters: set[asyncio.Task[Any]] = {reader}
            cancel_waiter = asyncio.create_task(cancel.wait()) if cancel is not None else None
            if cancel_waiter is not None:
                waiters.add(cancel_waiter)
            try:
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if cancel_waiter is not None and cancel_waiter in done:
                    category, error = "cancelled", "cancelled while the turn was running"
                    await self._terminate(process)
                elif reader.result() == "timeout":
                    category = "turn_timeout"
                    error = f"no output for {self._timeout_s:.0f}s"
                    await self._terminate(process)
            except Exception as exc:
                await self._terminate(process)
                category, error = "process_exit", f"turn supervision failed: {exc}"
            except BaseException:
                await self._terminate(process)
                emit(_event("process_exit", parser, detail=str(process.returncode)))
                raise
            finally:
                for task in (writer, reader, cancel_waiter):
                    if task is None:
                        continue
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                    elif not task.cancelled() and task.exception() is not None:
                        self._log.warning(
                            "claude_turn_task_failed",
                            turn_number=turn_number,
                            error=str(task.exception()),
                        )
            exit_code = await process.wait()

        emit(_event("process_exit", parser, detail=str(exit_code)))
        if category is None:
            category, error = classify_result(parser.result, exit_code, _last_line(stderr_path))
        if category is None:
            emit(_event("turn_completed", parser, detail=parser.model))
        elif category == "turn_timeout":
            emit(_event("turn_timeout", parser, detail=error))
        else:
            emit(_event("turn_failed", parser, detail=error))
        return finish(category, error, exit_code)

    async def _read_stream(
        self,
        process: asyncio.subprocess.Process,
        parser: StreamParser,
        emit: _Emitter,
        stdout_path: Path,
    ) -> str:
        """Tee stdout to the log file and feed the parser; "timeout" on silence, else "eof"."""
        stdout = process.stdout
        if stdout is None:
            return "eof"
        with stdout_path.open("ab") as out:
            while True:
                try:
                    raw = await asyncio.wait_for(stdout.readline(), timeout=self._timeout_s)
                except TimeoutError:
                    return "timeout"
                except ValueError:
                    self._log.warning(
                        "claude_stream_line_too_long",
                        turn_number=parser.turn_number,
                        limit=STREAM_LINE_LIMIT,
                    )
                    emit(parser.overrun())
                    continue
                if not raw:
                    return "eof"
                out.write(raw)
                out.flush()
                for event in parser.feed(raw.decode("utf-8", errors="replace")):
                    emit(event)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        """SIGTERM, wait for the grace period, then SIGKILL the whole process group."""
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


class _Emitter:
    """Logs every turn event and hands it to the observer, isolating observer failures."""

    def __init__(self, observer: TurnObserver | None, log: Any) -> None:
        self._observer = observer
        self._log = log

    def __call__(self, event: TurnEvent) -> None:
        self._log.debug(
            "claude_turn_event",
            kind=event.kind,
            turn_number=event.turn_number,
            message_type=event.message_type,
            tool_name=event.tool_name,
            detail=event.detail,
        )
        if self._observer is None:
            return
        try:
            self._observer.on_turn_event(event)
        except Exception:
            self._log.exception("turn_observer_failed", kind=event.kind)


def _event(kind: TurnEventKind, parser: StreamParser, *, detail: str | None) -> TurnEvent:
    return TurnEvent(
        kind=kind, turn_number=parser.turn_number, session_id=parser.session_id, detail=detail
    )


async def _feed_stdin(process: asyncio.subprocess.Process, prompt: str) -> None:
    stdin = process.stdin
    if stdin is None:
        return
    try:
        stdin.write(prompt.encode("utf-8"))
        await stdin.drain()
    except BrokenPipeError, ConnectionResetError:
        return
    finally:
        stdin.close()


def _last_line(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:_MESSAGE_LIMIT] if lines else ""
