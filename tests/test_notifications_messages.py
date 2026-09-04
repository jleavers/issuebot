"""Tests for the Slack message text."""

import pytest

from issuebot.config import GitHubLabels
from issuebot.events import (
    Blocked,
    Event,
    IssueCancelled,
    IssueCompleted,
    NotificationSent,
    PrOpened,
    RunEnded,
    RunStarted,
    StateChanged,
)
from issuebot.notifications import format_duration, format_event, issue_link, pr_link

REPO = "example/repo"
LABELS = GitHubLabels()
ISSUE = "<https://github.com/example/repo/issues/42|repo-42>"
PR_URL = "https://github.com/example/repo/pull/7"
PR = f"<{PR_URL}|PR #7>"


def fmt(event: Event, labels: GitHubLabels = LABELS) -> str | None:
    return format_event(event, repo=REPO, labels=labels)


def state_changed(**overrides: object) -> StateChanged:
    fields: dict[str, object] = {
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "from_label": "issuebot/todo",
        "to_label": "issuebot/in-progress",
        "actor": "issuebot",
    }
    fields.update(overrides)
    return StateChanged(**fields)  # type: ignore[arg-type]


def run_ended(**overrides: object) -> RunEnded:
    fields: dict[str, object] = {
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "run_id": "run-1",
        "outcome": "succeeded",
        "error": None,
        "turns": 2,
        "input_tokens": 100,
        "output_tokens": 10,
        "cost_usd": 0.314,
        "duration_s": 102.7,
    }
    fields.update(overrides)
    return RunEnded(**fields)  # type: ignore[arg-type]


# --- links and helpers ---------------------------------------------------------------


def test_issue_link_uses_the_repo_and_the_identifier() -> None:
    assert issue_link(REPO, state_changed()) == ISSUE


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (PR_URL, PR),
        (PR_URL + "/", f"<{PR_URL}/|PR #7>"),
        (
            "https://github.com/example/repo/pull/7/files",
            "<https://github.com/example/repo/pull/7/files|pull request>",
        ),
        ("https://example.test/changes/9", "<https://example.test/changes/9|pull request>"),
    ],
)
def test_pr_link_labels_numeric_tails(url: str, expected: str) -> None:
    assert pr_link(url) == expected


