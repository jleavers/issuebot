"""Tests for the Slack transport (against a local HTTP server) and the sink (with a fake poster)."""

import asyncio
import io
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from issuebot.config import GitHubLabels, SlackSettings
from issuebot.events import Blocked, Event, EventBus, NotificationSent, RunStarted, StateChanged
from issuebot.log import configure_logging
from issuebot.notifications import (
    PostResult,
    SlackSink,
    redact,
    slack_payload,
    subscribed_kinds,
    urllib_post,
)
from issuebot.notifications import slack as slack_module

URL = "https://hooks.slack.com/services/T000/B000/secret"
PATH = "/services/T000/B000/secret"


# --- a local HTTP server -----------------------------------------------------------------


@dataclass
class Received:
    path: str
    content_type: str | None
    body: bytes


class ScriptedServer:
    """Answers each POST from a script of (status, headers) pairs; 200 once the script is empty."""

    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, str]]] = []
        self.received: list[Received] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                server.received.append(Received(self.path, self.headers.get("Content-Type"), body))
                status, headers = server.script.pop(0) if server.script else (200, {})
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args: object) -> None:
                pass

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._http.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self._http.server_address[:2]
        return f"http://{host}:{port}{PATH}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if self._thread.is_alive():
            self._http.shutdown()
        self._http.server_close()


@pytest.fixture
def server() -> Iterator[ScriptedServer]:
    scripted = ScriptedServer()
    scripted.start()
    yield scripted
    scripted.stop()


# --- transport -----------------------------------------------------------------------------


async def test_urllib_post_delivers_json(server: ScriptedServer) -> None:
    result = await urllib_post(server.url, slack_payload("hello"), timeout_s=5)
    assert result == PostResult(status=200)
    assert result.ok
    (request,) = server.received
    assert request.path == PATH
    assert request.content_type == "application/json; charset=utf-8"
    assert json.loads(request.body) == {"text": "hello"}


async def test_urllib_post_reads_a_numeric_retry_after(server: ScriptedServer) -> None:
    server.script = [(429, {"Retry-After": "2"})]
    result = await urllib_post(server.url, slack_payload("x"), timeout_s=5)
    assert (result.status, result.retry_after_s, result.retryable) == (429, 2.0, True)
    assert result.error == "HTTP Error 429: Too Many Requests"


async def test_urllib_post_ignores_a_non_numeric_retry_after(server: ScriptedServer) -> None:
    server.script = [(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})]
    result = await urllib_post(server.url, slack_payload("x"), timeout_s=5)
    assert (result.status, result.retry_after_s) == (429, None)


async def test_urllib_post_reports_server_errors(server: ScriptedServer) -> None:
    server.script = [(500, {})]
    result = await urllib_post(server.url, slack_payload("x"), timeout_s=5)
    assert (result.status, result.retryable, result.ok) == (500, True, False)


async def test_urllib_post_reports_a_closed_port_without_raising() -> None:
    scripted = ScriptedServer()
    url = scripted.url
    scripted.stop()
    result = await urllib_post(url, slack_payload("x"), timeout_s=5)
    assert result.status is None
    assert result.retryable
    assert result.error is not None
    assert "secret" not in result.error


async def test_urllib_post_refuses_other_schemes() -> None:
    result = await urllib_post("ftp://hooks.slack.com/services/secret", slack_payload("x"))
    assert result == PostResult(status=None, error="unsupported URL scheme 'ftp'")


async def test_urllib_post_reports_an_unparseable_url_without_raising() -> None:
    result = await urllib_post("http://[::1", slack_payload("x"))
    assert result.status is None
    assert result.error is not None


async def test_urllib_post_reports_an_invalid_path_without_raising() -> None:
    result = await urllib_post("http://127.0.0.1:9/bad path", slack_payload("x"))
    assert result.status is None
    assert result.error is not None
    assert "bad path" not in result.error


# --- helpers ---------------------------------------------------------------------------


def test_redact_strips_the_url_and_its_path() -> None:
    text = f"failed for {URL} and again for {PATH}; status 500"
    assert redact(text, URL) == "failed for <webhook url> and again for <webhook url>; status 500"
    assert redact("nothing to hide", URL) == "nothing to hide"
    assert redact("root path only", "https://h/") == "root path only"


