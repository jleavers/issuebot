"""LISTEN issuebot_refresh: one connection, one task, a callback per notification."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import psycopg
from psycopg import AsyncConnection

from issuebot.db.connection import Connector, connect, error_text, reconnect_delay, redact
from issuebot.log import get_logger

REFRESH_CHANNEL = "issuebot_refresh"
_LOST = (psycopg.OperationalError, psycopg.InterfaceError)


class RefreshListener:
    """Calls ``on_notify`` for every NOTIFY on the refresh channel; reconnects with backoff."""

    def __init__(
        self,
        url: str,
        on_notify: Callable[[], None],
        *,
        connect: Connector = connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = url
        self._on_notify = on_notify
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
                await conn.execute(f"LISTEN {REFRESH_CHANNEL}")
            except _LOST as exc:
                failures += 1
                await self._lost(exc, failures)
                continue
            failures = 0
            if self._connected_once:
                self.reconnects += 1
            self._connected_once = True
            self._log.info("db_listen_started", channel=REFRESH_CHANNEL, reconnects=self.reconnects)
            try:
                async for notification in conn.notifies():
                    self.notified += 1
                    self._log.info("db_refresh_received", channel=notification.channel)
                    try:
                        self._on_notify()
                    except Exception:
                        self._log.exception("db_refresh_callback_failed")
            except _LOST as exc:
                failures = 1
                await self._lost(exc, failures)

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

    async def _close_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(psycopg.Error):
                await conn.close()
