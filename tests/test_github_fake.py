"""Tests for the in-memory FakeGitHub: protocol conformance and GitHub-like semantics."""

from datetime import UTC, datetime, timedelta

import pytest

from issuebot.config import GitHubSettings
from issuebot.github.errors import GitHubError
from issuebot.github.fake import FakeGitHub
from issuebot.github.models import WORKPAD_MARKER, StateLabel
from issuebot.github.state import LABEL_STYLES, LabelStyle

SETTINGS = GitHubSettings(repo="example/repo")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub(SETTINGS, now=Clock())


def test_repo_and_labels_come_from_settings(fake: FakeGitHub) -> None:
    assert fake.repo == "example/repo"
    assert fake.labels.todo == "issuebot/todo"
    assert set(fake.repo_labels) == set(SETTINGS.labels.as_tuple())


def test_add_issue_produces_normalised_issue(fake: FakeGitHub) -> None:
    issue = fake.add_issue("Add retry backoff", body="Details", labels=("Bug", "issuebot/todo"))
    assert issue.number == 1
    assert issue.id == "1"
    assert issue.identifier == "repo-1"
    assert issue.url == "https://github.com/example/repo/issues/1"
    assert issue.labels == ("bug", "issuebot/todo")
    assert issue.state is StateLabel.TODO
    assert issue.dispatchable
    assert issue.created_at.tzinfo is UTC
    second = fake.add_issue("Second")
    assert second.number == 2
    assert second.state is None and not second.dispatchable


async def test_fetch_by_states_filters_open_issues_by_role(fake: FakeGitHub) -> None:
    todo = fake.add_issue("A", labels=("issuebot/todo",))
    fake.add_issue("B", labels=("issuebot/review",))
    fake.add_issue("C")
    closed = fake.add_issue("D", labels=("issuebot/todo",))
    fake.close_issue(closed.number)
    issues = await fake.fetch_issues_by_states([StateLabel.TODO, StateLabel.REWORK])
    assert [issue.number for issue in issues] == [todo.number]
    assert await fake.fetch_issues_by_states([]) == []
    assert fake.calls[0] == ("fetch_issues_by_states", ((StateLabel.TODO, StateLabel.REWORK),))