def test_redact_strips_the_query_string() -> None:
    url = "https://hooks.example/hook?token=abc"
    text = "bad https://hooks.example/hook?token=abc then /hook?token=abc then token=abc"
    assert redact(text, url) == "bad <webhook url> then <webhook url> then <webhook url>"


@pytest.mark.parametrize(
    ("status", "ok", "retryable"),
    [
        (200, True, False),
        (204, True, False),
        (400, False, False),
        (404, False, False),
        (429, False, True),
        (500, False, True),
        (503, False, True),
        (None, False, True),
    ],
)
def test_post_result_flags(status: int | None, ok: bool, retryable: bool) -> None:
    result = PostResult(status=status)
    assert (result.ok, result.retryable) == (ok, retryable)


def test_subscribed_kinds_drops_notification_sent() -> None:
    settings = SlackSettings(events=["notification_sent", "blocked", "state_changed"])
    assert subscribed_kinds(settings) == frozenset({"blocked", "state_changed"})
    assert subscribed_kinds(SlackSettings(events=[])) == frozenset()


# --- the sink --------------------------------------------------------------------------


class FakePoster:
    def __init__(self, *results: PostResult) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []
        self.block: asyncio.Event | None = None
        self.raise_first = False

    async def __call__(self, url: str, payload: bytes, *, timeout_s: float) -> PostResult:
        self.calls.append({"url": url, "text": json.loads(payload)["text"], "timeout_s": timeout_s})
        if self.raise_first:
            self.raise_first = False
            raise RuntimeError("poster bug")
        if self.block is not None:
            await self.block.wait()
        return self.results.pop(0) if self.results else PostResult(status=200)

    @property
    def texts(self) -> list[str]:
        return [call["text"] for call in self.calls]


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(0)


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


@dataclass
class Rig:
    sink: SlackSink
    poster: FakePoster
    sleep: FakeSleep
    recorder: Recorder
    bus: EventBus
    stream: io.StringIO

    @property
    def log_lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]

    def logged(self, event: str) -> list[dict[str, Any]]:
        return [line for line in self.log_lines if line["event"] == event]


def make_rig(*results: PostResult, events: list[str] | None = None) -> Rig:
    stream = io.StringIO()
    configure_logging(fmt="json", level="DEBUG", stream=stream)  # type: ignore[arg-type]
    settings = SlackSettings(
        webhook_url=URL,  # type: ignore[arg-type]
        events=["state_changed", "blocked"] if events is None else events,
    )
    poster, sleep, recorder = FakePoster(*results), FakeSleep(), Recorder()
    sink = SlackSink(settings, repo="example/repo", labels=GitHubLabels(), post=poster, sleep=sleep)
    bus = EventBus([recorder, sink])
    return Rig(sink, poster, sleep, recorder, bus, stream)


def blocked(number: int = 42) -> Blocked:
    return Blocked(issue_number=number, issue_identifier=f"repo-{number}", reason="budget")


def claim(number: int = 42) -> StateChanged:
    return StateChanged(
        issue_number=number,
        issue_identifier=f"repo-{number}",
        from_label="issuebot/todo",
        to_label="issuebot/in-progress",
        actor="issuebot",
    )


