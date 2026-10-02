"""The Actions minutes poller and its Slack alert, against a fake gh, store and webhook."""

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from structlog.testing import capture_logs

from fakes.database import FakeDatabase
from fakes.web import PASSWORD
from issuebot.db import StoreUnavailableError
from issuebot.github.runner import GhResult
from issuebot.notifications.slack import PostResult
from issuebot.web import create_app
from issuebot.web.actions import (
    ALLOWANCE_ENV,
    TOKEN_ENV,
    WEBHOOK_ENV,
    ActionsPoller,
    ActionsSettings,
    actions_settings,
    alert_threshold,
)

NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
OCT = date(2026, 10, 1)
NOV = date(2026, 11, 1)
TOKEN = "ghp_billingtokenfortests000000000000"
WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"
LOGIN = GhResult(returncode=0, stdout=json.dumps({"login": "jleavers"}), stderr="")


def summary(used: float, *, number: int = 10) -> GhResult:
    document = {
        "timePeriod": {"year": 2026, "month": number},
        "usageItems": [{"unitType": "minutes", "grossQuantity": used}],
    }
    return GhResult(returncode=0, stdout=json.dumps(document), stderr="")


def rejected(stderr: str = "gh: Bad credentials (HTTP 401)") -> GhResult:
    return GhResult(returncode=1, stdout="", stderr=stderr)


class Runner:
    """Answers /user with ``login`` and each summary call with the next queued result; the last
    one repeats, so a loop that runs on does not exhaust it."""

    def __init__(self, *summaries: GhResult, login: GhResult = LOGIN) -> None:
        self.login = login
        self.summaries = list(summaries)
        self.calls: list[list[str]] = []

    async def run(self, args: Sequence[str], *, stdin: str | None = None) -> GhResult:
        self.calls.append(list(args))
        if args[1] == "/user":
            return self.login
        return self.summaries.pop(0) if len(self.summaries) > 1 else self.summaries[0]


class Store:
    """The table's semantics in memory, one account: an older month is ignored, a new one
    resets the memory, and the claim is conditional."""

    def __init__(self) -> None:
        self.period: date | None = None
        self.used: float | None = None
        self.alerted = 0
        self.error: str | None = None
        self.claims: list[int] = []

    async def record_actions_reading(
        self,
        *,
        account: str,
        period: date,
        used_minutes: float,
        included_minutes: int | None,
        observed_at: datetime,
    ) -> int | None:
        if self.period is not None and period < self.period:
            return None
        if period != self.period:
            self.alerted = 0
        self.period, self.used, self.error = period, used_minutes, None
        return self.alerted

    async def record_actions_error(self, *, account: str, error: str, error_at: datetime) -> None:
        self.error = error

    async def claim_actions_alert(self, *, account: str, period: date, target: int) -> bool:
        if period != self.period or self.alerted >= target:
            return False
        self.alerted = target
        self.claims.append(target)
        return True

    async def release_actions_alert(
        self, *, account: str, period: date, target: int, previous: int
    ) -> None:
        if period == self.period and self.alerted == target:
            self.alerted = previous


class Webhook:
    def __init__(self, status: int | None = 200) -> None:
        self.status = status
        self.texts: list[str] = []

    async def __call__(self, url: str, payload: bytes, *, timeout_s: float) -> PostResult:
        assert url == WEBHOOK
        self.texts.append(json.loads(payload)["text"])
        ok = self.status == 200
        return PostResult(status=self.status, error=None if ok else "HTTP Error 500")


def settings(*, included: int | None = 3000, webhook: str | None = WEBHOOK) -> ActionsSettings:
    return ActionsSettings(
        token=SecretStr(TOKEN),
        included_minutes=included,
        webhook_url=SecretStr(webhook) if webhook else None,
    )


def make(
    runner: Runner,
    store: Store,
    webhook: Webhook | None = None,
    *,
    config: ActionsSettings | None = None,
    interval_s: float = 3600.0,
) -> ActionsPoller:
    return ActionsPoller(
        config or settings(),
        store=store,
        runner=runner,
        post=webhook or Webhook(),
        now=lambda: NOW,
        interval_s=interval_s,
    )


# --- settings ----------------------------------------------------------------------------------


def test_no_token_turns_the_feature_off() -> None:
    assert actions_settings({ALLOWANCE_ENV: "3000", WEBHOOK_ENV: WEBHOOK}) is None


