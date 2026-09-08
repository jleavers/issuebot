"""Slack message text for issuebot events: one line of mrkdwn per notifiable kind."""

import re

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
