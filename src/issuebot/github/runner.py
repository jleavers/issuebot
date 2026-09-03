"""The only place that spawns the gh CLI."""

import asyncio
import contextlib
import os
import time
from collections.abc import Mapping, Sequence
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


@dataclass(frozen=True, slots=True)
class GhResult:
    returncode: int
    stdout: str
    stderr: str


class GhRunnerLike(Protocol):
    async def run(self, args: Sequence[str], *, stdin: str | None = None) -> GhResult: ...


class GhRunner:
    """Runs ``gh`` as an asyncio subprocess with a controlled environment and a timeout."""

    def __init__(
        self,
        *,
        command: str = "gh",
        token: SecretStr | None = None,
        timeout_ms: int = 30_000,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._command = command
        self._token = token
        self._timeout_s = timeout_ms / 1000
        self._environ = dict(os.environ if environ is None else environ)
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
        try:
            out, err = await asyncio.wait_for(process.communicate(payload), timeout=self._timeout_s)
        except TimeoutError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            summary = " ".join(args[:3])
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
