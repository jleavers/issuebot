"""The dashboard's billing reads: the summary's arithmetic, the two fetches, a failure's words."""

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from issuebot.github.billing import (
    ActionsUsage,
    describe_failure,
    fetch_actions_usage,
    fetch_login,
    next_period,
    parse_summary,
)
from issuebot.github.errors import GitHubError
from issuebot.github.runner import GhResult

FIXTURE = Path(__file__).parent / "fixtures" / "gh" / "billing_summary.json"
SUMMARY = "/users/jleavers/settings/billing/usage/summary?product=actions"


class StubRunner:
    """Returns the given results in order and records every argument list."""

    def __init__(self, *results: GhResult) -> None:
        self.calls: list[list[str]] = []
        self._results = list(results)

    async def run(self, args: Sequence[str], *, stdin: str | None = None) -> GhResult:
        self.calls.append(list(args))
        return self._results.pop(0)


def ok(document: object) -> GhResult:
    return GhResult(returncode=0, stdout=json.dumps(document), stderr="")


def month(*items: object, year: object = 2026, number: object = 10) -> dict[str, object]:
    return {"timePeriod": {"year": year, "month": number}, "usageItems": list(items)}


def test_the_recorded_summary_sums_to_what_the_billing_page_showed() -> None:
    # 2,192 Linux + 114 Windows: the page read 2,308 a minute later. Storage is measured in
    # gigabyte-hours, not minutes, and is left out.
    usage = parse_summary(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert usage == ActionsUsage(period=date(2026, 10, 1), used_minutes=2306.0)


def test_the_unit_is_compared_in_any_case() -> None:
    document = month(
        {"unitType": "Minutes", "grossQuantity": 5},
        {"unitType": "GigabyteHours", "grossQuantity": 99.5},
    )
    assert parse_summary(document).used_minutes == 5.0


def test_an_empty_month_is_zero() -> None:
    assert parse_summary(month(number=11)) == ActionsUsage(
        period=date(2026, 11, 1), used_minutes=0.0
    )


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"usageItems": []},
        {"timePeriod": {"year": 2026}, "usageItems": []},
        month(number=13),
        month(number=0),
        month(year=True),
        month(year="2026"),
        {"timePeriod": {"year": 2026, "month": 10}},
        month("not an object"),
        month({"unitType": "minutes", "grossQuantity": "5"}),
        month({"unitType": "minutes", "grossQuantity": True}),
    ],
)
def test_a_summary_it_cannot_read_is_a_response_error(document: object) -> None:
    with pytest.raises(GitHubError) as caught:
        parse_summary(document)
    assert caught.value.category == "response"


def test_next_period_is_the_first_of_the_following_month() -> None:
    assert next_period(date(2026, 10, 1)) == date(2026, 11, 1)
    assert next_period(date(2026, 12, 1)) == date(2027, 1, 1)


async def test_fetch_login_reads_whose_token_it_is() -> None:
    runner = StubRunner(ok({"login": "jleavers", "plan": {"name": "pro"}}))
    assert await fetch_login(runner) == "jleavers"
    assert runner.calls == [["api", "/user"]]


@pytest.mark.parametrize("document", [{}, {"login": ""}, {"login": "a/b"}, ["jleavers"]])
async def test_a_user_without_a_usable_login_is_a_response_error(document: object) -> None:
    with pytest.raises(GitHubError) as caught:
        await fetch_login(StubRunner(ok(document)))
    assert caught.value.category == "response"


async def test_fetch_actions_usage_asks_for_the_actions_summary_alone() -> None:
    runner = StubRunner(
        GhResult(returncode=0, stdout=FIXTURE.read_text(encoding="utf-8"), stderr="")
    )
    usage = await fetch_actions_usage(runner, "jleavers")
    assert usage.used_minutes == 2306.0
    # The token is the runner's GH_TOKEN, so no argument carries it.
    assert runner.calls == [["api", SUMMARY]]


@pytest.mark.parametrize("account", ["", "-leading", "a/b", "a?b", "x" * 40])
async def test_an_account_that_is_not_a_login_is_never_sent(account: str) -> None:
    runner = StubRunner()
    with pytest.raises(GitHubError) as caught:
        await fetch_actions_usage(runner, account)
    assert caught.value.category == "config" and runner.calls == []


@pytest.mark.parametrize(
    ("stderr", "category"),
    [
        ("gh: Bad credentials (HTTP 401)", "auth"),
        ("gh: Not Found (HTTP 404)", "not_found"),
        ("gh: I'm a teapot (HTTP 418)", "status"),
    ],
)
async def test_a_failed_read_carries_the_adapter_s_category(stderr: str, category: str) -> None:
    runner = StubRunner(GhResult(returncode=1, stdout="", stderr=stderr))
    with pytest.raises(GitHubError) as caught:
        await fetch_actions_usage(runner, "jleavers")
    assert caught.value.category == category
    assert caught.value.message == stderr


async def test_a_body_that_is_not_json_is_a_response_error() -> None:
    runner = StubRunner(GhResult(returncode=0, stdout="<html>", stderr=""))
    with pytest.raises(GitHubError) as caught:
        await fetch_login(runner)
    assert caught.value.category == "response"


@pytest.mark.parametrize(
    ("category", "words"),
    [
        ("auth", "token rejected"),
        ("not_found", "needs the user scope"),
        ("rate_limited", "rate limited"),
        ("transport", "could not be reached"),
        ("response", "not understood"),
        ("config", "gh could not be run"),
        ("status", "refused"),
    ],
)
def test_a_failure_is_described_in_issuebot_s_words_not_gh_s(category: str, words: str) -> None:
    error = GitHubError(category, "gh: stderr that says ghp_abc", stderr="ghp_abc")  # type: ignore[arg-type]
    text = describe_failure(error)
    assert words in text and "ghp_abc" not in text
