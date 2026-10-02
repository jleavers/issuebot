"""Slack message text for issuebot events: one line of mrkdwn per notifiable kind."""

import math
import re
from datetime import UTC, date, datetime

from issuebot.config import GitHubLabels
from issuebot.events import (
    Blocked,
    Event,
    IssueCancelled,
    IssueCompleted,
    IssueEvent,
    PrOpened,
    RunEnded,
    RunStarted,
    StateChanged,
)

_PR_TAIL = re.compile(r"/pull/(\d+)/?$")
_ACTORS = {"issuebot": "by issuebot", "agent": "by the agent", "human": "by a human"}
_ROLE_EMOJI = {
    "todo": ":inbox_tray:",
    "in_progress": ":hammer_and_wrench:",
    "review": ":eyes:",
    "rework": ":repeat:",
    "complete": ":white_check_mark:",
}
_OTHER_LABEL_EMOJI = ":label:"
_OUTCOME_WORDS = {"timed_out": "timed out"}


def issue_link(repo: str, event: IssueEvent) -> str:
    """``<https://github.com/{repo}/issues/{n}|{identifier}>``."""
    return f"<https://github.com/{repo}/issues/{event.issue_number}|{event.issue_identifier}>"


def pr_link(url: str) -> str:
    """``<url|PR #n>`` when the URL ends in ``/pull/<digits>``, else ``<url|pull request>``."""
    match = _PR_TAIL.search(url)
    label = f"PR #{match.group(1)}" if match else "pull request"
    return f"<{url}|{label}>"


def format_duration(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}m{total % 60:02d}s"


def _escape(text: str) -> str:
    """Mrkdwn-escape free text so it cannot form a link or ping a channel; order matters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_event(event: Event, *, repo: str, labels: GitHubLabels) -> str | None:
    """One line of Slack mrkdwn for the seven notifiable kinds; None for anything else."""
    if not isinstance(event, IssueEvent):
        return None
    issue = issue_link(repo, event)
    match event:
        case StateChanged():
            return _state_changed(event, issue, labels)
        case Blocked():
            return f":no_entry: {issue} blocked: {_escape(event.reason)}"
        case RunStarted():
            return f":rocket: {issue} run started (attempt {event.attempt})"
        case RunEnded():
            return _run_ended(event, issue)
        case PrOpened():
            return f":link: {issue} opened {pr_link(event.pr_url)}"
        case IssueCompleted():
            return _completed(event, issue)
        case IssueCancelled():
            return f":wastebasket: {issue} cancelled: {_escape(event.reason)}"
    return None


def _completed(event: IssueCompleted, issue: str) -> str:
    if event.resolution == "no_change":
        return f":mag: {issue} closed with no change needed"
    merged = f" · {pr_link(event.pr_url)} merged" if event.pr_url else ""
    return f":tada: {issue} complete{merged}"


def _state_changed(event: StateChanged, issue: str, labels: GitHubLabels) -> str:
    emoji = _emoji_for(event.to_label, labels)
    move = f"{_label(event.from_label)} → {_label(event.to_label)}"
    text = f"{emoji} {issue} {move} {_ACTORS[event.actor]}"
    if event.pr_url:
        text += f" · {pr_link(event.pr_url)}"
    return text


def _label(name: str | None) -> str:
    return f"`{name}`" if name else "no label"


def _emoji_for(name: str | None, labels: GitHubLabels) -> str:
    if name is None:
        return _OTHER_LABEL_EMOJI
    lowered = name.lower()
    for role, emoji in _ROLE_EMOJI.items():
        if getattr(labels, role).lower() == lowered:
            return emoji
    return _OTHER_LABEL_EMOJI


def _run_ended(event: RunEnded, issue: str) -> str:
    turns = f"{event.turns} turn" if event.turns == 1 else f"{event.turns} turns"
    stats = f"{turns}, {format_duration(event.duration_s)}, ${event.cost_usd:.2f}"
    if event.outcome == "succeeded":
        return f":white_check_mark: {issue} run succeeded: {stats}"
    word = _OUTCOME_WORDS.get(event.outcome, event.outcome)
    detail = f": {_escape(event.error)}" if event.error else ""
    return f":x: {issue} run {word}{detail} ({stats})"


def format_actions_alert(*, account: str, used: int, included: int, resets_on: date) -> str:
    """The dashboard's low-minutes line (spec 2026-10-02): whole minutes, one mrkdwn line."""
    who = _escape(account)
    resets = f"{resets_on.day} {resets_on:%b}"
    if used >= included:
        return (
            f":rotating_light: GitHub Actions: {who} has used all {included:,} included minutes "
            f"this month ({used:,} used). Runs in private repositories are now billed or "
            f"refused, depending on the account's budget, until {resets}."
        )
    percent = round(used / included * 100)
    return (
        f":warning: GitHub Actions: {who} has used {used:,} of {included:,} included minutes "
        f"this month ({percent}%); {included - used:,} left until {resets}."
    )


# The Claude usage windows by the names claude gives them (`rateLimitType`, `unifiedWindows`).
_CLAUDE_WINDOWS = {"five_hour": "5-hour", "seven_day": "7-day"}


def format_claude_limit_alert(
    *, window: str | None, percent: int, hit: bool, resets_at: datetime, now: datetime
) -> str:
    """The Claude usage line (spec 2026-10-02, claude-limits-alert): a warning that a
    window is filling, or, when ``hit``, a limit that has stopped
    the board. Times are UTC; a window name claude has not used before is shown as reported,
    escaped. ``percent`` is shown only on a warning; a hit says the limit is reached whatever
    the figure."""
    when = _utc_moment(resets_at, now)
    if not hit:
        return (
            f":warning: Claude: {_claude_window(window, 'window')} is {percent}% used; "
            f"it resets {when}."
        )
    return (
        f":rotating_light: Claude: {_claude_window(window, 'limit')} is reached; issuebot "
        f"stops claiming issues until {when} ({_time_until(resets_at, now)})."
    )


def _claude_window(window: str | None, noun: str) -> str:
    if window in _CLAUDE_WINDOWS:
        return f"the {_CLAUDE_WINDOWS[window]} usage {noun}"
    if window:
        return f"the usage {noun} ({_escape(window)})"
    return f"the usage {noun}"


def _utc_moment(moment: datetime, now: datetime) -> str:
    """``20:00 UTC`` on today's UTC date, else ``Fri 9 Oct, 05:00 UTC`` (no glibc-only ``%-d``)."""
    moment, today = moment.astimezone(UTC), now.astimezone(UTC).date()
    clock = f"{moment:%H:%M} UTC"
    if moment.date() == today:
        return clock
    return f"{moment:%a} {moment.day} {moment:%b}, {clock}"


def _time_until(moment: datetime, now: datetime) -> str:
    """``in 42 min``, ``in 2 h 13 min`` or ``in 6 d 11 h``: minutes rounded up, never negative."""
    minutes = max(math.ceil((moment - now).total_seconds() / 60), 0)
    if minutes < 60:
        return f"in {minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"in {hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"in {days} d {hours} h"
