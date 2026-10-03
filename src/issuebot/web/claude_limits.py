"""The Claude usage limits watcher, and the Slack alert it posts (spec 2026-10-02,
claude-limits-alert).

Runs in the hub's web process beside the Actions minutes poller, whenever the web has
``SLACK_WEBHOOK_URL``. Every worker writes its newest Claude usage reading and its dispatch hold
into its repository's ``runtime_snapshot`` row; once a minute this reads every row -- one query,
no API call, no credential but the webhook -- and posts when the 7-day window passes 75% or 90%,
or when a worker holds dispatch because claude refused a turn on usage.

``claude_limit_alerts`` is the alert's memory, one row per window instance: claude's window name
and the moment it reopens. One subscription reports one reset to every worker, so every worker
on one wall posts once, and a restart -- which every upgrade is -- finds what was already posted.
A claim is taken before the post and given back if the post fails; a cancellation (shutdown)
between the two leaves the claim held, which keeps delivery at most once. So does a release
that itself fails after a failed post (logged ``claude_limits_alert_failed``, then
``claude_limits_store_failed``): that threshold is not posted for that window.
"""

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import SecretStr

from issuebot.db.errors import DatabaseError
from issuebot.db.queries import SnapshotRow
from issuebot.log import get_logger
from issuebot.notifications.messages import format_claude_limit_alert
from issuebot.notifications.slack import POST_TIMEOUT_S, Poster, slack_payload, urllib_post
from issuebot.web.actions import WEBHOOK_ENV
from issuebot.web.views import parse_moment

INTERVAL_S = 60.0
# A failed post is tried again no sooner than this, so a dead webhook is not called every minute.
RETRY_BACKOFF_S = 900.0
SEVEN_DAY = "seven_day"
# Percent of the 7-day window, each posted at most once per window. The 5-hour window has no
# warnings -- it resets several times a day -- and neither has a 100% one: claude refusing is
# the hit, and the hit is posted from the hold. Constants, as SlackSink's retry policy is.
SEVEN_DAY_THRESHOLDS: tuple[int, ...] = (75, 90)
HIT_PERCENT = 100
# The key's window for a refusal claude did not name.
UNKNOWN_WINDOW = "unknown"


def webhook_setting(environ: Mapping[str, str]) -> SecretStr | None:
    """The webhook the alert posts to, or None when it is unset and the watcher is off."""
    webhook = environ.get(WEBHOOK_ENV, "").strip()
    return SecretStr(webhook) if webhook else None


@dataclass(frozen=True, slots=True)
class Alert:
    """One alert the snapshots call for: the memory's key, the claim's target, the figure shown."""

    limit_window: str  # seven_day for a warning; the window claude refused on, or "unknown"
    resets_at: datetime  # when that window reopens: which instance of it
    target: int  # 75 or 90 for a warning, 100 for a hit
    percent: int  # what the message says: the utilisation rounded, or 100 for a hit


def due_alerts(snapshots: Mapping[str, SnapshotRow], now: datetime) -> list[Alert]:
    """The alerts the workers' snapshots call for at ``now``, before the memory is asked.

    Total over the JSON, as ``views`` is: a field it cannot read is no reading and no hold. A
    reading or a hold whose window has already reopened is history, not news.
    """
    highest: dict[datetime, float] = {}
    walls: dict[tuple[str, datetime], None] = {}
    for row in snapshots.values():
        reading = _seven_day(row.data)
        if reading is not None and reading[1] > now:
            utilization, resets_at = reading
            # One subscription is one key, and use only rises within a window: the highest
            # reading is the newest, whichever worker saw it.
            highest[resets_at] = max(utilization, highest.get(resets_at, 0.0))
        wall = _usage_wall(row.data)
        if wall is not None and wall[1] > now:
            walls[wall] = None
    alerts = []
    for resets_at, utilization in sorted(highest.items()):
        if (SEVEN_DAY, resets_at) in walls:
            continue  # the 7-day hit for the same window says more
        target = max((t for t in SEVEN_DAY_THRESHOLDS if utilization * 100 >= t), default=0)
        if target:
            percent = round(min(utilization, 1.0) * 100)
            alerts.append(Alert(SEVEN_DAY, resets_at, target, percent))
    alerts.extend(Alert(window, until, HIT_PERCENT, HIT_PERCENT) for window, until in walls)
    return alerts


