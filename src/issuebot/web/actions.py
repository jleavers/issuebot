"""The dashboard's GitHub Actions minutes poller, and the Slack alert it posts (spec 2026-10-02).

Runs in the hub's web process and nowhere else: the token it holds is the billing account's own,
a classic token with the ``user`` scope, and the web runs no session. One cycle at start, then
one an hour: learn the token's login once, read the month's summary, upsert the account's row,
and post an alert when the reading reaches a threshold not yet posted this month. The row is the
alert's memory, claimed before the post and given back if the post fails, so a restart -- which
every upgrade is -- never posts twice and a failed post is retried an hour later. A cancellation
(the web shutting down) mid-claim or mid-post leaves the claim held, which keeps delivery at
most once: that threshold may go unposted this month, but it is never posted twice.
"""

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from pydantic import SecretStr

from issuebot.db.errors import DatabaseError
from issuebot.github.billing import (
    ActionsUsage,
    describe_failure,
    fetch_actions_usage,
    fetch_login,
    next_period,
)
from issuebot.github.errors import GitHubError
from issuebot.github.runner import GhRunnerLike
from issuebot.log import get_logger
from issuebot.notifications.messages import format_actions_alert
from issuebot.notifications.slack import POST_TIMEOUT_S, Poster, slack_payload, urllib_post

TOKEN_ENV = "ISSUEBOT_GITHUB_BILLING_TOKEN"
ALLOWANCE_ENV = "ISSUEBOT_ACTIONS_INCLUDED_MINUTES"
WEBHOOK_ENV = "SLACK_WEBHOOK_URL"
POLL_INTERVAL_S = 3600.0
# Percent of the allowance, each posted at most once a month. Constants, as SlackSink's retry
# policy is: a deployment that wants others is a code change, not a setting.
THRESHOLDS: tuple[int, ...] = (75, 90, 100)


@dataclass(frozen=True, slots=True)
class ActionsSettings:
    token: SecretStr
    included_minutes: int | None
    webhook_url: SecretStr | None


def actions_settings(environ: Mapping[str, str]) -> ActionsSettings | None:
    """The poller's settings, or None when the token is unset and the feature is off.

    A malformed allowance raises ``ValueError`` whether or not the token is set: it is an
    operator's mistake, and refusing to start names it where a dash on the tile would not.
    """
    raw = environ.get(ALLOWANCE_ENV, "").strip()
    included: int | None = None
    if raw:
        try:
            included = int(raw)
        except ValueError:
            included = 0
        if included <= 0:
            raise ValueError(
                f"{ALLOWANCE_ENV} must be a positive whole number of minutes, such as 3000"
            )
    token = environ.get(TOKEN_ENV, "").strip()
    if not token:
        return None
    webhook = environ.get(WEBHOOK_ENV, "").strip()
    return ActionsSettings(
        token=SecretStr(token),
        included_minutes=included,
        webhook_url=SecretStr(webhook) if webhook else None,
    )


def alert_threshold(used: int, included: int) -> int:
    """The highest threshold ``used`` whole minutes have reached; 0 for none."""
    return max((t for t in THRESHOLDS if used * 100 >= t * included), default=0)


class ActionsStore(Protocol):
    """The poller's writes; ``issuebot.db.Database`` is the real one."""

    async def record_actions_reading(
        self,
        *,
        account: str,
        period: date,
        used_minutes: float,
        included_minutes: int | None,
        observed_at: datetime,
    ) -> int | None: ...

    async def record_actions_error(
        self, *, account: str, error: str, error_at: datetime
    ) -> None: ...

    async def claim_actions_alert(self, *, account: str, period: date, target: int) -> bool: ...

    async def release_actions_alert(
        self, *, account: str, period: date, target: int, previous: int
    ) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ActionsPoller:
    """One cycle at start, then one every ``interval_s``; ``poll_once`` is the test seam."""

    def __init__(
        self,
        settings: ActionsSettings,
        *,
        store: ActionsStore,
        runner: GhRunnerLike,
        post: Poster = urllib_post,
        now: Callable[[], datetime] = _utcnow,
        interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._settings = settings
        self._store = store
        self._runner = runner
        self._post = post
        self._now = now
        self._interval_s = interval_s
        self._account: str | None = None
        self._failure: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger(__name__)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="actions-minutes")

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
                self._log.warning("actions_minutes_store_failed", error=exc.message)
            except Exception:
                # Never let the poller die quietly: the next cycle is the retry.
                self._log.exception("actions_minutes_poll_crashed")
            await asyncio.sleep(self._interval_s)

    async def poll_once(self) -> None:
        if self._account is None:
            try:
                self._account = await fetch_login(self._runner)
            except GitHubError as exc:
                self._failed(exc)
                return
            # Which account is read is the first thing to check when no window appears: a token
            # of any other account than the repositories' owner draws nothing anywhere.
            self._log.info("actions_minutes_account", account=self._account)
        account = self._account
        try:
            usage = await fetch_actions_usage(self._runner, account)
        except GitHubError as exc:
            self._failed(exc)
            await self._store.record_actions_error(
                account=account, error=describe_failure(exc), error_at=self._now()
            )
            return
        self._recovered()
        now = self._now()
        alerted = await self._store.record_actions_reading(
            account=account,
            period=usage.period,
            used_minutes=usage.used_minutes,
            included_minutes=self._settings.included_minutes,
            observed_at=now,
        )
        if alerted is None:
            self._log.info(
                "actions_minutes_older_period_ignored",
                account=account,
                period=usage.period.isoformat(),
            )
            return
        current = now.astimezone(UTC)
        if usage.period < date(current.year, current.month, 1):
            # GitHub still answering for a month that has ended: the reading is kept, but an
            # alert would say "until 1 Nov" on 1 Nov while the window already reads 0%.
            return
        await self._alert(account, usage, alerted)

    async def _alert(self, account: str, usage: ActionsUsage, alerted: int) -> None:
        included = self._settings.included_minutes
        webhook = self._settings.webhook_url
        if included is None or webhook is None:
            return
        used = round(usage.used_minutes)
        target = alert_threshold(used, included)
        if target <= alerted:
            return
        claimed = await self._store.claim_actions_alert(
            account=account, period=usage.period, target=target
        )
        if not claimed:
            return
        text = format_actions_alert(
            account=account, used=used, included=included, resets_on=next_period(usage.period)
        )
        result = await self._post(
            webhook.get_secret_value(), slack_payload(text), timeout_s=POST_TIMEOUT_S
        )
        if result.ok:
            self._log.info(
                "actions_minutes_alert_sent",
                account=account,
                threshold=target,
                used=used,
                included=included,
            )
            return
        # Logged before the claim is given back, so a database error on the release cannot
        # hide the failed post.
        self._log.warning(
            "actions_minutes_alert_failed",
            account=account,
            threshold=target,
            status=result.status,
            error=result.error,
        )
        await self._store.release_actions_alert(
            account=account, period=usage.period, target=target, previous=alerted
        )

    def _failed(self, exc: GitHubError) -> None:
        """Log a failure when it starts or changes, not on every hourly cycle."""
        description = describe_failure(exc)
        if description == self._failure:
            return
        self._failure = description
        token = self._settings.token.get_secret_value()
        self._log.warning(
            "actions_minutes_failed",
            category=exc.category,
            error=description,
            detail=exc.message.replace(token, "***"),
        )

    def _recovered(self) -> None:
        if self._failure is not None:
            self._failure = None
            self._log.info("actions_minutes_recovered")