async def settle(turns: int = 40) -> None:
    """Let the drain task run: each post and each fake sleep costs a loop turn."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def test_handle_filters_by_kind_and_formats() -> None:
    rig = make_rig(events=["state_changed"])
    rig.bus.publish(claim())
    rig.bus.publish(blocked())
    rig.bus.publish(
        NotificationSent(
            issue_number=42, issue_identifier="repo-42", channel="slack", about_kind="blocked"
        )
    )
    rig.bus.publish(Event())
    rig.sink.start(rig.bus)
    await rig.sink.close()
    assert rig.poster.texts == [
        ":hammer_and_wrench: <https://github.com/example/repo/issues/42|repo-42> "
        "`issuebot/todo` → `issuebot/in-progress` by issuebot"
    ]
    assert rig.poster.calls[0]["url"] == URL
    assert rig.poster.calls[0]["timeout_s"] == slack_module.POST_TIMEOUT_S


async def test_notification_sent_is_never_subscribed() -> None:
    rig = make_rig(events=["notification_sent", "blocked"])
    assert rig.sink.kinds == frozenset({"blocked"})
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await settle()
    assert len(rig.poster.calls) == 1
    assert rig.recorder.kinds == ["blocked", "notification_sent"]
    await settle()
    assert len(rig.poster.calls) == 1
    await rig.sink.close()


async def test_delivery_publishes_notification_sent_about_the_kind() -> None:
    rig = make_rig()
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await settle()
    sent = rig.recorder.events[-1]
    assert isinstance(sent, NotificationSent)
    assert (sent.issue_number, sent.issue_identifier) == (42, "repo-42")
    assert (sent.channel, sent.about_kind) == ("slack", "blocked")
    assert (rig.sink.sent, rig.sink.failed, rig.sink.dropped) == (1, 0, 0)
    (line,) = rig.logged("slack_notification_sent")
    assert (line["kind"], line["issue_number"], line["attempt"]) == ("blocked", 42, 1)
    await rig.sink.close()
    assert "secret" not in rig.stream.getvalue()


async def test_events_before_start_are_buffered_and_delivered_in_order() -> None:
    rig = make_rig()
    rig.bus.publish(claim(1))
    rig.bus.publish(blocked(2))
    rig.bus.publish(claim(3))
    assert rig.poster.calls == []
    rig.sink.start(rig.bus)
    await rig.sink.close()
    assert [text.split("|")[1].split(">")[0] for text in rig.poster.texts] == [
        "repo-1",
        "repo-2",
        "repo-3",
    ]
    assert rig.sink.sent == 3
    (closed,) = rig.logged("slack_sink_closed")
    assert (closed["sent"], closed["failed"], closed["dropped"]) == (3, 0, 0)


async def test_429_waits_for_retry_after_capped() -> None:
    rig = make_rig(
        PostResult(status=429, retry_after_s=2.0),
        PostResult(status=429, retry_after_s=90.0),
        PostResult(status=200),
    )
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await rig.sink.close()
    assert rig.sleep.delays == [2.0, slack_module.RETRY_AFTER_CAP_S]
    assert len(rig.poster.calls) == 3
    assert (rig.sink.sent, rig.sink.failed) == (1, 0)
    retries = rig.logged("slack_post_retry")
    assert [(line["attempt"], line["status"], line["delay_s"]) for line in retries] == [
        (1, 429, 2.0),
        (2, 429, 30.0),
    ]


async def test_429_without_retry_after_uses_the_backoff() -> None:
    rig = make_rig(PostResult(status=429), PostResult(status=200))
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await rig.sink.close()
    assert rig.sleep.delays == [1.0]
    assert rig.sink.sent == 1


async def test_server_and_network_errors_back_off_then_give_up() -> None:
    rig = make_rig(
        PostResult(status=500, error="HTTP Error 500: Internal Server Error"),
        PostResult(status=None, error="ConnectionRefusedError: refused"),
        PostResult(status=503, error="HTTP Error 503: Service Unavailable"),
    )
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await rig.sink.close()
    assert rig.sleep.delays == list(slack_module.RETRY_DELAYS_S)
    assert len(rig.poster.calls) == 3
    assert (rig.sink.sent, rig.sink.failed) == (0, 1)
    assert rig.recorder.kinds == ["blocked"]
    (failed,) = rig.logged("slack_notification_failed")
    assert (failed["level"], failed["attempts"], failed["status"]) == ("warning", 3, 503)
    assert failed["error"] == "HTTP Error 503: Service Unavailable"


async def test_other_4xx_is_permanent() -> None:
    rig = make_rig(PostResult(status=400, error="HTTP Error 400: Bad Request"))
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await rig.sink.close()
    assert rig.sleep.delays == []
    assert len(rig.poster.calls) == 1
    assert (rig.sink.sent, rig.sink.failed) == (0, 1)
    (failed,) = rig.logged("slack_notification_failed")
    assert (failed["attempts"], failed["status"]) == (1, 400)


async def test_full_queue_drops_and_keeps_delivering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_module, "QUEUE_LIMIT", 2)
    rig = make_rig()
    for number in (1, 2, 3):
        rig.bus.publish(claim(number))
    assert rig.sink.dropped == 1
    (line,) = rig.logged("slack_queue_full")
    assert (line["level"], line["issue_number"], line["limit"]) == ("warning", 3, 2)
    rig.sink.start(rig.bus)
    await rig.sink.close()
    assert len(rig.poster.calls) == 2
    assert (rig.sink.sent, rig.sink.dropped) == (2, 1)


async def test_close_drains_then_drops_later_events() -> None:
    rig = make_rig()
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await rig.sink.close()
    assert rig.sink._task is not None and rig.sink._task.done()
    assert len(rig.poster.calls) == 1
    rig.bus.publish(blocked())
    await settle()
    assert len(rig.poster.calls) == 1
    assert rig.sink.dropped == 1
    (dropped,) = rig.logged("slack_sink_closed_drop")
    assert dropped["level"] == "debug"


async def test_close_times_out_on_a_hanging_poster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_module, "DRAIN_TIMEOUT_S", 0.05)
    rig = make_rig()
    rig.poster.block = asyncio.Event()
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked(1))
    rig.bus.publish(blocked(2))
    await settle()
    await rig.sink.close()
    assert rig.sink._task is not None and rig.sink._task.cancelled()
    assert (rig.sink.sent, rig.sink.failed, rig.sink.dropped) == (0, 1, 1)
    (timeout,) = rig.logged("slack_drain_timeout")
    assert (timeout["level"], timeout["left"]) == ("warning", 1)
    (cancelled,) = rig.logged("slack_delivery_cancelled")
    assert (cancelled["level"], cancelled["issue_number"]) == ("warning", 1)
    (closed,) = rig.logged("slack_sink_closed")
    assert (closed["sent"], closed["failed"], closed["dropped"]) == (0, 1, 1)


async def test_start_twice_raises_and_close_before_start_is_a_noop() -> None:
    rig = make_rig()
    await rig.sink.close()
    assert rig.logged("slack_sink_closed") == []
    rig.sink.start(rig.bus)
    with pytest.raises(RuntimeError, match="already started"):
        rig.sink.start(rig.bus)
    await rig.sink.close()


async def test_close_twice_is_a_noop() -> None:
    rig = make_rig()
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await rig.sink.close()
    await rig.sink.close()
    assert len(rig.poster.calls) == 1
    assert len(rig.logged("slack_sink_closed")) == 1


async def test_poster_exception_is_logged_and_the_loop_continues() -> None:
    rig = make_rig()
    rig.poster.raise_first = True
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked(1))
    rig.bus.publish(blocked(2))
    await rig.sink.close()
    assert len(rig.poster.calls) == 2
    assert (rig.sink.sent, rig.sink.failed) == (1, 1)
    (crashed,) = rig.logged("slack_deliver_crashed")
    assert (crashed["level"], crashed["issue_number"]) == ("error", 1)
    assert "poster bug" in crashed["exception"]


def test_sink_requires_a_webhook() -> None:
    with pytest.raises(ValueError, match="webhook_url"):
        SlackSink(SlackSettings(), repo="example/repo", labels=GitHubLabels())


async def test_sink_posts_to_a_local_server(server: ScriptedServer) -> None:
    settings = SlackSettings(webhook_url=server.url)  # type: ignore[arg-type]
    sink = SlackSink(settings, repo="example/repo", labels=GitHubLabels())
    bus = EventBus([sink])
    sink.start(bus)
    bus.publish(
        RunStarted(
            issue_number=42,
            issue_identifier="repo-42",
            run_id="run-1",
            attempt=1,
            session_id=None,
            workspace_path="/w",
        )
    )
    bus.publish(claim())
    await sink.close()
    assert sink.sent == 1
    (request,) = server.received
    assert json.loads(request.body)["text"].startswith(":hammer_and_wrench: ")
