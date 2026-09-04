"""Slack incoming-webhook sink: a queue drained by one task, bounded retry, redacted errors."""

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from issuebot.config import GitHubLabels, SlackSettings
from issuebot.events import Event, EventBus, IssueEvent, NotificationSent
from issuebot.log import get_logger
from issuebot.notifications.messages import format_event

QUEUE_LIMIT = 100
MAX_ATTEMPTS = 3
POST_TIMEOUT_S = 10.0
RETRY_DELAYS_S: tuple[float, ...] = (1.0, 4.0)
RETRY_AFTER_CAP_S = 30.0
DRAIN_TIMEOUT_S = 10.0
REDACTED = "<webhook url>"
_SCHEMES = ("http", "https")


@dataclass(frozen=True, slots=True)
class PostResult:
    """What one webhook POST came back with. ``error`` never contains the webhook URL."""

    status: int | None
    retry_after_s: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500


class Poster(Protocol):
    async def __call__(self, url: str, payload: bytes, *, timeout_s: float) -> PostResult: ...


def slack_payload(text: str) -> bytes:
    return json.dumps({"text": text}).encode("utf-8")


def redact(text: str, url: str) -> str:
    """Replace the webhook URL, and its path on its own, with a placeholder."""
    redacted = text.replace(url, REDACTED)
    path = urlsplit(url).path
    if path and path != "/":
        redacted = redacted.replace(path, REDACTED)
    return redacted


def subscribed_kinds(slack: SlackSettings) -> frozenset[str]:
    """The allow-list minus ``notification_sent``, which the sink never notifies about."""
    return frozenset(slack.events) - {NotificationSent.kind}


async def urllib_post(url: str, payload: bytes, *, timeout_s: float = POST_TIMEOUT_S) -> PostResult:
    """POST ``payload`` as JSON with urllib in a worker thread; never raises."""
    scheme = urlsplit(url).scheme
    if scheme not in _SCHEMES:
        return PostResult(status=None, error=f"unsupported URL scheme {scheme!r}")
    return await asyncio.to_thread(_post_blocking, url, payload, timeout_s)


def _post_blocking(url: str, payload: bytes, timeout_s: float) -> PostResult:
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return PostResult(status=response.status)
    except urllib.error.HTTPError as exc:
        return PostResult(
            status=exc.code,
            retry_after_s=_retry_after(exc.headers.get("Retry-After")),
            error=redact(str(exc), url),
        )
    except (OSError, ValueError) as exc:
        return PostResult(status=None, error=redact(f"{type(exc).__name__}: {exc}", url))


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class _Pending:
    event: IssueEvent
    text: str


class SlackSink:
    """Posts subscribed events to a Slack incoming webhook from one background task.

    ``handle`` only formats and enqueues; ``start`` creates the drain task; ``close``
    drains what is queued (bounded) and stops it. Failures are logged and counted.
    """

    name = "slack"

    def __init__(
        self,
        slack: SlackSettings,
        *,
        repo: str,
        labels: GitHubLabels,
        post: Poster = urllib_post,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if slack.webhook_url is None:
            raise ValueError("SlackSink needs notifications.slack.webhook_url")
        self._url = slack.webhook_url.get_secret_value()
        self.kinds = subscribed_kinds(slack)
        self._repo = repo
        self._labels = labels
        self._post = post
        self._sleep = sleep
        self._queue: asyncio.Queue[_Pending | None] = asyncio.Queue()
        self._bus: EventBus | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self._log = get_logger(__name__)

    def handle(self, event: Event) -> None:
        if not isinstance(event, IssueEvent) or event.kind not in self.kinds:
            return
        if self._closed:
            self._log.debug(
                "slack_sink_closed_drop", kind=event.kind, issue_number=event.issue_number
            )
            return
        text = format_event(event, repo=self._repo, labels=self._labels)
        if text is None:
            return
        if self._queue.qsize() >= QUEUE_LIMIT:
            self.dropped += 1
            self._log.warning(
                "slack_queue_full",
                kind=event.kind,
                issue_number=event.issue_number,
                limit=QUEUE_LIMIT,
            )
            return
        self._queue.put_nowait(_Pending(event, text))

    def start(self, bus: EventBus) -> None:
        """Create the drain task on the running loop; ``NotificationSent`` goes to ``bus``."""
        if self._task is not None:
            raise RuntimeError("SlackSink is already started")
        self._bus = bus
        self._task = asyncio.create_task(self._drain(), name="issuebot-slack-sink")
        self._log.info("slack_sink_started", kinds=sorted(self.kinds))

    async def close(self) -> None:
        """Deliver what is queued for at most DRAIN_TIMEOUT_S, then stop the drain task."""
        if self._task is None:
            return
        self._closed = True
        self._queue.put_nowait(None)
        try:
            await asyncio.wait_for(self._task, DRAIN_TIMEOUT_S)
        except TimeoutError:
            self._log.warning(
                "slack_drain_timeout",
                left=max(self._queue.qsize() - 1, 0),
                timeout_s=DRAIN_TIMEOUT_S,
            )
        self._log.info(
            "slack_sink_closed", sent=self.sent, failed=self.failed, dropped=self.dropped
        )

    async def _drain(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            try:
                await self._deliver(item)
            except Exception:
                self.failed += 1
                self._log.exception(
                    "slack_deliver_crashed",
                    kind=item.event.kind,
                    issue_number=item.event.issue_number,
                )

    async def _deliver(self, item: _Pending) -> None:
        event = item.event
        payload = slack_payload(item.text)
        attempt = 0
        result = PostResult(status=None)
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            result = await self._post(self._url, payload, timeout_s=POST_TIMEOUT_S)
            if result.ok:
                self.sent += 1
                self._log.info(
                    "slack_notification_sent",
                    kind=event.kind,
                    issue_number=event.issue_number,
                    attempt=attempt,
                )
                self._publish_sent(event)
                return
            if not result.retryable or attempt == MAX_ATTEMPTS:
                break
            delay = self._retry_delay(result, attempt)
            self._log.warning(
                "slack_post_retry",
                kind=event.kind,
                issue_number=event.issue_number,
                attempt=attempt,
                status=result.status,
                error=result.error,
                delay_s=delay,
            )
            await self._sleep(delay)
        self.failed += 1
        self._log.warning(
            "slack_notification_failed",
            kind=event.kind,
            issue_number=event.issue_number,
            attempts=attempt,
            status=result.status,
            error=result.error,
        )

    @staticmethod
    def _retry_delay(result: PostResult, attempt: int) -> float:
        delay = RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S)) - 1]
        if result.status == 429 and result.retry_after_s is not None:
            delay = result.retry_after_s
        return min(delay, RETRY_AFTER_CAP_S)

    def _publish_sent(self, event: IssueEvent) -> None:
        if self._bus is None:
            return
        self._bus.publish(
            NotificationSent(
                issue_number=event.issue_number,
                issue_identifier=event.issue_identifier,
                channel=self.name,
                about_kind=event.kind,
            )
        )
