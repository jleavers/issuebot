"""LISTEN on the repository's refresh channel: one connection, one task, a callback per
notification."""

import asyncio
import contextlib
import hashlib
import re
from collections.abc import Awaitable, Callable

import psycopg
from psycopg import AsyncConnection

from issuebot.db.connection import Connector, connect, error_text, reconnect_delay, redact
from issuebot.log import get_logger

REFRESH_CHANNEL = "issuebot_refresh"
REPO_PAYLOAD = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")  # RepoName, settings.py
_CHANNEL_DIGEST_CHARS = 16


def refresh_channel(repo: str | None) -> str:
    """The channel a repository's worker listens on: one per repository (#110).

    A NOTIFY on it reaches the one worker it is for, so the fan-out is never
    database-wide: a client that can NOTIFY can still wake that worker, at the rate the
    orchestrator admits, but not every worker on the store at once. The name carries a
    digest of the repository rather than the repository, since an identifier is 63 bytes
    and ``owner/name`` can be longer. Without a repository it is the bare channel, which no
    worker listens on.
    """
    if repo is None:
        return REFRESH_CHANNEL
    digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:_CHANNEL_DIGEST_CHARS]
    return f"{REFRESH_CHANNEL}_{digest}"


_LOST = (psycopg.OperationalError, psycopg.InterfaceError)


class RefreshListener:
    """Calls ``on_notify`` for every NOTIFY on the refresh channel; reconnects with backoff.

    A lost connection and any other unexpected error are both contained here: the task logs
    and reconnects rather than ending, so only cancellation (from ``close``) stops it.
    """

    def __init__(
        self,
        url: str,
        on_notify: Callable[[], None],
        *,
        repo: str | None = None,
        connect: Connector = connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = url
        self._on_notify = on_notify
        self._repo = repo
        self.channel = refresh_channel(repo)
        self._connect = connect
        self._sleep = sleep
        self._conn: AsyncConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._connected_once = False
        self.notified = 0
        self.reconnects = 0
        self._log = get_logger(__name__)

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("RefreshListener is already started")
        self._task = asyncio.create_task(self._run(), name="issuebot-refresh-listener")

    async def close(self) -> None:
        """Stop listening and close the connection; idempotent, a no-op before ``start``."""
        if self._task is None or self._closed:
            return
        self._closed = True
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        await self._close_connection()
        self._log.info("db_listen_closed", notified=self.notified, reconnects=self.reconnects)

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                conn = await self._connect(self._url)
                self._conn = conn
                await conn.execute(f"LISTEN {self.channel}")
            except _LOST as exc:
                failures += 1
                await self._lost(exc, failures)
                continue
            except Exception as exc:
                failures += 1
                await self._crashed(exc, failures)
                continue
            failures = 0
            if self._connected_once:
                self.reconnects += 1
            self._connected_once = True
            self._log.info("db_listen_started", channel=self.channel, reconnects=self.reconnects)
            try:
                async for notification in conn.notifies():
                    self.notified += 1
                    payload = notification.payload or ""
                    if not self._accepts(payload):
                        continue
                    self._log.info(
                        "db_refresh_received", channel=notification.channel, payload=payload
                    )
                    try:
                        self._on_notify()
                    except Exception:
                        self._log.exception("db_refresh_callback_failed")
            except _LOST as exc:
                failures = 1
                await self._lost(exc, failures)
            except Exception as exc:
                failures = 1
                await self._crashed(exc, failures)

    def _accepts(self, payload: str) -> bool:
        """An empty payload, or this repository's name, wakes the worker; another repository's
        name is a NOTIFY on the wrong channel and is dropped at debug; anything else is
        dropped with a warning (spec §5)."""
        if self._repo is None or not payload or payload == self._repo:
            return True
        if REPO_PAYLOAD.match(payload):
            self._log.debug("db_refresh_other_repo", payload=payload, repo=self._repo)
        else:
            self._log.warning("refresh_payload_ignored", payload=payload, repo=self._repo)
        return False

    async def _lost(self, exc: Exception, failures: int) -> None:
        await self._close_connection()
        delay = reconnect_delay(failures)
        self._log.warning(
            "db_listen_lost",
            error=redact(f"{type(exc).__name__}: {error_text(exc)}", self._url),
            attempt=failures,
            delay_s=delay,
        )
        await self._sleep(delay)

    async def _crashed(self, exc: Exception, failures: int) -> None:
        """An unexpected error: log it with the traceback, then reconnect as if lost."""
        delay = reconnect_delay(failures)
        self._log.exception(
            "db_listen_crashed",
            error=redact(f"{type(exc).__name__}: {error_text(exc)}", self._url),
            attempt=failures,
            delay_s=delay,
        )
        await self._close_connection()
        await self._sleep(delay)

    async def _close_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(psycopg.Error):
                await conn.close()
