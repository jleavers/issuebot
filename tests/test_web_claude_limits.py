"""The Claude usage watcher and its Slack alert, against fake snapshots, store and webhook."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from fakes.web import NOW, limits, snapshot
from issuebot.db import StoreUnavailableError
from issuebot.db.queries import SnapshotRow
from issuebot.notifications.slack import PostResult
from issuebot.orchestrator.state import DispatchHold
from issuebot.web.claude_limits import (
    RETRY_BACKOFF_S,
    ClaudeLimitsWatcher,
    webhook_setting,
)

WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"
# fakes.web's NOW is Friday 4 September 2026, 12:00 UTC; `limits()` resets its 7-day window
# three days later, on Monday 7 September at 12:00.
WEEK_RESET = NOW + timedelta(days=3)
WALL = NOW + timedelta(hours=2, minutes=13)


def usage_hold(until: datetime | None = WALL, window: str | None = "five_hour") -> DispatchHold:
    return DispatchHold(
        kind="usage",
        reason="claude usage limit reached: You've hit your session limit",
        since=NOW - timedelta(minutes=1),
        until=until,
        window=window,
    )


class Store:
    """The table's semantics in memory, and the snapshots the watcher reads."""

    def __init__(self, *rows: SnapshotRow) -> None:
        self.rows = {f"acme/repo-{n}": row for n, row in enumerate(rows)}
        self.alerted: dict[tuple[str, datetime], int] = {}
        self.claims: list[tuple[str, datetime, int]] = []
        self.reads = 0
        self.unreachable = 0
        self.release_fails = False

    def show(self, *rows: SnapshotRow) -> None:
        self.rows = {f"acme/repo-{n}": row for n, row in enumerate(rows)}

    @asynccontextmanager
    async def queries(self) -> AsyncIterator[Store]:
        if self.unreachable:
            self.unreachable -= 1
            raise StoreUnavailableError("cannot connect: refused")
        yield self

    async def snapshots(self) -> dict[str, SnapshotRow]:
        self.reads += 1
        return dict(self.rows)

    async def claude_limit_alerted(self, *, limit_window: str, resets_at: datetime) -> int:
        return self.alerted.get((limit_window, resets_at), 0)

    async def claim_claude_limit_alert(
        self, *, limit_window: str, resets_at: datetime, target: int
    ) -> bool:
        if self.alerted.get((limit_window, resets_at), 0) >= target:
            return False
        self.alerted[(limit_window, resets_at)] = target
        self.claims.append((limit_window, resets_at, target))
        return True

    async def release_claude_limit_alert(
        self, *, limit_window: str, resets_at: datetime, target: int, previous: int
    ) -> None:
        if self.release_fails:
            raise StoreUnavailableError("cannot connect: refused")
        if self.alerted.get((limit_window, resets_at)) == target:
            self.alerted[(limit_window, resets_at)] = previous


class Webhook:
    def __init__(self, status: int | None = 200) -> None:
        self.status = status
        self.texts: list[str] = []

    async def __call__(self, url: str, payload: bytes, *, timeout_s: float) -> PostResult:
        assert url == WEBHOOK
        self.texts.append(json.loads(payload)["text"])
        ok = self.status == 200
        return PostResult(status=self.status, error=None if ok else "HTTP Error 500")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make(
    store: Store,
    webhook: Webhook | None = None,
    clock: Clock | None = None,
    *,
    interval_s: float = 60.0,
) -> ClaudeLimitsWatcher:
    moment = clock or Clock()
    return ClaudeLimitsWatcher(
        SecretStr(WEBHOOK),
        store=store,
        post=webhook or Webhook(),
        now=lambda: moment.now,
        interval_s=interval_s,
    )


# --- the setting -------------------------------------------------------------------------------


def test_the_webhook_turns_the_watcher_on_and_its_absence_off() -> None:
    assert webhook_setting({}) is None
    assert webhook_setting({"SLACK_WEBHOOK_URL": "  "}) is None
    setting = webhook_setting({"SLACK_WEBHOOK_URL": f" {WEBHOOK}\n"})
    assert setting is not None and setting.get_secret_value() == WEBHOOK


# --- 7-day warnings ----------------------------------------------------------------------------