async def test_fetch_by_ids_omits_unknown_and_pull_request_numbers(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A", labels=("issuebot/in-progress",))
    pr = fake.open_pr(issue.number)
    found = await fake.fetch_issues_by_ids([str(issue.number), str(pr.number), "999", "x"])
    assert [i.number for i in found] == [issue.number]
    assert found[0].linked_pr is not None and found[0].linked_pr.number == pr.number
    assert found[0].linked_pr.state == "open"


async def test_merge_pr_closes_issue_and_terminal_fetch_sees_it(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A", labels=("issuebot/review",))
    pr = fake.open_pr(issue.number)
    fake.merge_pr(pr.number)
    snapshot = fake.issue(issue.number)
    assert snapshot.github_state == "closed"
    assert snapshot.closed_at is not None
    assert snapshot.linked_pr is not None and snapshot.linked_pr.state == "merged"
    assert snapshot.linked_pr.merged_at is not None
    terminal = await fake.fetch_terminal_issues()
    assert [i.number for i in terminal] == [issue.number]
    assert await fake.fetch_issues_by_states([StateLabel.REVIEW]) == []


def test_close_pr_does_not_close_issue(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A", labels=("issuebot/review",))
    pr = fake.open_pr(issue.number)
    fake.close_pr(pr.number)
    snapshot = fake.issue(issue.number)
    assert snapshot.github_state == "open"
    assert snapshot.linked_pr is not None and snapshot.linked_pr.state == "closed"


async def test_set_state_is_exclusive_and_bumps_updated_at(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A", labels=("bug", "issuebot/todo", "issuebot/review"))
    assert fake.issue(issue.number).state is None
    await fake.set_state(issue.number, StateLabel.IN_PROGRESS)
    snapshot = fake.issue(issue.number)
    assert snapshot.state is StateLabel.IN_PROGRESS
    assert snapshot.labels == ("bug", "issuebot/in-progress")
    assert snapshot.updated_at > issue.updated_at
    await fake.clear_state(issue.number)
    assert fake.issue(issue.number).labels == ("bug",)
    assert fake.issue(issue.number).state is None


async def test_set_state_requires_the_label_to_exist_in_the_repo(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A")
    del fake.repo_labels["issuebot/in-progress"]
    with pytest.raises(GitHubError) as exc:
        await fake.set_state(issue.number, StateLabel.IN_PROGRESS)
    assert exc.value.category == "not_found"
    assert "labels ensure" in exc.value.message


async def test_set_state_requires_every_state_label_to_exist_in_the_repo(
    fake: FakeGitHub,
) -> None:
    issue = fake.add_issue("A")
    del fake.repo_labels["issuebot/complete"]
    with pytest.raises(GitHubError) as exc:
        await fake.set_state(issue.number, StateLabel.TODO)
    assert exc.value.category == "not_found"
    assert "labels ensure" in exc.value.message
    assert fake.issue(issue.number).labels == ()


async def test_set_state_on_unknown_issue_raises_not_found(fake: FakeGitHub) -> None:
    with pytest.raises(GitHubError) as exc:
        await fake.set_state(999, StateLabel.TODO)
    assert exc.value.category == "not_found"


async def test_clear_state_requires_every_state_label_to_exist_in_the_repo(
    fake: FakeGitHub,
) -> None:
    issue = fake.add_issue("A", labels=("issuebot/todo",))
    del fake.repo_labels["issuebot/complete"]
    with pytest.raises(GitHubError) as exc:
        await fake.clear_state(issue.number)
    assert exc.value.category == "not_found"
    assert "labels ensure" in exc.value.message
    assert fake.issue(issue.number).labels == ("issuebot/todo",)


def test_human_helpers_change_labels_without_recording_calls(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A")
    fake.human_set_state(issue.number, StateLabel.TODO)
    fake.human_add_label(issue.number, "Bug")
    assert fake.issue(issue.number).labels == ("issuebot/todo", "bug")
    fake.human_remove_label(issue.number, "bug")
    fake.human_set_state(issue.number, StateLabel.REWORK)
    assert fake.issue(issue.number).state is StateLabel.REWORK
    assert fake.calls == []


async def test_comments_and_workpad(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A")
    assert await fake.find_workpad_comment(issue.number) is None
    first = await fake.comment(issue.number, "hello")
    pad = await fake.comment(issue.number, f"{WORKPAD_MARKER}\n\n### Plan\n")
    assert first.id != pad.id
    assert first.author == "issuebot"
    found = await fake.find_workpad_comment(issue.number)
    assert found is not None and found.id == pad.id
    updated = await fake.update_comment(pad.id, f"{WORKPAD_MARKER}\n\n- [x] done\n")
    assert updated.id == pad.id and "done" in updated.body
    assert updated.updated_at > pad.updated_at
    assert [c.id for c in fake.comments_for(issue.number)] == [first.id, pad.id]
    with pytest.raises(GitHubError) as exc:
        await fake.update_comment(12345, "x")
    assert exc.value.category == "not_found"


async def test_ensure_and_missing_labels(fake: FakeGitHub) -> None:
    assert await fake.missing_labels() == []
    assert [r.outcome for r in await fake.ensure_labels()] == ["unchanged"] * 5
    del fake.repo_labels["issuebot/rework"]
    fake.repo_labels["issuebot/review"] = LabelStyle("000000", "old")
    assert await fake.missing_labels() == ["issuebot/rework"]
    outcomes = {r.name: r.outcome for r in await fake.ensure_labels()}
    assert outcomes["issuebot/rework"] == "created"
    assert outcomes["issuebot/review"] == "updated"
    assert outcomes["issuebot/todo"] == "unchanged"
    assert fake.repo_labels["issuebot/review"] == LABEL_STYLES[StateLabel.REVIEW]


def test_preseed_labels_can_be_disabled() -> None:
    fake = FakeGitHub(SETTINGS, preseed_labels=False)
    assert fake.repo_labels == {}


async def test_probes(fake: FakeGitHub) -> None:
    assert (await fake.auth_status()).login == "fake-user"
    info = await fake.repo_info()
    assert (info.full_name, info.default_branch, info.private) == ("example/repo", "main", False)
    limit = await fake.rate_limit()
    assert limit.remaining == 4999 and limit.limit == 5000


async def test_fail_next_injects_errors_in_order(fake: FakeGitHub) -> None:
    fake.fail_next("transport", times=2)
    for _ in range(2):
        with pytest.raises(GitHubError) as exc:
            await fake.auth_status()
        assert exc.value.category == "transport"
    assert (await fake.auth_status()).login == "fake-user"
    assert [name for name, _ in fake.calls] == ["auth_status"] * 3


def test_reopen_issue_clears_closed_at(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A")
    fake.close_issue(issue.number)
    assert fake.issue(issue.number).closed_at is not None
    fake.reopen_issue(issue.number)
    assert fake.issue(issue.number).github_state == "open"
    assert fake.issue(issue.number).closed_at is None


def test_explicit_numbers_and_shared_numbering(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A", number=10)
    pr = fake.open_pr(issue.number)
    assert pr.number == 11
    assert fake.add_issue("B").number == 12
    with pytest.raises(ValueError, match="already exists"):
        fake.add_issue("C", number=10)
