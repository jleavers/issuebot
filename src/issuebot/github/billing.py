"""The billing account's GitHub Actions minutes: whose token it is, and what the month has used.

Read by the dashboard's poller (``issuebot.web.actions``) with a token of its own -- a classic
token carrying the ``user`` scope, the only credential GitHub's billing usage endpoints accept
-- and never by the worker. Spec: docs/superpowers/specs/2026-10-02-actions-minutes-design.md.

"Used" is what the billing page shows, established against it on 2026-10-02: every minute of
every Actions SKU, public repositories included, Windows at 1x -- the sum of ``grossQuantity``
over the items measured in minutes, which leaves storage out. The plan's allowance is not in
the API at all; it is a setting.
"""

import json
import re
from dataclasses import dataclass
from datetime import date

from issuebot.github.errors import GitHubError, categorise
from issuebot.github.runner import GhRunnerLike

USER_PATH = "/user"
SUMMARY_PATH = "/users/{account}/settings/billing/usage/summary?product=actions"
# GitHub's login alphabet. The account is interpolated into a path, so anything else is refused
# rather than sent.
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")


@dataclass(frozen=True, slots=True)
class ActionsUsage:
    period: date  # the first day of the month the figure is for (UTC)
    used_minutes: float


def next_period(period: date) -> date:
    """The first day of the month after ``period``: when its allowance resets."""
    if period.month == 12:
        return date(period.year + 1, 1, 1)
    return date(period.year, period.month + 1, 1)


def parse_summary(document: object) -> ActionsUsage:
    """``period`` from ``timePeriod``; ``used_minutes`` summed over the minute-measured items."""
    if not isinstance(document, dict):
        raise GitHubError("response", "billing summary is not a JSON object")
    period = document.get("timePeriod")
    year = period.get("year") if isinstance(period, dict) else None
    number = period.get("month") if isinstance(period, dict) else None
    if not (_whole(year) and _whole(number) and 1 <= number <= 12):
        raise GitHubError("response", "billing summary has no usable timePeriod")
    items = document.get("usageItems")
    if not isinstance(items, list):
        raise GitHubError("response", "billing summary has no usageItems")
    used = 0.0
    for item in items:
        if not isinstance(item, dict):
            raise GitHubError("response", "billing summary has a usage item that is not an object")
        unit = item.get("unitType")
        if not isinstance(unit, str) or unit.lower() != "minutes":
            continue
        quantity = item.get("grossQuantity")
        if not isinstance(quantity, int | float) or isinstance(quantity, bool):
            raise GitHubError("response", "billing summary has a non-numeric grossQuantity")
        used += float(quantity)
    return ActionsUsage(period=date(year, number, 1), used_minutes=used)


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


async def fetch_login(runner: GhRunnerLike) -> str:
    """The login the token belongs to, which is whose billing it can read."""
    document = await _get(runner, USER_PATH)
    login = document.get("login") if isinstance(document, dict) else None
    if not isinstance(login, str) or not _LOGIN.fullmatch(login):
        raise GitHubError("response", "GET /user returned no usable login")
    return login


async def fetch_actions_usage(runner: GhRunnerLike, account: str) -> ActionsUsage:
    """This month's Actions minutes for ``account``, which must be the token's own."""
    if not _LOGIN.fullmatch(account):
        raise GitHubError("config", "not a GitHub login")
    return parse_summary(await _get(runner, SUMMARY_PATH.format(account=account)))


async def _get(runner: GhRunnerLike, path: str) -> object:
    result = await runner.run(["api", path])
    if result.returncode != 0:
        first = next((line.strip() for line in result.stderr.splitlines() if line.strip()), "")
        raise GitHubError(
            categorise(result.returncode, result.stderr),
            first or f"gh exited with status {result.returncode}",
            exit_code=result.returncode,
            stderr=result.stderr,
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubError("response", f"{path.split('?')[0]} did not return JSON") from exc


def describe_failure(error: GitHubError) -> str:
    """issuebot's own words for a failed read: what the window's tooltip and the row carry.

    Never ``gh``'s stderr, which goes to the log; only the category reaches a page.
    """
    match error.category:
        case "auth":
            return "token rejected: it may have expired or been revoked"
        case "not_found":
            return (
                "no access to this account's billing (404): the token needs the user scope "
                "and must belong to the account"
            )
        case "rate_limited":
            return "rate limited by GitHub"
        case "transport":
            return "GitHub could not be reached"
        case "response":
            return "GitHub's answer was not understood"
        case "config":
            return "gh could not be run"
        case _:
            return "GitHub refused the request"
