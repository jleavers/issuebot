"""PostgresSink: enqueue in ``handle``/``record_*``; one drain task writes and reconnects."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from issuebot.agent.turnlog import TurnCapture, capture_turns
from issuebot.db.connection import reconnect_delay
from issuebot.db.errors import StoreError, StoreUnavailableError
from issuebot.db.store import IssueSnapshot, Store
from issuebot.events import Event, IssueEvent, RunEnded
from issuebot.github import Issue
from issuebot.log import get_logger

QUEUE_LIMIT = 1000
DRAIN_TIMEOUT_S = 10.0


class SnapshotLike(Protocol):
    """What ``record_snapshot`` needs from a RuntimeSnapshot; keeps ``db`` off ``orchestrator``."""

    @property
    def at(self) -> datetime: ...

    def to_dict(self) -> dict[str, Any]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _EventItem:
    event: Event


_Marker = Literal["issues", "snapshot"]
_Item = _EventItem | _Marker
_Work = (
    _EventItem
    | tuple[Literal["issues"], list[IssueSnapshot]]
    | tuple[Literal["snapshot"], datetime, dict[str, Any]]
)


class PostgresSink:
    """Writes events, polled issues and the runtime snapshot from one background task.

    ``handle``, ``record_issues`` and ``record_snapshot`` only enqueue; ``start`` creates
    the drain task; ``close`` drains what is queued (bounded) and stops it. A lost
    connection is retried with backoff and the item in flight is retried, not dropped.
    A ``run_ended`` item has its turn files captured (in a thread, once) before its first
    write attempt, so the ``run_turns`` rows land in the same transaction as the run's.
    """

    name = "postgres"

    def __init__(
        self,
        store: Store,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = _utcnow,
        description: str | None = None,
        capture: Callable[[Path], list[TurnCapture]] = capture_turns,
    ) -> None:
        self._store = store
        self._sleep = sleep
        self._now = now
        self._description = description
        self._capture_turns = capture
        self._queue: asyncio.Queue[_Item | None] = asyncio.Queue()
        self._issues: dict[int, IssueSnapshot] = {}
        self._issues_queued = False
        self._snapshot: tuple[datetime, dict[str, Any]] | None = None
        self._snapshot_queued = False
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._connected = False
        self._ever_connected = False
        self.written = 0
        self.failed = 0
        self.dropped = 0
        self.reconnects = 0
        self._log = get_logger(__name__)

    # --- the enqueue side (never blocks, never raises) ------------------------------------

    def handle(self, event: Event) -> None:
        number = event.issue_number if isinstance(event, IssueEvent) else None
        if self._closed:
            self.dropped += 1
            self._log.debug("db_sink_closed_drop", kind=event.kind, issue_number=number)
            return
        if self._queue.qsize() >= QUEUE_LIMIT:
            self.dropped += 1
            self._log.warning(
                "db_queue_full", kind=event.kind, issue_number=number, limit=QUEUE_LIMIT
            )
            return
        self._queue.put_nowait(_EventItem(event))

    def record_issues(self, issues: Sequence[Issue]) -> None:
        """Merge polled snapshots into the pending batch; a later snapshot replaces an earlier."""
        if self._closed or not issues:
            return
        seen_at = self._now()
        for issue in issues:
            self._issues[issue.number] = IssueSnapshot(issue=issue, seen_at=seen_at)
        if not self._issues_queued:
            self._issues_queued = True
            self._queue.put_nowait("issues")

    def record_snapshot(self, snapshot: SnapshotLike) -> None:
        """Keep the latest runtime snapshot; it is written once the drain task gets to it."""
        if self._closed:
            return
        self._snapshot = (snapshot.at, snapshot.to_dict())
        if not self._snapshot_queued:
            self._snapshot_queued = True
            self._queue.put_nowait("snapshot")

    # --- lifetime ------------------------------------------------------------------------

    def start(self) -> None:
        """Create the drain task on the running loop; the first connect happens there."""
        if self._task is not None:
            raise RuntimeError("PostgresSink is already started")
        self._task = asyncio.create_task(self._drain(), name="issuebot-postgres-sink")
        self._log.info("db_sink_started")

    async def close(self) -> None:
        """Write what is queued for at most DRAIN_TIMEOUT_S, then stop and close the store."""
        if self._task is None or self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)
        try:
            await asyncio.wait_for(self._task, DRAIN_TIMEOUT_S)
        except TimeoutError:
            left = 0
            while True:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is not None:
                    left += 1
            self.dropped += left
            self._log.warning("db_drain_timeout", left=left, timeout_s=DRAIN_TIMEOUT_S)
        await self._store.close()
        self._log.info(
            "db_sink_closed",
            written=self.written,
            failed=self.failed,
            dropped=self.dropped,
            reconnects=self.reconnects,
        )

    # --- the drain task --------------------------------------------------------------------

    async def _drain(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            work = self._take(item)
            if work is None:
                continue
            try:
                await self._write(work)
            except asyncio.CancelledError:
                self.failed += 1
                self._log.warning("db_write_cancelled", **_describe(work))
                raise
            except Exception:
                self.failed += 1
                self._log.exception("db_write_crashed", **_describe(work))

    def _take(self, item: _Item) -> _Work | None:
        """Resolve a queue item into the work to do, taking the pending batch or slot now.

        None when a snapshot marker finds no snapshot: nothing to write, never an empty row.
        """
        if isinstance(item, _EventItem):
            return item
        if item == "issues":
            batch = list(self._issues.values())
            self._issues = {}
            self._issues_queued = False
            return ("issues", batch)
        self._snapshot_queued = False
        if self._snapshot is None:
            return None
        at, data = self._snapshot
        self._snapshot = None
        return ("snapshot", at, data)

    async def _write(self, work: _Work) -> None:
        turns = await self._capture(work)
        retries = 0
        while True:
            if not self._connected:
                await self._ensure_connected()
            try:
                await self._apply(work, turns)
            except StoreUnavailableError as exc:
                self._connected = False
                retries += 1
                delay = None if retries == 1 else reconnect_delay(retries - 1)
                self._log.warning(
                    "db_write_retry",
                    error=exc.message,
                    retries=retries,
                    delay_s=delay,
                    **_describe(work),
                )
                if delay is not None:
                    await self._sleep(delay)
                continue
            except StoreError as exc:
                self.failed += 1
                self._log.warning("db_write_failed", error=exc.message, **_describe(work))
                return
            self.written += 1
            return

    async def _ensure_connected(self) -> None:
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._store.connect()
            except StoreUnavailableError as exc:
                delay = reconnect_delay(attempt)
                self._log.warning(
                    "db_connect_failed", attempt=attempt, error=exc.message, delay_s=delay
                )
                await self._sleep(delay)
                continue
            self._connected = True
            if self._ever_connected:
                self.reconnects += 1
            self._ever_connected = True
            self._log.info(
                "db_connected",
                database=self._description,
                attempt=attempt,
                reconnects=self.reconnects,
            )
            return

    async def _capture(self, work: _Work) -> tuple[TurnCapture, ...]:
        """The turn files of a run_ended item, read once in a thread; () for anything else."""
        if not isinstance(work, _EventItem) or not isinstance(work.event, RunEnded):
            return ()
        event = work.event
        if not event.log_dir:
            return ()
        try:
            captures = await asyncio.to_thread(self._capture_turns, Path(event.log_dir))
        except Exception as exc:
            self._log.warning(
                "db_turns_capture_failed",
                run_id=event.run_id,
                log_dir=event.log_dir,
                error=f"{type(exc).__name__}: {exc}",
            )
            return ()
        self._log.info(
            "db_turns_captured",
            run_id=event.run_id,
            turns=len(captures),
            stream_bytes=sum(capture.stream_bytes for capture in captures),
        )
        return tuple(captures)

    async def _apply(self, work: _Work, turns: tuple[TurnCapture, ...]) -> None:
        if isinstance(work, _EventItem):
            await self._store.apply_event(work.event, turns=turns)
        elif work[0] == "issues":
            await self._store.upsert_issues(work[1])
        else:
            await self._store.write_snapshot(work[1], work[2])


def _describe(work: _Work) -> dict[str, Any]:
    if isinstance(work, _EventItem):
        event = work.event
        number = event.issue_number if isinstance(event, IssueEvent) else None
        return {"kind": event.kind, "issue_number": number}
    if work[0] == "issues":
        return {"kind": "issues", "count": len(work[1])}
    return {"kind": "snapshot"}