async def test_a_seven_day_reading_past_75_posts_once() -> None:
    store, webhook = Store(snapshot(rate_limits=limits(seven=0.77))), Webhook()
    watcher = make(store, webhook)
    await watcher.poll_once()
    await watcher.poll_once()
    assert webhook.texts == [
        ":warning: Claude: the 7-day usage window is 77% used; it resets Mon 7 Sep, 12:00 UTC."
    ]
    assert store.claims == [("seven_day", WEEK_RESET, 75)]


async def test_90_follows_75_in_the_same_window() -> None:
    store, webhook = Store(snapshot(rate_limits=limits(seven=0.77))), Webhook()
    watcher = make(store, webhook)
    await watcher.poll_once()
    store.show(snapshot(rate_limits=limits(seven=0.91)))
    await watcher.poll_once()
    assert [claim[2] for claim in store.claims] == [75, 90]
    assert "is 91% used" in webhook.texts[1]


async def test_a_jump_past_both_thresholds_posts_90_alone() -> None:
    store, webhook = Store(snapshot(rate_limits=limits(seven=0.95))), Webhook()
    await make(store, webhook).poll_once()
    assert store.claims == [("seven_day", WEEK_RESET, 90)] and len(webhook.texts) == 1


async def test_below_75_posts_nothing_and_the_5_hour_window_never_warns() -> None:
    store, webhook = Store(snapshot(rate_limits=limits(five=0.99, seven=0.74))), Webhook()
    await make(store, webhook).poll_once()
    assert (store.claims, webhook.texts) == ([], [])


async def test_a_new_week_starts_afresh() -> None:
    store, webhook = Store(snapshot(rate_limits=limits(seven=0.80))), Webhook()
    watcher = make(store, webhook)
    await watcher.poll_once()
    store.show(snapshot(rate_limits=limits(seven=0.80, seven_resets_in=timedelta(days=10))))
    await watcher.poll_once()
    assert [claim[1] for claim in store.claims] == [WEEK_RESET, NOW + timedelta(days=10)]


async def test_the_highest_reading_of_one_window_is_the_one_posted() -> None:
    """Review Focus 2: two workers on one subscription, one fresher than the other."""
    store = Store(
        snapshot(rate_limits=limits(seven=0.70, observed_ago=timedelta(days=2))),
        snapshot(rate_limits=limits(seven=0.80)),
    )
    webhook = Webhook()
    await make(store, webhook).poll_once()
    assert store.claims == [("seven_day", WEEK_RESET, 75)]
    assert len(webhook.texts) == 1 and "is 80% used" in webhook.texts[0]


async def test_an_expired_reading_posts_nothing() -> None:
    """Review Focus 3: a stopped worker's reading from a window that has since reset."""
    stale = limits(seven=0.95, seven_resets_in=timedelta(minutes=-1))
    store, webhook = Store(snapshot(rate_limits=stale)), Webhook()
    await make(store, webhook).poll_once()
    assert webhook.texts == []


# --- limit hits --------------------------------------------------------------------------------


async def test_every_worker_on_one_wall_posts_one_hit() -> None:
    """Review Focus 2: every worker on the subscription is refused against the same reset."""
    store = Store(snapshot(dispatch_hold=usage_hold()), snapshot(dispatch_hold=usage_hold()))
    webhook = Webhook()
    await make(store, webhook).poll_once()
    assert webhook.texts == [
        ":rotating_light: Claude: the 5-hour usage limit is reached; issuebot stops claiming "
        "issues until 14:13 UTC (in 2 h 13 min)."
    ]
    assert store.claims == [("five_hour", WALL, 100)]


async def test_a_later_wall_posts_again() -> None:
    store, webhook = Store(snapshot(dispatch_hold=usage_hold())), Webhook()
    watcher = make(store, webhook)
    await watcher.poll_once()
    store.show(snapshot(dispatch_hold=usage_hold(until=WALL + timedelta(hours=5))))
    await watcher.poll_once()
    assert len(webhook.texts) == 2


async def test_an_expired_or_undated_hold_posts_nothing() -> None:
    """Review Focus 1 and 3: a hold claude did not date, and one whose window has reopened."""
    store = Store(
        snapshot(dispatch_hold=usage_hold(until=None)),
        snapshot(dispatch_hold=usage_hold(until=NOW - timedelta(minutes=1))),
    )
    webhook = Webhook()
    await make(store, webhook).poll_once()
    assert (store.claims, webhook.texts) == ([], [])


