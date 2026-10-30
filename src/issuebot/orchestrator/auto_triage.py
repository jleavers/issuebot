"""Auto-triage's record and its pure rules (spec 2026-10-25, section 2).

With ``triage.auto`` on, the worker labels each new maintainer issue ``triage-todo`` itself. It
remembers one thing per repository, a watermark: every open issue opened after it is decided
once, oldest first, and an issue behind it is never looked at again. That is the "at most once".
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from issuebot.config import GitHubLabels
from issuebot.github import MAINTAINER_ASSOCIATIONS, Issue
from issuebot.orchestrator.approval import AUTHOR_DELETED, NOT_A_MAINTAINER

# At most one scan this often (30 GraphQL points an hour), so a new issue is labelled within
# about three minutes of being opened.
SCAN_INTERVAL_S = 120.0

# How long the next scan waits after one stopped at its page ceiling. That read walked every page
# it may, some thirty points, and the next from the same watermark would walk them all again:
# every two minutes, that is close to a fifth of the account's hourly GraphQL budget.
CEILING_RETRY_S = 3600.0

# An issue opened less than this before a scan waits for the next: otherwise one GitHub has not
# yet listed could be overtaken by a newer one, with the watermark moving past both.
SETTLE = timedelta(seconds=60)


@dataclass(frozen=True, kw_only=True, slots=True)
class AutoTriageRecord:
    """Whether auto-triage was on when last settled, and its watermark. Each change is
    published whole as an ``AutoTriageState``, which is how a restarted worker reads it back."""

    auto: bool
    watermark: datetime


def settle(
    record: AutoTriageRecord | None, *, auto: bool, now: datetime
) -> AutoTriageRecord | None:
    """The record after reading the setting, or ``None`` when nothing changes.

    Switching on, with no record or after a spell off, puts the watermark at ``now``: nothing
    opened before is labelled. A restart with it on keeps the watermark, so what opened while
    the worker was stopped is. Switching off is recorded with the watermark where it was.
    """
    if auto and (record is None or not record.auto):
        return AutoTriageRecord(auto=True, watermark=now)
    if not auto and record is not None and record.auto:
        return AutoTriageRecord(auto=False, watermark=record.watermark)
    return None


def skip_reason(issue: Issue, labels: GitHubLabels) -> str | None:
    """Why ``issue`` is not auto-triaged, or ``None`` when it is.

    "Carries an issuebot label" is any state label or marker (``operator-step`` included), not
    only the states and triage labels section 2 names. An issue with sub-issues has been split
    already, by a maintainer linking them before the scan reached it: ``triage-todo`` on it is a
    parent review a person asks for (section 1), which would put on ``triage-complete`` and start
    a record the worker then reviews by itself, so it is passed over as a step is (phase 4
    ruling 23). The author checks are the ones approval makes later, so a stranger's issue is
    passed over here rather than labelled and handed back.
    """
    if issue.github_state != "open":
        return "closed"
    issuebot = {label.lower() for label in (*labels.as_tuple(), *labels.markers())}
    if issuebot.intersection(label.lower() for label in issue.labels):
        return "carries an issuebot label"
    if issue.parent is not None or issue.parent_elsewhere:
        return "has a parent issue"
    if issue.sub_issue_count > 0:
        return "has sub-issues"
    if issue.author is None:
        return AUTHOR_DELETED
    if issue.author_association not in MAINTAINER_ASSOCIATIONS:
        return NOT_A_MAINTAINER.format(
            author=issue.author, association=issue.author_association or "unknown"
        )
    return None
