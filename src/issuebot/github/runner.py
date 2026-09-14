"""The only place that spawns the gh CLI."""

import asyncio
import contextlib
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import SecretStr

from issuebot.github.errors import GitHubError
from issuebot.log import get_logger

_FIXED_ENVIRONMENT = {
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "NO_COLOR": "1",
    "GH_PAGER": "cat",
}
_LOGGED_ARG_LENGTH = 120

# What one ``gh`` invocation may write to stdout, and separately to stderr, before it is
# killed (#110). The bound on a response's size lives here, at the one seam every read
# crosses, rather than at the callers: a page of issues, a pull request's fields, an issue's
# comments are all GitHub-hosted text that any account can grow, and ``request_timeout_ms``
# bounds only how long the process may run, not how much it may hand back inside that time.
# Sized in bytes, since that is what the pipe carries: GitHub's ceiling on a comment or an
# issue body is 65,536 *characters*, which is 256 KiB of UTF-8 at four bytes a character, so
# the largest legitimate read (a page of a hundred of them, with their user objects) is about
# 26 MiB, and the cap is the power of two above it. No request issuebot makes can reach it,
# whatever the text is made of.
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
_READ_CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class GhResult:
    returncode: int
    stdout: str
    stderr: str


class GhRunnerLike(Protocol):
    async def run(self, args: Sequence[str], *, stdin: str | None = None) -> GhResult: ...


class GhRunner:
    """Runs ``gh`` as an asyncio subprocess with a controlled environment, a wall-clock
    timeout for the process and a size cap for what it writes back."""

    def __init__(
        self,
        *,
        command: str = "gh",
        token: SecretStr | None = None,
        timeout_ms: int = 30_000,
        environ: Mapping[str, str] | None = None,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> None:
        self._command = command
        self._token = token
        self._timeout_s = timeout_ms / 1000
        self._environ = dict(os.environ if environ is None else environ)
        self._max_output_bytes = max_output_bytes
        self._log = get_logger(__name__)

    def child_environment(self) -> dict[str, str]:
        env = dict(self._environ)
        env.update(_FIXED_ENVIRONMENT)
        if self._token is not None:
            env["GH_TOKEN"] = self._token.get_secret_value()
        return env

    async def run(self, args: Sequence[str], *, stdin: str | None = None) -> GhResult:
        argv = [self._command, *args]
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.child_environment(),
            )
        except OSError as exc:
            self._log.debug(
                "gh_invocation",
                argv=[arg[:_LOGGED_ARG_LENGTH] for arg in argv],
                exit_code=None,
                error=str(exc),
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            raise GitHubError("config", f"cannot run {self._command!r}: {exc}") from exc

        payload = stdin.encode("utf-8") if stdin is not None else None
        summary = " ".join(args[:3])
        overrun = asyncio.Event()
        try:
            out, err = await asyncio.wait_for(
                self._communicate(process, payload, overrun), timeout=self._timeout_s
            )
        except TimeoutError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if overrun.is_set():
                # The kill went out and the pipes still did not close in time: report the
                # cause, not the symptom.
                raise self._overrun_error(argv, started, summary, process.returncode) from None
            self._log.debug(
                "gh_invocation",
                argv=[arg[:_LOGGED_ARG_LENGTH] for arg in argv],
                exit_code=None,
                timed_out=True,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            raise GitHubError(
                "transport", f"gh timed out after {self._timeout_s:.0f}s: {summary}"
            ) from None
        except BaseException:
            # Ensure cleanup on any other exception (e.g., CancelledError)
            if process.returncode is None:
                process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
            raise

        if overrun.is_set():
            raise self._overrun_error(argv, started, summary, process.returncode)
        result = GhResult(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
        )
        self._log.debug(
            "gh_invocation",
            argv=[arg[:_LOGGED_ARG_LENGTH] for arg in argv],
            exit_code=result.returncode,
            duration_ms=round((time.monotonic() - started) * 1000),
            stdout_bytes=len(out),
            stderr_bytes=len(err),
        )
        return result

    async def _communicate(
        self, process: asyncio.subprocess.Process, payload: bytes | None, overrun: asyncio.Event
    ) -> tuple[bytes, bytes]:
        """``process.communicate`` with a cap: past ``max_output_bytes`` on either stream the
        process is killed, ``overrun`` is set, and the streams are drained to their end."""

        def on_overrun() -> None:
            overrun.set()
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()

        out, err, _ = await asyncio.gather(
            _read_capped(process.stdout, self._max_output_bytes, on_overrun),
            _read_capped(process.stderr, self._max_output_bytes, on_overrun),
            _feed(process, payload),
        )
        await process.wait()
        return out, err

    def _overrun_error(
        self, argv: list[str], started: float, summary: str, exit_code: int | None
    ) -> GitHubError:
        """The exit code is the kill's when the cap cut the process short, and the child's own
        when it finished inside the pipe buffer before the reader caught up."""
        self._log.debug(
            "gh_invocation",
            argv=[arg[:_LOGGED_ARG_LENGTH] for arg in argv],
            exit_code=exit_code,
            overrun=True,
            max_output_bytes=self._max_output_bytes,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return GitHubError(
            "response", f"gh output exceeded {self._max_output_bytes} bytes: {summary}"
        )


async def _read_capped(
    stream: asyncio.StreamReader | None, limit: int, on_overrun: Callable[[], None]
) -> bytes:
    """Read a stream to its end, keeping at most ``limit`` bytes; ``on_overrun`` fires once,
    at the first byte past the cap, and the rest is read and dropped so the child can exit."""
    if stream is None:
        return b""
    chunks: list[bytes] = []
    size = 0
    overrun = False
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            return b"".join(chunks)
        if overrun:
            continue
        size += len(chunk)
        if size > limit:
            overrun = True
            on_overrun()
            continue
        chunks.append(chunk)


async def _feed(process: asyncio.subprocess.Process, payload: bytes | None) -> None:
    stdin = process.stdin
    if stdin is None:
        return
    try:
        if payload:
            stdin.write(payload)
            await stdin.drain()
    except BrokenPipeError, ConnectionResetError:
        return
    finally:
        stdin.close()