async def test_a_hold_claude_did_not_name_is_keyed_unknown() -> None:
    store, webhook = Store(snapshot(dispatch_hold=usage_hold(window=None))), Webhook()
    await make(store, webhook).poll_once()
    assert store.claims == [("unknown", WALL, 100)]
    assert webhook.texts[0].startswith(":rotating_light: Claude: the usage limit is reached;")


async def test_a_seven_day_hit_says_so_once_instead_of_a_warning() -> None:
    """The 7-day window's warning and its hit share a key; a cycle that sees both posts the hit."""
    store = Store(
        snapshot(
            rate_limits=limits(seven=1.0),
            dispatch_hold=usage_hold(until=WEEK_RESET, window="seven_day"),
        )
    )
    webhook = Webhook()
    await make(store, webhook).poll_once()
    assert store.claims == [("seven_day", WEEK_RESET, 100)]
    assert len(webhook.texts) == 1 and webhook.texts[0].startswith(":rotating_light:")


async def test_another_kind_of_hold_posts_nothing() -> None:
    held = DispatchHold(kind="auth", reason="not logged in", since=NOW, until=WALL)
    store, webhook = Store(snapshot(dispatch_hold=held)), Webhook()
    await make(store, webhook).poll_once()
    assert webhook.texts == []


async def test_snapshots_it_cannot_read_are_skipped() -> None:
    row = snapshot()
    row.data["rate_limits"] = {"seven_day": {"utilization": "most", "resets_at": "soon"}}
    row.data["dispatch_hold"] = {"kind": "usage", "reason": "x", "until": "not a time"}
    store, webhook = Store(row), Webhook()
    await make(store, webhook).poll_once()
    assert webhook.texts == []


# --- memory and failure ------------------------------------------------------------------------


async def test_a_restart_posts_nothing_new() -> None:
    """Review Focus 4: a second process over the same table."""
    store, webhook = Store(snapshot(rate_limits=limits(seven=0.80))), Webhook()
    await make(store, webhook).poll_once()
    await make(store, webhook).poll_once()
    assert len(webhook.texts) == 1


async def test_a_dead_webhook_is_retried_only_after_the_backoff() -> None:
    """Review Focus 5."""
    store, webhook, clock = Store(snapshot(rate_limits=limits(seven=0.80))), Webhook(500), Clock()
    with capture_logs() as logs:
        watcher = make(store, webhook, clock)
        await watcher.poll_once()
        assert store.alerted[("seven_day", WEEK_RESET)] == 0
        clock.advance(60)
        await watcher.poll_once()
        assert len(webhook.texts) == 1
        clock.advance(RETRY_BACKOFF_S)
        webhook.status = 200
        await watcher.poll_once()
        await watcher.poll_once()
    assert len(webhook.texts) == 2
    assert store.alerted[("seven_day", WEEK_RESET)] == 75
    assert [entry["event"] for entry in logs if entry["event"].startswith("claude_limits_")] == [
        "claude_limits_alert_failed",
        "claude_limits_alert_sent",
    ]
    assert WEBHOOK not in repr(logs)


async def test_a_failed_release_still_logs_the_failed_post() -> None:
    store, webhook = Store(snapshot(rate_limits=limits(seven=0.80))), Webhook(500)
    store.release_fails = True
    with capture_logs() as logs:
        watcher = make(store, webhook)
        with pytest.raises(StoreUnavailableError):
            await watcher.poll_once()
    assert any(entry["event"] == "claude_limits_alert_failed" for entry in logs)


# --- the loop ----------------------------------------------------------------------------------


async def test_start_runs_a_cycle_and_stop_ends_the_loop() -> None:
    store = Store(snapshot(rate_limits=limits(seven=0.80)))
    watcher = make(store, interval_s=3600.0)
    watcher.start()
    watcher.start()  # a second start is a no-op, not a second loop
    for _ in range(50):
        await asyncio.sleep(0)
    await watcher.stop()
    await watcher.stop()
    assert store.reads == 1


async def test_a_database_error_is_logged_and_the_next_cycle_retries() -> None:
    store = Store(snapshot(rate_limits=limits(seven=0.80)))
    store.unreachable = 1
    webhook = Webhook()
    with capture_logs() as logs:
        watcher = make(store, webhook, interval_s=0)
        watcher.start()
        for _ in range(100):
            if webhook.texts:
                break
            await asyncio.sleep(0)
        await watcher.stop()
    assert len(webhook.texts) == 1
    assert any(entry["event"] == "claude_limits_store_failed" for entry in logs)
