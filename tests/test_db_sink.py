"""Tests for PostgresSink with a fake store (no database)."""

import asyncio
import io
import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from issuebot.agent.scrub import DEFAULT_SCRUBBER, Scrubber
from issuebot.agent.turnlog import TurnCapture, capture_turns
from issuebot.db import StoreError, StoreUnavailableError
from issuebot.db import sink as sink_module
from issuebot.db.sink import PostgresSink
from issuebot.db.store import IssueSnapshot
from issuebot.events import Blocked, Event, EventBus, LogSink, RunEnded, StateChanged
from issuebot.github import Issue
from issuebot.log import configure_logging

T0 = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
DESCRIPTION = "postgresql://issuebot@db.example/issuebot"
SAMPLE = Path(__file__).parent / "fixtures" / "runs" / "20260904T202535Z-0964cd"


class FakeStore:
    """Records every write; raises what the test scripts; can hang."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.connects = 0
        self.closed = False
        self.fail_connect: list[Exception] = []
        self.fail_next: list[Exception] = []
        self.hang = False

    async def connect(self) -> None:
        self.connects += 1
        if self.fail_connect:
            raise self.fail_connect.pop(0)

    async def close(self) -> None:
        self.closed = True

    async def _gate(self) -> None:
        if self.hang:
            await asyncio.Event().wait()
        if self.fail_next:
            raise self.fail_next.pop(0)

    async def apply_event(self, event: Event, turns: Sequence[TurnCapture] = ()) -> None:
        await self._gate()
        self.calls.append(("event", event, tuple(turns)))

    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None:
        await self._gate()
        self.calls.append(("issues", list(issues)))

    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None:
        await self._gate()
        self.calls.append(("snapshot", at, dict(data)))


class FakeSnapshot:
    def __init__(self, at: datetime, **data: Any) -> None:
        self.at = at
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class Harness:
    def __init__(
        self,
        capture: Callable[[Path], list[TurnCapture]] = capture_turns,
        scrubber: Scrubber = DEFAULT_SCRUBBER,
    ) -> None:
        self.store = FakeStore()
        self.sleeps: list[float] = []
        self.clock = Clock()
        self.stream = io.StringIO()
        configure_logging(fmt="json", level="DEBUG", stream=self.stream)  # type: ignore[arg-type]
        self.sink = PostgresSink(
            self.store,
            sleep=self.sleep,
            now=self.clock,
            description=DESCRIPTION,
            capture=capture,
            scrubber=scrubber,
        )

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    def logged(self, event: str) -> list[dict[str, Any]]:
        lines = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        return [line for line in lines if line["event"] == event]


def blocked(number: int, reason: str = "x") -> Blocked:
    return Blocked(issue_number=number, issue_identifier=f"repo-{number}", reason=reason)


def run_ended(log_dir: str | None) -> RunEnded:
    return RunEnded(
        issue_number=7,
        issue_identifier="repo-7",
        run_id="20260904T202535Z-0964cd",
        outcome="succeeded",
        error=None,
        turns=1,
        input_tokens=513338,
        output_tokens=8425,
        cost_usd=0.8976,
        duration_s=205.0,
        log_dir=log_dir,
    )


@pytest.fixture
def h() -> Harness:
    return Harness()


# --- enqueue and order --------------------------------------------------------------------


async def test_events_published_before_start_are_written_in_order(h: Harness) -> None:
    bus = EventBus([LogSink(), h.sink])
    bus.publish(blocked(1))
    bus.publish(blocked(2))
    h.sink.start()
    await h.sink.close()
    assert [call[1].issue_number for call in h.store.calls] == [1, 2]
    assert (h.sink.written, h.sink.failed, h.sink.dropped) == (2, 0, 0)
    assert h.store.connects == 1 and h.store.closed
    connected = h.logged("db_connected")[0]
    assert (connected["attempt"], connected["database"]) == (1, DESCRIPTION)
    closed = h.logged("db_sink_closed")[0]
    assert (closed["written"], closed["failed"], closed["dropped"]) == (2, 0, 0)


async def test_the_cap_drops_and_logs_but_delivery_continues(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sink_module, "QUEUE_LIMIT", 2)
    for number in (1, 2, 3):
        h.sink.handle(blocked(number))
    assert h.sink.dropped == 1
    full = h.logged("db_queue_full")[0]
    assert (full["kind"], full["issue_number"], full["limit"]) == ("blocked", 3, 2)
    h.sink.start()
    await h.sink.close()
    assert [call[1].issue_number for call in h.store.calls] == [1, 2]


async def test_markers_bypass_the_cap(
    h: Harness, monkeypatch: pytest.MonkeyPatch, make_issue: Callable[..., Issue]
) -> None:
    monkeypatch.setattr(sink_module, "QUEUE_LIMIT", 1)
    h.sink.handle(blocked(1))
    h.sink.handle(blocked(2))
    h.sink.record_issues([make_issue()])
    h.sink.record_snapshot(FakeSnapshot(T0))
    h.sink.start()
    await h.sink.close()
    assert [call[0] for call in h.store.calls] == ["event", "issues", "snapshot"]
    assert h.sink.dropped == 1


async def test_record_issues_merges_by_number_and_stamps_seen_at(
    h: Harness, make_issue: Callable[..., Issue]
) -> None:
    first = make_issue(number=1, identifier="repo-1", title="one")
    second = make_issue(number=2, identifier="repo-2", title="two")
    renamed = make_issue(number=1, identifier="repo-1", title="one, renamed")
    h.sink.record_issues([first, second])
    h.sink.record_issues([renamed])
    h.sink.record_issues([])
    h.sink.start()
    await h.sink.close()
    assert len(h.store.calls) == 1
    kind, batch = h.store.calls[0]
    assert kind == "issues"
    assert [(s.issue.number, s.issue.title, s.seen_at) for s in batch] == [
        (1, "one, renamed", T0 + timedelta(seconds=2)),
        (2, "two", T0 + timedelta(seconds=1)),
    ]
    assert h.sink.written == 1


async def test_record_snapshot_keeps_only_the_latest(h: Harness) -> None:
    h.sink.record_snapshot(FakeSnapshot(T0, tick_count=1))
    h.sink.record_snapshot(FakeSnapshot(T0 + timedelta(seconds=30), tick_count=2))
    h.sink.start()
    await h.sink.close()
    assert h.store.calls == [("snapshot", T0 + timedelta(seconds=30), {"tick_count": 2})]


async def test_markers_are_requeued_after_the_drain_took_them(
    h: Harness, make_issue: Callable[..., Issue]
) -> None:
    h.sink.start()
    h.sink.record_issues([make_issue(number=1, identifier="repo-1")])
    h.sink.record_snapshot(FakeSnapshot(T0, tick_count=1))
    await h.settle()
    h.sink.record_issues([make_issue(number=2, identifier="repo-2")])
    h.sink.record_snapshot(FakeSnapshot(T0, tick_count=2))
    await h.sink.close()
    kinds = [call[0] for call in h.store.calls]
    assert kinds == ["issues", "snapshot", "issues", "snapshot"]
    assert [s.issue.number for s in h.store.calls[2][1]] == [2]
    assert h.store.calls[3][2] == {"tick_count": 2}


async def test_events_and_markers_keep_publication_order(
    h: Harness, make_issue: Callable[..., Issue]
) -> None:
    h.sink.record_issues([make_issue()])
    h.sink.handle(blocked(1))
    h.sink.record_snapshot(FakeSnapshot(T0))
    h.sink.handle(blocked(2))
    h.sink.start()
    await h.sink.close()
    assert [call[0] for call in h.store.calls] == ["issues", "event", "snapshot", "event"]


# --- failures -----------------------------------------------------------------------------


async def test_a_lost_connection_reconnects_and_retries_the_item(h: Harness) -> None:
    h.store.fail_next = [StoreUnavailableError("server closed the connection")]
    h.sink.handle(blocked(1))
    h.sink.handle(blocked(2))
    h.sink.start()
    await h.sink.close()
    assert [call[1].issue_number for call in h.store.calls] == [1, 2]
    assert (h.sink.written, h.sink.failed, h.sink.reconnects) == (2, 0, 1)
    assert h.store.connects == 2
    assert h.sleeps == []
    retry = h.logged("db_write_retry")[0]
    assert (retry["kind"], retry["issue_number"]) == ("blocked", 1)
    assert retry["error"] == "server closed the connection"


async def test_repeated_write_failures_back_off_before_each_reconnect(h: Harness) -> None:
    h.store.fail_next = [StoreUnavailableError("disk full") for _ in range(3)]
    h.sink.handle(blocked(1))
    h.sink.handle(blocked(2))
    h.sink.start()
    await h.sink.close()
    assert [call[1].issue_number for call in h.store.calls] == [1, 2]
    assert (h.sink.written, h.sink.failed, h.sink.reconnects) == (2, 0, 3)
    assert h.store.connects == 4
    assert h.sleeps == [1.0, 2.0]
    retries = h.logged("db_write_retry")
    assert [line["retries"] for line in retries] == [1, 2, 3]
    assert [line["delay_s"] for line in retries] == [None, 1.0, 2.0]


async def test_connect_failures_back_off_before_the_first_write(h: Harness) -> None:
    h.store.fail_connect = [StoreUnavailableError("refused") for _ in range(3)]
    h.sink.handle(blocked(1))
    h.sink.start()
    await h.sink.close()
    assert h.sleeps == [1.0, 2.0, 4.0]
    assert h.store.connects == 4
    assert (h.sink.written, h.sink.reconnects) == (1, 0)
    failed = h.logged("db_connect_failed")
    assert [line["attempt"] for line in failed] == [1, 2, 3]
    assert failed[-1]["delay_s"] == 4.0
    assert h.logged("db_connected")[0]["attempt"] == 4


async def test_a_statement_failure_drops_the_item_and_continues(h: Harness) -> None:
    h.store.fail_next = [StoreError("DataError: bad value")]
    h.sink.handle(blocked(1))
    h.sink.handle(blocked(2))
    h.sink.start()
    await h.sink.close()
    assert [call[1].issue_number for call in h.store.calls] == [2]
    assert (h.sink.written, h.sink.failed, h.sink.dropped) == (1, 1, 0)
    assert h.store.connects == 1
    failed = h.logged("db_write_failed")[0]
    assert (failed["issue_number"], failed["error"]) == (1, "DataError: bad value")


async def test_an_unexpected_exception_is_logged_and_the_loop_survives(h: Harness) -> None:
    h.store.fail_next = [RuntimeError("bug")]
    h.sink.handle(blocked(1))
    h.sink.handle(blocked(2))
    h.sink.start()
    await h.sink.close()
    assert [call[1].issue_number for call in h.store.calls] == [2]
    assert (h.sink.written, h.sink.failed) == (1, 1)
    crashed = h.logged("db_write_crashed")[0]
    assert crashed["issue_number"] == 1
    assert "bug" in crashed["exception"]


# --- lifetime -----------------------------------------------------------------------------


async def test_close_times_out_on_a_hanging_store(
    h: Harness, monkeypatch: pytest.MonkeyPatch, make_issue: Callable[..., Issue]
) -> None:
    monkeypatch.setattr(sink_module, "DRAIN_TIMEOUT_S", 0.05)
    h.store.hang = True
    h.sink.handle(blocked(1))
    h.sink.handle(blocked(2))
    h.sink.record_issues([make_issue()])
    h.sink.start()
    await h.sink.close()
    assert h.store.calls == []
    assert (h.sink.written, h.sink.failed, h.sink.dropped) == (0, 1, 2)
    assert h.store.closed
    timeout = h.logged("db_drain_timeout")[0]
    assert (timeout["left"], timeout["timeout_s"]) == (2, 0.05)
    assert h.logged("db_write_cancelled")[0]["issue_number"] == 1
    assert h.sink._task is not None and h.sink._task.cancelled()


async def test_close_twice_and_close_before_start_are_noops(h: Harness) -> None:
    await h.sink.close()
    assert not h.store.closed
    h.sink.start()
    await h.sink.close()
    await h.sink.close()
    assert len(h.logged("db_sink_closed")) == 1


async def test_start_twice_raises(h: Harness) -> None:
    h.sink.start()
    with pytest.raises(RuntimeError, match="already started"):
        h.sink.start()
    await h.sink.close()


async def test_after_close_events_are_dropped_and_records_ignored(
    h: Harness, make_issue: Callable[..., Issue]
) -> None:
    h.sink.start()
    await h.sink.close()
    h.sink.handle(blocked(1))
    h.sink.record_issues([make_issue()])
    h.sink.record_snapshot(FakeSnapshot(T0))
    assert h.sink.dropped == 1
    assert h.logged("db_sink_closed_drop")[0]["issue_number"] == 1
    assert h.sink._queue.qsize() == 0


# --- turn capture -----------------------------------------------------------------------------


async def test_run_ended_captures_the_turn_files(h: Harness, tmp_path: Path) -> None:
    shutil.copytree(SAMPLE, tmp_path / "run")
    h.sink.handle(run_ended(str(tmp_path / "run")))
    h.sink.start()
    await h.sink.close()
    (call,) = h.store.calls
    assert call[0] == "event" and call[1].kind == "run_ended"
    (turn,) = call[2]
    assert (turn.turn_number, turn.model, turn.stream_lines) == (1, "claude-opus-5", 95)
    captured = h.logged("db_turns_captured")[0]
    assert (captured["run_id"], captured["turns"]) == ("20260904T202535Z-0964cd", 1)
    assert captured["stream_bytes"] == 114948


async def test_run_ended_stores_the_log_dir_scrubbed_after_reading_from_it(tmp_path: Path) -> None:
    """The path names the operator's home and the dashboard renders it (#91); the capture
    still needs the real one."""
    shutil.copytree(SAMPLE, tmp_path / "run")
    read_from: list[Path] = []

    def capture(log_dir: Path) -> list[TurnCapture]:
        read_from.append(log_dir)
        return capture_turns(log_dir)

    h = Harness(capture, scrubber=Scrubber(home=str(tmp_path)))
    h.sink.handle(run_ended(str(tmp_path / "run")))
    h.sink.start()
    await h.sink.close()
    (call,) = h.store.calls
    assert read_from == [tmp_path / "run"]
    assert call[1].log_dir == "~/run"
    assert len(call[2]) == 1


async def test_a_missing_log_dir_gives_no_captures(h: Harness, tmp_path: Path) -> None:
    h.sink.handle(run_ended(str(tmp_path / "gone")))
    h.sink.start()
    await h.sink.close()
    assert h.store.calls[0][2] == ()
    assert h.logged("db_turns_captured")[0]["turns"] == 0


async def test_run_ended_without_a_log_dir_never_captures() -> None:
    calls: list[Path] = []

    def capture(log_dir: Path) -> list[TurnCapture]:
        calls.append(log_dir)
        return []

    h = Harness(capture)
    h.sink.handle(run_ended(None))
    h.sink.handle(blocked(1))
    h.sink.start()
    await h.sink.close()
    assert calls == []
    assert [call[1].kind for call in h.store.calls] == ["run_ended", "blocked"]
    assert h.logged("db_turns_captured") == []


async def test_a_capture_failure_is_logged_and_the_event_still_written() -> None:
    def capture(log_dir: Path) -> list[TurnCapture]:
        raise RuntimeError("disk on fire")

    h = Harness(capture)
    h.sink.handle(run_ended("/workspaces/repo-7/.issuebot/runs/x"))
    h.sink.start()
    await h.sink.close()
    assert h.store.calls[0][2] == ()
    assert (h.sink.written, h.sink.failed) == (1, 0)
    failed = h.logged("db_turns_capture_failed")[0]
    assert failed["log_dir"] == "/workspaces/repo-7/.issuebot/runs/x"
    assert failed["error"] == "RuntimeError: disk on fire"


async def test_the_capture_runs_once_even_when_the_write_is_retried(tmp_path: Path) -> None:
    calls: list[Path] = []

    def capture(log_dir: Path) -> list[TurnCapture]:
        calls.append(log_dir)
        return capture_turns(log_dir)

    shutil.copytree(SAMPLE, tmp_path / "run")
    h = Harness(capture)
    h.store.fail_next = [StoreUnavailableError("server closed the connection")]
    h.sink.handle(run_ended(str(tmp_path / "run")))
    h.sink.start()
    await h.sink.close()
    assert calls == [tmp_path / "run"]
    (call,) = h.store.calls
    assert len(call[2]) == 1 and h.sink.reconnects == 1


async def test_a_bare_event_has_no_issue_number(h: Harness) -> None:
    class Bare(Event):
        pass

    h.sink.handle(Bare())
    h.sink.start()
    await h.sink.close()
    assert h.store.calls[0][1].kind == "event"
    assert h.sink.written == 1


async def test_state_changed_reaches_the_store_through_the_bus(h: Harness) -> None:
    bus = EventBus([h.sink])
    h.sink.start()
    bus.publish(
        StateChanged(
            issue_number=1,
            issue_identifier="repo-1",
            from_label=None,
            to_label="issuebot/todo",
            actor="human",
        )
    )
    await h.settle()
    assert h.store.calls[0][1].kind == "state_changed"
    await h.sink.close()
