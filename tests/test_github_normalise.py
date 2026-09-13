"""Tests for GitHubError and GraphQL issue normalisation."""

from datetime import UTC, datetime
from typing import Any

import pytest

from issuebot.config import GitHubLabels
from issuebot.github.errors import GitHubError
from issuebot.github.models import WORKPAD_MARKER, StateLabel, is_workpad_body
from issuebot.github.normalise import issue_from_node, label_name, repo_short_name, role_for

LABELS = GitHubLabels()
REPO = "example/repo"


def node(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": 42,
        "title": "Add retry backoff",
        "body": "We need exponential backoff.",
        "author": {"login": "reporter"},
        "state": "OPEN",
        "url": "https://github.com/example/repo/issues/42",
        "createdAt": "2026-09-01T09:00:00Z",
        "updatedAt": "2026-09-02T10:11:12Z",
        "closedAt": None,
        "labels": {"nodes": [{"name": "Bug"}, {"name": "issuebot/in-progress"}, {"name": "bug"}]},
        "assignees": {"nodes": [{"login": "jleavers"}]},
        "closedByPullRequestsReferences": {"nodes": []},
    }
    base.update(overrides)
    return base


def pr(
    number: int, state: str, merged_at: str | None = None, mergeable: str | None = "MERGEABLE"
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "number": number,
        "url": f"https://github.com/example/repo/pull/{number}",
        "state": state,
        "mergedAt": merged_at,
    }
    if mergeable is not None:
        fields["mergeable"] = mergeable
    return fields


# --- errors ----------------------------------------------------------------------


def test_error_str_and_retryable() -> None:
    err = GitHubError("rate_limited", "slow down", exit_code=1, stderr="x" * 600)
    assert str(err) == "rate_limited: slow down"
    assert err.category == "rate_limited"
    assert err.message == "slow down"
    assert err.exit_code == 1
    assert err.stderr is not None and len(err.stderr) == 500
    assert err.retryable
    assert GitHubError("transport", "down").retryable
    for category in ("auth", "not_found", "status", "response", "config"):
        assert not GitHubError(category, "x").retryable  # type: ignore[arg-type]


# --- label helpers -----------------------------------------------------------------


def test_label_name_and_role_for() -> None:
    assert label_name(LABELS, StateLabel.IN_PROGRESS) == "issuebot/in-progress"
    assert role_for(LABELS, "issuebot/in-progress") is StateLabel.IN_PROGRESS
    assert role_for(LABELS, "  ISSUEBOT/Review ") is StateLabel.REVIEW
    assert role_for(LABELS, "bug") is None
    custom = GitHubLabels(todo="queue", complete="done")
    assert label_name(custom, StateLabel.TODO) == "queue"
    assert role_for(custom, "Done") is StateLabel.COMPLETE


def test_repo_short_name() -> None:
    assert repo_short_name("jleavers/issuebot") == "issuebot"


# --- issue_from_node ---------------------------------------------------------------


def test_full_record() -> None:
    issue = issue_from_node(node(), repo=REPO, labels=LABELS)
    assert issue.id == "42"
    assert issue.identifier == "repo-42"
    assert issue.number == 42
    assert issue.title == "Add retry backoff"
    assert issue.body == "We need exponential backoff."
    assert issue.author == "reporter"
    assert issue.github_state == "open"
    assert issue.state is StateLabel.IN_PROGRESS
    assert issue.state_labels == ("issuebot/in-progress",)
    assert issue.labels == ("bug", "issuebot/in-progress")
    assert issue.url == "https://github.com/example/repo/issues/42"
    assert issue.assignees == ("jleavers",)
    assert issue.created_at == datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    assert issue.updated_at == datetime(2026, 9, 2, 10, 11, 12, tzinfo=UTC)
    assert issue.closed_at is None
    assert issue.linked_pr is None
    assert issue.dispatchable


def test_minimal_closed_record() -> None:
    issue = issue_from_node(
        node(
            body=None,
            state="CLOSED",
            closedAt="2026-09-03T00:00:00Z",
            labels={"nodes": []},
            assignees={"nodes": []},
        ),
        repo=REPO,
        labels=LABELS,
    )
    assert issue.body is None
    assert issue.github_state == "closed"
    assert issue.closed_at == datetime(2026, 9, 3, tzinfo=UTC)
    assert issue.state is None
    assert issue.state_labels == ()
    assert issue.labels == ()
    assert issue.assignees == ()
    assert not issue.dispatchable


def test_empty_body_is_none() -> None:
    assert issue_from_node(node(body=""), repo=REPO, labels=LABELS).body is None


@pytest.mark.parametrize("author", [None, {}, {"login": ""}, {"login": 7}, "ghost"])
def test_deleted_or_unusable_author_is_none(author: Any) -> None:
    """GitHub sends ``author: null`` once the account is gone; the envelope then says so."""
    assert issue_from_node(node(author=author), repo=REPO, labels=LABELS).author is None