@pytest.mark.parametrize(("seconds", "text"), [(196.7, "3m16s"), (5, "0m05s"), (0, "0m00s")])
def test_format_duration(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text


# --- state_changed ---------------------------------------------------------------------


def test_claim_by_issuebot() -> None:
    assert fmt(state_changed()) == (
        f":hammer_and_wrench: {ISSUE} `issuebot/todo` → `issuebot/in-progress` by issuebot"
    )


def test_agent_move_to_review_links_the_pull_request() -> None:
    event = state_changed(
        from_label="issuebot/in-progress",
        to_label="issuebot/review",
        actor="agent",
        pr_url=PR_URL,
    )
    assert fmt(event) == (
        f":eyes: {ISSUE} `issuebot/in-progress` → `issuebot/review` by the agent · {PR}"
    )


def test_human_move_to_rework() -> None:
    event = state_changed(from_label="issuebot/review", to_label="issuebot/rework", actor="human")
    assert fmt(event) == f":repeat: {ISSUE} `issuebot/review` → `issuebot/rework` by a human"


def test_labels_stripped_reads_no_label() -> None:
    event = state_changed(from_label="issuebot/review", to_label=None)
    assert fmt(event) == f":label: {ISSUE} `issuebot/review` → no label by issuebot"


@pytest.mark.parametrize(
    ("role", "emoji"),
    [
        ("todo", ":inbox_tray:"),
        ("in_progress", ":hammer_and_wrench:"),
        ("review", ":eyes:"),
        ("rework", ":repeat:"),
        ("complete", ":white_check_mark:"),
    ],
)
def test_emoji_follows_the_target_role(role: str, emoji: str) -> None:
    text = fmt(state_changed(to_label=getattr(LABELS, role)))
    assert text is not None
    assert text.startswith(f"{emoji} ")


def test_emoji_matches_configured_labels_case_insensitively() -> None:
    labels = GitHubLabels(review="Bot: Review")
    text = fmt(state_changed(to_label="bot: review"), labels)
    assert text is not None
    assert text.startswith(":eyes: ")


def test_unknown_label_gets_the_generic_emoji() -> None:
    text = fmt(state_changed(to_label="wontfix"))
    assert text == f":label: {ISSUE} `issuebot/todo` → `wontfix` by issuebot"


# --- the other kinds -------------------------------------------------------------------


def test_blocked() -> None:
    event = Blocked(issue_number=42, issue_identifier="repo-42", reason="Turn budget exhausted.")
    assert fmt(event) == f":no_entry: {ISSUE} blocked: Turn budget exhausted."


def test_run_started() -> None:
    event = RunStarted(
        issue_number=42,
        issue_identifier="repo-42",
        run_id="run-1",
        attempt=2,
        session_id=None,
        workspace_path="/workspaces/repo-42",
    )
    assert fmt(event) == f":rocket: {ISSUE} run started (attempt 2)"


def test_run_ended_succeeded() -> None:
    assert fmt(run_ended()) == f":white_check_mark: {ISSUE} run succeeded: 2 turns, 1m42s, $0.31"


def test_run_ended_single_turn_is_singular() -> None:
    assert (
        fmt(run_ended(turns=1)) == f":white_check_mark: {ISSUE} run succeeded: 1 turn, 1m42s, $0.31"
    )


def test_run_ended_failed_with_error() -> None:
    event = run_ended(outcome="failed", error="process_exit: claude exited 1")
    assert (
        fmt(event)
        == f":x: {ISSUE} run failed: process_exit: claude exited 1 (2 turns, 1m42s, $0.31)"
    )


def test_run_ended_timed_out_without_error() -> None:
    event = run_ended(outcome="timed_out", turns=1)
    assert fmt(event) == f":x: {ISSUE} run timed out (1 turn, 1m42s, $0.31)"


def test_pr_opened() -> None:
    event = PrOpened(issue_number=42, issue_identifier="repo-42", pr_number=7, pr_url=PR_URL)
    assert fmt(event) == f":link: {ISSUE} opened {PR}"


def test_issue_completed_with_and_without_a_pull_request() -> None:
    with_pr = IssueCompleted(issue_number=42, issue_identifier="repo-42", pr_url=PR_URL)
    without = IssueCompleted(issue_number=42, issue_identifier="repo-42", pr_url=None)
    assert fmt(with_pr) == f":tada: {ISSUE} complete · {PR} merged"
    assert fmt(without) == f":tada: {ISSUE} complete"


def test_issue_cancelled() -> None:
    event = IssueCancelled(
        issue_number=42, issue_identifier="repo-42", reason="closed without a merged pull request"
    )
    assert fmt(event) == f":wastebasket: {ISSUE} cancelled: closed without a merged pull request"


def test_free_text_is_escaped_for_mrkdwn() -> None:
    blocked = Blocked(issue_number=42, issue_identifier="repo-42", reason="<!channel> & <b>")
    assert fmt(blocked) == f":no_entry: {ISSUE} blocked: &lt;!channel&gt; &amp; &lt;b&gt;"
    event = run_ended(outcome="failed", error="a<b")
    assert fmt(event) == f":x: {ISSUE} run failed: a&lt;b (2 turns, 1m42s, $0.31)"


def test_notification_sent_and_bare_events_are_not_formatted() -> None:
    sent = NotificationSent(
        issue_number=42, issue_identifier="repo-42", channel="slack", about_kind="blocked"
    )
    assert fmt(sent) is None
    assert fmt(Event()) is None
