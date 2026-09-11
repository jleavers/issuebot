"""Tests for RefreshListener: a fake connection (hermetic) and one real NOTIFY round trip."""

import asyncio
import io
import json
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest

from issuebot.db import migrate
from issuebot.db.database import Database
from issuebot.db.listen import REFRESH_CHANNEL, RefreshListener
from issuebot.log import configure_logging

URL = "postgresql://issuebot:s3cret@db.example/issuebot"


class FakeNotify:
    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.payload = ""
        self.pid = 1


class FakeConnection:
    """``notifies()`` yields what the test feeds; an exception fed ends the stream with it."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.feed: asyncio.Queue[FakeNotify | Exception] = asyncio.Queue()
        self.closed = False

    async def execute(self, query: str, params: Any = None) -> None:
        self.executed.append(query)

    async def close(self) -> None:
        self.closed = True

    async def notifies(self) -> AsyncIterator[FakeNotify]:
        while True:
            item = await self.feed.get()
            if isinstance(item, Exception):
                raise item
            yield item


class Harness:
    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self.fail_connect: list[Exception] = []
        self.sleeps: list[float] = []
        self.calls = 0
        self.stream = io.StringIO()
        configure_logging(fmt="json", level="DEBUG", stream=self.stream)  # type: ignore[arg-type]
        self.listener = RefreshListener(URL, self.on_notify, connect=self.connect, sleep=self.sleep)

    async def connect(self, url: str) -> Any:
        assert url == URL
        if self.fail_connect:
            raise self.fail_connect.pop(0)
        conn = FakeConnection()
        self.connections.append(conn)
        return conn

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)

    def on_notify(self) -> None:
        self.calls += 1

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    def logged(self, event: str) -> list[dict[str, Any]]:
        lines = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        return [line for line in lines if line["event"] == event]


@pytest.fixture
def h() -> Harness:
    return Harness()


async def test_a_notification_calls_the_callback(h: Harness) -> None:
    h.listener.start()
    await h.settle()
    (conn,) = h.connections
    assert conn.executed == [f"LISTEN {REFRESH_CHANNEL}"]
    assert h.logged("db_listen_started")[0]["channel"] == "issuebot_refresh"
    conn.feed.put_nowait(FakeNotify(REFRESH_CHANNEL))
    conn.feed.put_nowait(FakeNotify(REFRESH_CHANNEL))
    await h.settle()
    assert (h.calls, h.listener.notified) == (2, 2)
    assert len(h.logged("db_refresh_received")) == 2
    await h.listener.close()
    assert conn.closed
    closed = h.logged("db_listen_closed")[0]
    assert (closed["notified"], closed["reconnects"]) == (2, 0)


async def test_a_lost_connection_reconnects_with_backoff(h: Harness) -> None:
    h.listener.start()
    await h.settle()
    h.connections[0].feed.put_nowait(psycopg.OperationalError(f"server closed {URL}"))
    await h.settle()
    assert len(h.connections) == 2
    assert h.connections[0].closed
    assert h.sleeps == [1.0]
    assert h.listener.reconnects == 1
    lost = h.logged("db_listen_lost")[0]
    assert lost["error"] == "OperationalError: server closed <database url>"
    assert (lost["attempt"], lost["delay_s"]) == (1, 1.0)
    assert "s3cret" not in h.stream.getvalue()
    h.connections[1].feed.put_nowait(FakeNotify(REFRESH_CHANNEL))
    await h.settle()
    assert h.calls == 1
    await h.listener.close()


async def test_an_unexpected_error_is_logged_and_listening_resumes(h: Harness) -> None:
    h.listener.start()
    await h.settle()
    h.connections[0].feed.put_nowait(RuntimeError("bug"))
    await h.settle()
    assert len(h.connections) == 2
    assert h.connections[0].closed
    assert h.sleeps == [1.0]
    assert h.listener.reconnects == 1
    (crashed,) = h.logged("db_listen_crashed")
    assert "bug" in crashed["exception"]
    assert (crashed["attempt"], crashed["delay_s"]) == (1, 1.0)
    h.connections[1].feed.put_nowait(FakeNotify(REFRESH_CHANNEL))
    await h.settle()
    assert h.calls == 1
    await h.listener.close()
    assert len(h.logged("db_listen_closed")) == 1


async def test_connect_failures_back_off_and_count_consecutively(h: Harness) -> None:
    h.fail_connect = [psycopg.OperationalError("refused"), psycopg.InterfaceError("refused")]
    h.listener.start()
    await h.settle()
    assert h.sleeps == [1.0, 2.0]
    assert len(h.connections) == 1
    assert h.listener.reconnects == 0
    assert [line["attempt"] for line in h.logged("db_listen_lost")] == [1, 2]
    await h.listener.close()


async def test_a_raising_callback_is_logged_and_listening_continues(h: Harness) -> None:
    def explode() -> None:
        raise RuntimeError("bug")

    h.listener._on_notify = explode
    h.listener.start()
    await h.settle()
    h.connections[0].feed.put_nowait(FakeNotify(REFRESH_CHANNEL))
    h.connections[0].feed.put_nowait(FakeNotify(REFRESH_CHANNEL))
    await h.settle()
    assert h.listener.notified == 2
    assert len(h.logged("db_refresh_callback_failed")) == 2
    await h.listener.close()


async def test_close_is_idempotent_and_a_noop_before_start(h: Harness) -> None:
    await h.listener.close()
    assert h.logged("db_listen_closed") == []
    h.listener.start()
    with pytest.raises(RuntimeError, match="already started"):
        h.listener.start()
    await h.settle()
    await h.listener.close()
    await h.listener.close()
    assert len(h.logged("db_listen_closed")) == 1
    assert h.listener._task is not None and h.listener._task.cancelled()


def notify(payload: str) -> FakeNotify:
    item = FakeNotify(REFRESH_CHANNEL)
    item.payload = payload
    return item


async def test_a_scoped_listener_fires_on_its_repo_and_on_an_empty_payload() -> None:
    h = Harness()
    h.listener = RefreshListener(
        URL, h.on_notify, repo="example/repo", connect=h.connect, sleep=h.sleep
    )
    h.listener.start()
    await h.settle()
    (conn,) = h.connections
    conn.feed.put_nowait(notify("example/repo"))
    conn.feed.put_nowait(notify(""))
    conn.feed.put_nowait(notify("example/other"))
    conn.feed.put_nowait(notify("not a repo!"))
    await h.settle()
    assert h.calls == 2
    assert h.listener.notified == 4  # every notification is counted, two were filtered
    assert len(h.logged("db_refresh_other_repo")) == 1
    (ignored,) = h.logged("refresh_payload_ignored")
    assert ignored["payload"] == "not a repo!"
    await h.listener.close()


async def test_an_unscoped_listener_fires_on_every_payload(h: Harness) -> None:
    h.listener.start()
    await h.settle()
    (conn,) = h.connections
    conn.feed.put_nowait(notify("example/other"))
    conn.feed.put_nowait(notify("anything"))
    await h.settle()
    assert h.calls == 2
    await h.listener.close()


# --- against a real server -------------------------------------------------------------------


async def test_a_real_notify_reaches_the_callback(db_url: str) -> None:
    await migrate(db_url)
    received = asyncio.Event()
    calls = 0

    def on_notify() -> None:
        nonlocal calls
        calls += 1
        received.set()

    listener = Database(db_url).listener(on_notify, repo="example/repo")
    listener.start()
    try:
        for _ in range(50):  # wait for LISTEN to be in place
            if listener._conn is not None and listener._conn.pgconn.status == 0:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        await Database(db_url).notify_refresh("example/repo")
        await asyncio.wait_for(received.wait(), timeout=2.0)
        assert (calls, listener.notified) == (1, 1)
        await Database(db_url).notify_refresh("example/other")
        for _ in range(100):
            if listener.notified == 2:
                break
            await asyncio.sleep(0.02)
        assert listener.notified == 2  # counted...
        assert calls == 1  # ...but not delivered: a different repository
    finally:
        await listener.close()