def test_two_state_labels_is_a_conflict() -> None:
    issue = issue_from_node(
        node(labels={"nodes": [{"name": "issuebot/review"}, {"name": "issuebot/todo"}]}),
        repo=REPO,
        labels=LABELS,
    )
    assert issue.state is None
    assert issue.state_labels == ("issuebot/todo", "issuebot/review")  # role order
    assert not issue.dispatchable


def test_closed_issue_with_state_label_is_not_dispatchable() -> None:
    issue = issue_from_node(node(state="CLOSED"), repo=REPO, labels=LABELS)
    assert issue.state is StateLabel.IN_PROGRESS
    assert not issue.dispatchable


def test_custom_label_names_are_recognised() -> None:
    custom = GitHubLabels(in_progress="wip")
    issue = issue_from_node(node(labels={"nodes": [{"name": "WIP"}]}), repo=REPO, labels=custom)
    assert issue.state is StateLabel.IN_PROGRESS
    assert issue.state_labels == ("wip",)


def test_linked_pr_prefers_merged_then_open_then_closed() -> None:
    refs = {
        "nodes": [
            pr(50, "CLOSED"),
            pr(52, "OPEN"),
            pr(51, "MERGED", "2026-09-02T12:00:00Z"),
            pr(48, "MERGED", "2026-09-01T12:00:00Z"),
        ]
    }
    issue = issue_from_node(node(closedByPullRequestsReferences=refs), repo=REPO, labels=LABELS)
    assert issue.linked_pr is not None
    assert issue.linked_pr.number == 51
    assert issue.linked_pr.state == "merged"
    assert issue.linked_pr.merged_at == datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    refs = {"nodes": [pr(50, "CLOSED"), pr(53, "OPEN"), pr(52, "OPEN")]}
    issue = issue_from_node(node(closedByPullRequestsReferences=refs), repo=REPO, labels=LABELS)
    assert issue.linked_pr is not None and issue.linked_pr.number == 53
    assert issue.linked_pr.state == "open" and issue.linked_pr.merged_at is None

    refs = {"nodes": [pr(50, "CLOSED"), pr(49, "CLOSED")]}
    issue = issue_from_node(node(closedByPullRequestsReferences=refs), repo=REPO, labels=LABELS)
    assert issue.linked_pr is not None and issue.linked_pr.number == 50


def test_unusable_pr_reference_is_skipped() -> None:
    refs = {"nodes": [{"number": "x"}, None, pr(52, "OPEN")]}
    issue = issue_from_node(node(closedByPullRequestsReferences=refs), repo=REPO, labels=LABELS)
    assert issue.linked_pr is not None and issue.linked_pr.number == 52


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("MERGEABLE", "mergeable"), ("CONFLICTING", "conflicting"), ("UNKNOWN", "unknown")],
)
def test_linked_pr_carries_mergeability(raw: str, expected: str) -> None:
    refs = {"nodes": [pr(52, "OPEN", mergeable=raw)]}
    issue = issue_from_node(node(closedByPullRequestsReferences=refs), repo=REPO, labels=LABELS)
    assert issue.linked_pr is not None
    assert issue.linked_pr.mergeable == expected


@pytest.mark.parametrize("raw", [None, "", "WEIRD", 7])
def test_absent_or_unrecognised_mergeability_reads_unknown(raw: object) -> None:
    """An older response, or a value GitHub adds later, must never look like a conflict."""
    reference = pr(52, "OPEN", mergeable=None)
    if raw is not None:
        reference["mergeable"] = raw
    refs = {"nodes": [reference]}
    issue = issue_from_node(node(closedByPullRequestsReferences=refs), repo=REPO, labels=LABELS)
    assert issue.linked_pr is not None
    assert issue.linked_pr.mergeable == "unknown"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("number", None),
        ("number", "42"),
        ("number", True),
        ("title", ""),
        ("state", "WEIRD"),
        ("url", None),
        ("createdAt", None),
        ("updatedAt", "not a date"),
    ],
)
def test_malformed_record_raises_response_error(field: str, value: Any) -> None:
    with pytest.raises(GitHubError) as exc:
        issue_from_node(node(**{field: value}), repo=REPO, labels=LABELS)
    assert exc.value.category == "response"
    assert field in exc.value.message


def test_unusable_optional_metadata_normalises_quietly() -> None:
    issue = issue_from_node(
        node(labels="nope", assignees={"nodes": [None, {"login": ""}]}, closedAt="garbage"),
        repo=REPO,
        labels=LABELS,
    )
    assert issue.labels == ()
    assert issue.assignees == ()
    assert issue.closed_at is None


# --- is_workpad_body --------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (WORKPAD_MARKER, True),
        (f"{WORKPAD_MARKER} extra text", False),
        (f"\n\n{WORKPAD_MARKER}\n\n### Plan\n", True),
        ("", False),
        (None, False),
    ],
)
def test_is_workpad_body(body: str | None, expected: bool) -> None:
    assert is_workpad_body(body) is expected