def _seven_day(data: Mapping[str, Any]) -> tuple[float, datetime] | None:
    limits = data.get("rate_limits")
    window = limits.get(SEVEN_DAY) if isinstance(limits, dict) else None
    if not isinstance(window, dict):
        return None
    utilization = window.get("utilization")
    resets_at = parse_moment(window.get("resets_at"))
    if resets_at is None or isinstance(utilization, bool):
        return None
    if not isinstance(utilization, int | float):
        return None
    return float(utilization), resets_at


def _usage_wall(data: Mapping[str, Any]) -> tuple[str, datetime] | None:
    """A usage hold's window and reset; None for any other hold, or one claude did not date."""
    hold = data.get("dispatch_hold")
    if not isinstance(hold, dict) or hold.get("kind") != "usage":
        return None
    until = parse_moment(hold.get("until"))
    if until is None:
        return None
    window = hold.get("window")
    return (window if isinstance(window, str) and window else UNKNOWN_WINDOW), until


class SnapshotReader(Protocol):
    async def snapshots(self) -> dict[str, SnapshotRow]: ...


class ClaudeLimitsStore(Protocol):
    """What the watcher needs of ``issuebot.db.Database``."""

    def queries(self) -> AbstractAsyncContextManager[SnapshotReader]: ...

    async def claude_limit_alerted(self, *, limit_window: str, resets_at: datetime) -> int: ...

    async def claim_claude_limit_alert(
        self, *, limit_window: str, resets_at: datetime, target: int
    ) -> bool: ...

    async def release_claude_limit_alert(
        self, *, limit_window: str, resets_at: datetime, target: int, previous: int
    ) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ClaudeLimitsWatcher:
    """One cycle at start, then one every ``interval_s``; ``poll_once`` is the test seam."""

    def __init__(
        self,
        webhook_url: SecretStr,
        *,
        store: ClaudeLimitsStore,
        post: Poster = urllib_post,
        now: Callable[[], datetime] = _utcnow,
        interval_s: float = INTERVAL_S,
        backoff_s: float = RETRY_BACKOFF_S,
    ) -> None:
        self._webhook_url = webhook_url
        self._store = store
        self._post = post
        self._now = now
        self._interval_s = interval_s
        self._backoff = timedelta(seconds=backoff_s)
        # Per key, when a failed post may be tried again; in memory, since a restart may retry.
        self._retry_after: dict[tuple[str, datetime], datetime] = {}
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger(__name__)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="claude-limits")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except DatabaseError as exc:
                self._log.warning("claude_limits_store_failed", error=exc.message)
            except Exception:
                # Never let the watcher die quietly: the next cycle is the retry.
                self._log.exception("claude_limits_poll_crashed")
            await asyncio.sleep(self._interval_s)

    async def poll_once(self) -> None:
        now = self._now()
        self._retry_after = {key: at for key, at in self._retry_after.items() if key[1] > now}
        async with self._store.queries() as queries:
            snapshots = await queries.snapshots()
        for alert in due_alerts(snapshots, now):
            await self._alert(alert, now)

    async def _alert(self, alert: Alert, now: datetime) -> None:
        key = (alert.limit_window, alert.resets_at)
        retry_at = self._retry_after.get(key)
        if retry_at is not None and now < retry_at:
            return
        previous = await self._store.claude_limit_alerted(
            limit_window=alert.limit_window, resets_at=alert.resets_at
        )
        if alert.target <= previous:
            return
        claimed = await self._store.claim_claude_limit_alert(
            limit_window=alert.limit_window, resets_at=alert.resets_at, target=alert.target
        )
        if not claimed:
            return
        window = None if alert.limit_window == UNKNOWN_WINDOW else alert.limit_window
        text = format_claude_limit_alert(
            window=window,
            percent=alert.percent,
            hit=alert.target == HIT_PERCENT,
            resets_at=alert.resets_at,
            now=now,
        )
        result = await self._post(
            self._webhook_url.get_secret_value(), slack_payload(text), timeout_s=POST_TIMEOUT_S
        )
        fields = {
            "window": alert.limit_window,
            "resets_at": alert.resets_at.isoformat(),
            "threshold": alert.target,
        }
        if result.ok:
            self._retry_after.pop(key, None)
            self._log.info("claude_limits_alert_sent", **fields)
            return
        # Logged before the release: a store failure there must not hide the failed post.
        self._log.warning(
            "claude_limits_alert_failed", **fields, status=result.status, error=result.error
        )
        self._retry_after[key] = now + self._backoff
        await self._store.release_claude_limit_alert(
            limit_window=alert.limit_window,
            resets_at=alert.resets_at,
            target=alert.target,
            previous=previous,
        )
