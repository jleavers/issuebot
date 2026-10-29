"""The auto-triage record: when it changes, and why an issue is not auto-triaged."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from issuebot.config import GitHubLabels
from issuebot.github import Issue
from issuebot.orchestrator.approval import AUTHOR_DELETED, NOT_A_MAINTAINER
from issuebot.orchestrator.auto_triage import (
    SCAN_INTERVAL_S,
    SETTLE,
    AutoTriageRecord,
    settle,
    skip_reason,
)

W = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
T = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


def test_the_constants() -> None:
    assert SCAN_INTERVAL_S == 120.0
    assert timedelta(seconds=60) == SETTLE


def test_settle_turns_on_with_the_watermark_at_now() -> None:
    assert settle(None, auto=True, now=T) == AutoTriageRecord(auto=True, watermark=T)


def test_settle_keeps_the_watermark_across_a_restart_with_it_on() -> None:
    assert settle(AutoTriageRecord(auto=True, watermark=W), auto=True, now=T) is None


def test_settle_records_turning_off() -> None:
    assert settle(AutoTriageRecord(auto=True, watermark=W), auto=False, now=T) == (
        AutoTriageRecord(auto=False, watermark=W)
    )


def test_settle_after_a_spell_off_moves_the_watermark_to_now() -> None:
    assert settle(AutoTriageRecord(auto=False, watermark=W), auto=True, now=T) == (
        AutoTriageRecord(auto=True, watermark=T)
    )


def test_settle_with_it_off_and_no_record_records_nothing() -> None:
    assert settle(None, auto=False, now=T) is None


def test_settle_with_it_off_after_off_changes_nothing() -> None:
    assert settle(AutoTriageRecord(auto=False, watermark=W), auto=False, now=T) is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"github_state": "closed"}, "closed"),
        ({"labels": ("issuebot/todo",)}, "carries an issuebot label"),
        ({"labels": ("issuebot/triage-complete",)}, "carries an issuebot label"),
        ({"labels": ("issuebot/operator-step",)}, "carries an issuebot label"),
        ({"parent": 7}, "has a parent issue"),
        ({"author": None}, AUTHOR_DELETED),
        (
            {"author_association": "NONE"},
            NOT_A_MAINTAINER.format(author="reporter", association="NONE"),
        ),
        (
            {"author_association": None},
            NOT_A_MAINTAINER.format(author="reporter", association="unknown"),
        ),
        ({"labels": ("enhancement",)}, None),
    ],
)
def test_skip_reason(
    make_issue: Callable[..., Issue], overrides: dict[str, object], expected: str | None
) -> None:
    fields: dict[str, object] = {
        "state": None,
        "state_labels": (),
        "labels": (),
        "author_association": "MEMBER",
    }
    fields.update(overrides)
    assert skip_reason(make_issue(**fields), GitHubLabels()) == expected