def test_settings_carry_the_three_values() -> None:
    config = actions_settings({TOKEN_ENV: TOKEN, ALLOWANCE_ENV: "3000", WEBHOOK_ENV: WEBHOOK})
    assert config is not None
    assert config.token.get_secret_value() == TOKEN
    assert config.included_minutes == 3000
    assert config.webhook_url is not None and config.webhook_url.get_secret_value() == WEBHOOK


def test_settings_strip_what_an_env_file_leaves_around_a_value() -> None:
    config = actions_settings(
        {TOKEN_ENV: f" {TOKEN}\n", ALLOWANCE_ENV: " 3000 ", WEBHOOK_ENV: "  "}
    )
    assert config is not None
    assert (config.token.get_secret_value(), config.included_minutes, config.webhook_url) == (
        TOKEN,
        3000,
        None,
    )


def test_an_unset_allowance_is_none_not_a_guess() -> None:
    config = actions_settings({TOKEN_ENV: TOKEN})
    assert config is not None and config.included_minutes is None


@pytest.mark.parametrize("value", ["three thousand", "0", "-5", "3000.5"])
def test_a_malformed_allowance_raises_even_without_a_token(value: str) -> None:
    with pytest.raises(ValueError, match=ALLOWANCE_ENV):
        actions_settings({ALLOWANCE_ENV: value})


@pytest.mark.parametrize(
    ("used", "threshold"),
    [(0, 0), (2249, 0), (2250, 75), (2699, 75), (2700, 90), (3000, 100), (9000, 100)],
)
def test_alert_threshold(used: int, threshold: int) -> None:
    assert alert_threshold(used, 3000) == threshold


# --- a cycle -----------------------------------------------------------------------------------


async def test_a_cycle_stores_the_month_s_reading() -> None:
    store = Store()
    await make(Runner(summary(2306.0)), store).poll_once()
    assert (store.period, store.used, store.error) == (OCT, 2306.0, None)


async def test_the_login_is_resolved_once() -> None:
    runner = Runner(summary(10.0))
    poller = make(runner, Store())
    await poller.poll_once()
    await poller.poll_once()
    assert [call[1] for call in runner.calls].count("/user") == 1


async def test_the_account_read_is_logged_once() -> None:
    """A token minted on the wrong account draws no window anywhere; this line says why."""
    with capture_logs() as logs:
        poller = make(Runner(summary(10.0)), Store())
        await poller.poll_once()
        await poller.poll_once()
    accounts = [entry for entry in logs if entry["event"] == "actions_minutes_account"]
    assert [entry["account"] for entry in accounts] == ["jleavers"]


async def test_a_failed_read_records_the_error_and_keeps_the_reading() -> None:
    store = Store()
    runner = Runner(summary(2306.0), rejected())
    poller = make(runner, store)
    await poller.poll_once()
    await poller.poll_once()
    assert store.used == 2306.0
    assert store.error == "token rejected (401): it may have expired or been revoked"


async def test_a_failed_login_writes_nothing_and_the_next_cycle_retries() -> None:
    store = Store()
    runner = Runner(summary(10.0), login=rejected())
    poller = make(runner, store)
    await poller.poll_once()
    assert (store.period, store.error) == (None, None)
    runner.login = LOGIN
    await poller.poll_once()
    assert store.used == 10.0


async def test_a_failure_is_logged_when_it_starts_and_when_it_clears() -> None:
    with capture_logs() as logs:
        poller = make(Runner(rejected(), rejected(), rejected(), summary(10.0)), Store())
        for _ in range(4):
            await poller.poll_once()
    events = [entry["event"] for entry in logs if entry["event"].startswith("actions_minutes_")]
    assert events.count("actions_minutes_failed") == 1
    assert events.count("actions_minutes_recovered") == 1


async def test_the_token_never_reaches_a_log_line() -> None:
    with capture_logs() as logs:
        poller = make(Runner(rejected(f"gh: Bad credentials for {TOKEN} (HTTP 401)")), Store())
        await poller.poll_once()
    assert TOKEN not in repr(logs)


# --- the alert ---------------------------------------------------------------------------------


async def test_crossing_a_threshold_posts_once() -> None:
    store, webhook = Store(), Webhook()
    poller = make(Runner(summary(2306.0), summary(2310.0)), store, webhook)
    await poller.poll_once()
    await poller.poll_once()
    assert len(webhook.texts) == 1 and webhook.texts[0].startswith(":warning:")
    assert "(77%)" in webhook.texts[0] and store.alerted == 75


async def test_a_jump_past_several_thresholds_posts_the_highest_alone() -> None:
    store, webhook = Store(), Webhook()
    await make(Runner(summary(2950.0)), store, webhook).poll_once()
    assert store.claims == [90] and len(webhook.texts) == 1 and "(98%)" in webhook.texts[0]


async def test_the_allowance_used_up_posts_the_last_alert() -> None:
    webhook = Webhook()
    await make(Runner(summary(3012.0)), Store(), webhook).poll_once()
    assert webhook.texts[0].startswith(":rotating_light:") and "until 1 Nov" in webhook.texts[0]


async def test_a_restart_above_a_threshold_does_not_post_again() -> None:
    store, webhook = Store(), Webhook()
    await make(Runner(summary(2306.0)), store, webhook).poll_once()
    await make(Runner(summary(2320.0)), store, webhook).poll_once()  # a new process, same table
    assert len(webhook.texts) == 1


async def test_a_failed_post_releases_the_claim_and_the_next_cycle_retries() -> None:
    store, webhook = Store(), Webhook(status=500)
    poller = make(Runner(summary(2306.0)), store, webhook)
    with capture_logs() as logs:
        await poller.poll_once()
    assert store.alerted == 0 and len(webhook.texts) == 1
    assert any(entry["event"] == "actions_minutes_alert_failed" for entry in logs)
    webhook.status = 200
    await poller.poll_once()
    assert store.alerted == 75 and len(webhook.texts) == 2


async def test_an_older_month_from_github_posts_nothing() -> None:
    store, webhook = Store(), Webhook()
    store.period, store.used, store.alerted = NOV, 10.0, 0
    await make(Runner(summary(3012.0, number=10)), store, webhook).poll_once()
    assert (store.period, store.used, webhook.texts) == (NOV, 10.0, [])


async def test_a_new_month_posts_its_thresholds_afresh() -> None:
    store, webhook = Store(), Webhook()
    store.period, store.used, store.alerted = OCT, 2950.0, 90
    await make(Runner(summary(2306.0, number=11)), store, webhook).poll_once()
    assert len(webhook.texts) == 1 and webhook.texts[0].startswith(":warning:")
    assert "(77%)" in webhook.texts[0]
    assert (store.alerted, store.period) == (75, NOV)


@pytest.mark.parametrize(
    "config", [settings(webhook=None), settings(included=None)], ids=["no-webhook", "no-allowance"]
)
async def test_without_a_webhook_or_an_allowance_nothing_is_claimed_or_posted(
    config: ActionsSettings,
) -> None:
    store, webhook = Store(), Webhook()
    await make(Runner(summary(3012.0)), store, webhook, config=config).poll_once()
    assert store.used == 3012.0 and store.claims == [] and webhook.texts == []


# --- the loop and the app ----------------------------------------------------------------------


async def test_start_runs_a_cycle_and_stop_ends_the_loop() -> None:
    store = Store()
    poller = make(Runner(summary(10.0)), store)
    poller.start()
    poller.start()  # a second start is a no-op, not a second loop
    for _ in range(50):
        if store.used is not None:
            break
        await asyncio.sleep(0)
    assert store.used == 10.0
    await poller.stop()
    await poller.stop()


async def test_a_database_error_is_logged_and_the_next_cycle_retries() -> None:
    class Flaky(Store):
        failures = 1

        async def record_actions_reading(self, **kwargs: object) -> int | None:  # type: ignore[override]
            if self.failures:
                self.failures -= 1
                raise StoreUnavailableError("cannot connect: refused")
            return await super().record_actions_reading(**kwargs)  # type: ignore[arg-type]

    store = Flaky()
    with capture_logs() as logs:
        poller = make(Runner(summary(10.0)), store, interval_s=0)
        poller.start()
        for _ in range(100):
            if store.used is not None:
                break
            await asyncio.sleep(0)
        await poller.stop()
    assert store.used == 10.0
    assert any(entry["event"] == "actions_minutes_store_failed" for entry in logs)


class FakePoller:
    def __init__(self) -> None:
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    async def stop(self) -> None:
        self.events.append("stop")


def test_the_app_starts_and_stops_the_poller_with_its_lifespan() -> None:
    poller = FakePoller()
    app = create_app(FakeDatabase(), password=PASSWORD, actions=poller)  # type: ignore[arg-type]
    assert app.state.actions is poller
    with TestClient(app):
        assert poller.events == ["start"]
    assert poller.events == ["start", "stop"]


def test_an_app_without_a_poller_has_none() -> None:
    assert create_app(FakeDatabase(), password=PASSWORD).state.actions is None
