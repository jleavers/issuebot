"""Tests for the in-memory FakeGitHub: protocol conformance and GitHub-like semantics."""

from datetime import UTC, datetime, timedelta

import pytest

from issuebot.config import GitHubSettings
from issuebot.github.errors import GitHubError
from issuebot.github.fake import FakeGitHub
from issuebot.github.models import WORKPAD_MARKER, BranchRules, StateLabel
from issuebot.github.state import LABEL_STYLES, LabelStyle

SETTINGS = GitHubSettings(repo="example/repo")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub(SETTINGS, now=Clock())


def test_repo_and_labels_come_from_settings(fake: FakeGitHub) -> None:
    assert fake.repo == "example/repo"
    assert fake.labels.todo == "issuebot/todo"
    assert set(fake.repo_labels) == {*SETTINGS.labels.as_tuple(), *SETTINGS.labels.markers()}


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


def test_add_issue_carries_the_author_through_the_node(fake: FakeGitHub) -> None:
    assert fake.add_issue("By the default reporter").author == "reporter"
    assert fake.add_issue("By a named account", author="mallory").author == "mallory"
    # GitHub sends ``author: null`` once the account is deleted; the fake does the same.
    assert fake.add_issue("By a deleted account", author=None).author is None


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


async def test_a_pull_request_by_anyone_else_closes_nothing_for_issuebot(fake: FakeGitHub) -> None:
    """``open_pr`` by another author, or from a fork, is a reference the normaliser drops (#77)."""
    issue = fake.add_issue("A", labels=("issuebot/review",))
    theirs = fake.open_pr(issue.number, author="mallory")
    assert fake.issue(issue.number).linked_pr is None
    fake.merge_pr(theirs.number)
    snapshot = fake.issue(issue.number)
    assert snapshot.github_state == "closed" and snapshot.linked_pr is None
    fake.reopen_issue(issue.number)
    forked = fake.open_pr(issue.number, cross_repository=True)
    assert forked.number != theirs.number
    assert fake.issue(issue.number).linked_pr is None
    ours = fake.open_pr(issue.number)
    linked = fake.issue(issue.number).linked_pr
    assert linked is not None and linked.number == ours.number


def test_the_fake_acts_as_one_login(fake: FakeGitHub) -> None:
    other = FakeGitHub(SETTINGS, login="Robot")
    issue = other.add_issue("A")
    other.open_pr(issue.number, author="robot")
    assert other.issue(issue.number).linked_pr is not None, "matched case-insensitively"
    assert fake.login == "issuebot"


def test_close_pr_does_not_close_issue(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A", labels=("issuebot/review",))
    pr = fake.open_pr(issue.number)
    fake.close_pr(pr.number)
    snapshot = fake.issue(issue.number)
    assert snapshot.github_state == "open"
    assert snapshot.linked_pr is not None and snapshot.linked_pr.state == "closed"


def test_pr_mergeability_round_trips_through_the_node(fake: FakeGitHub) -> None:
    """The fake emits GitHub's upper-case value so the shared normaliser lowercases it."""
    issue = fake.add_issue("A", labels=("issuebot/review",))
    pr = fake.open_pr(issue.number)
    assert pr.mergeable == "mergeable"
    assert fake.issue(issue.number).linked_pr.mergeable == "mergeable"  # type: ignore[union-attr]
    fake.set_pr_mergeable(pr.number, "conflicting")
    assert fake.issue(issue.number).linked_pr.mergeable == "conflicting"  # type: ignore[union-attr]
    fake.set_pr_mergeable(pr.number, "unknown")
    assert fake.issue(issue.number).linked_pr.mergeable == "unknown"  # type: ignore[union-attr]


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


async def test_a_marker_comment_by_anyone_else_is_not_the_workpad(fake: FakeGitHub) -> None:
    """The first line is public; the author is what the workpad is resolved by (#77)."""
    issue = fake.add_issue("A")
    impostor = fake.add_comment(issue.number, f"{WORKPAD_MARKER}\n\nmine now", author="mallory")
    assert impostor.author == "mallory"
    assert await fake.find_workpad_comment(issue.number) is None
    pad = await fake.comment(issue.number, f"{WORKPAD_MARKER}\n\n### Plan\n")
    found = await fake.find_workpad_comment(issue.number)
    assert found is not None and found.id == pad.id
    assert [c.id for c in fake.comments_for(issue.number)] == [impostor.id, pad.id]
    assert not any(name == "add_comment" for name, _ in fake.calls)


async def test_ensure_and_missing_labels(fake: FakeGitHub) -> None:
    assert await fake.missing_labels() == []
    assert [r.outcome for r in await fake.ensure_labels()] == ["unchanged"] * 9
    del fake.repo_labels["issuebot/rework"]
    fake.repo_labels["issuebot/review"] = LabelStyle("000000", "old")
    assert await fake.missing_labels() == ["issuebot/rework"]
    outcomes = {r.name: r.outcome for r in await fake.ensure_labels()}
    assert outcomes["issuebot/rework"] == "created"
    assert outcomes["issuebot/review"] == "updated"
    assert outcomes["issuebot/todo"] == "unchanged"
    assert fake.repo_labels["issuebot/review"] == LABEL_STYLES[StateLabel.REVIEW]


async def test_ensure_labels_creates_the_extra_labels_too(fake: FakeGitHub) -> None:
    style = LabelStyle("BFD4F2", "Run this issue with the sonnet model")
    results = await fake.ensure_labels({"issuebot/model/sonnet": style})
    assert results[-1].name == "issuebot/model/sonnet"
    assert results[-1].outcome == "created"
    assert fake.repo_labels["issuebot/model/sonnet"] == style
    assert await fake.missing_labels() == []


async def test_missing_labels_reports_the_extra_names_too(fake: FakeGitHub) -> None:
    extra = ("issuebot/model/sonnet",)
    assert await fake.missing_labels(extra) == ["issuebot/model/sonnet"]
    fake.repo_labels["issuebot/model/sonnet"] = LabelStyle("BFD4F2", "sonnet")
    assert await fake.missing_labels(extra) == []
    del fake.repo_labels["issuebot/rework"]
    assert await fake.missing_labels(extra) == ["issuebot/rework"]


async def test_ensure_and_missing_labels_cover_the_no_fault_marker(fake: FakeGitHub) -> None:
    """The marker is issuebot's too: `labels ensure` creates it and `validate` misses it."""
    del fake.repo_labels["issuebot/no-fault"]
    assert await fake.missing_labels() == ["issuebot/no-fault"]
    outcomes = {r.name: r.outcome for r in await fake.ensure_labels()}
    assert outcomes["issuebot/no-fault"] == "created"
    assert await fake.missing_labels() == []


async def test_set_state_leaves_the_no_fault_marker_alone(fake: FakeGitHub) -> None:
    """The marker has to survive the move to `complete`, or the close cannot be classified."""
    issue = fake.add_issue("A", labels=("issuebot/review", "issuebot/no-fault"))
    await fake.set_state(issue.number, StateLabel.COMPLETE)
    assert fake.issue(issue.number).labels == ("issuebot/no-fault", "issuebot/complete")
    assert fake.issue(issue.number).state is StateLabel.COMPLETE


async def test_a_claim_keeps_triage_complete(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A", labels=("issuebot/todo", "issuebot/triage-complete"))
    await fake.set_state(issue.number, StateLabel.IN_PROGRESS, clear_markers=True)
    assert fake.issue(issue.number).labels == ("issuebot/triage-complete", "issuebot/in-progress")


def test_preseed_labels_can_be_disabled() -> None:
    fake = FakeGitHub(SETTINGS, preseed_labels=False)
    assert fake.repo_labels == {}


async def test_probes(fake: FakeGitHub) -> None:
    assert (await fake.auth_status()).login == "issuebot"
    info = await fake.repo_info()
    assert (info.full_name, info.default_branch, info.private) == ("example/repo", "main", False)
    assert info.admin is False
    admin = FakeGitHub(SETTINGS, admin=True)
    assert (await admin.repo_info()).admin is True
    limit = await fake.rate_limit()
    assert limit.remaining == 4999 and limit.limit == 5000


async def test_branch_rules_defaults_to_one_review_and_can_be_set() -> None:
    fake = FakeGitHub(GitHubSettings(repo="example/repo"))
    default = await fake.branch_rules("main")
    assert default.required_approving_reviews == 1
    assert default.require_last_push_approval is True
    assert default.require_code_owner_review is True
    assert default.dismiss_stale_reviews_on_push is True
    assert default.bypassable == ()
    fake.branch_rules_result = BranchRules(branch="main", required_approving_reviews=None)
    result = await fake.branch_rules("main")
    assert result.required_approving_reviews is None
    assert result.require_last_push_approval is False
    assert result.require_code_owner_review is False
    assert result.dismiss_stale_reviews_on_push is False
    assert result.bypassable == ()


async def test_fail_next_injects_errors_in_order(fake: FakeGitHub) -> None:
    fake.fail_next("transport", times=2)
    for _ in range(2):
        with pytest.raises(GitHubError) as exc:
            await fake.auth_status()
        assert exc.value.category == "transport"
    assert (await fake.auth_status()).login == "issuebot"
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


async def test_the_fake_keeps_a_label_history_credited_to_its_actor(fake: FakeGitHub) -> None:
    """``count_own_label_additions`` counts the account's own additions of a label (#104)."""
    issue = fake.add_issue("A", labels=("issuebot/review",))
    assert await fake.count_own_label_additions(issue.number, "issuebot/rework") == 0
    await fake.set_state(issue.number, StateLabel.REWORK)
    fake.human_set_state(issue.number, StateLabel.REVIEW)
    await fake.set_state(issue.number, StateLabel.REWORK)
    fake.human_set_state(issue.number, StateLabel.REWORK, actor="reviewer")
    fake.human_add_label(issue.number, "issuebot/rework", actor="reviewer")  # already there
    assert await fake.count_own_label_additions(issue.number, "issuebot/rework") == 2
    assert await fake.count_own_label_additions(issue.number, "ISSUEBOT/REWORK") == 2
    assert await fake.count_own_label_additions(issue.number, "issuebot/review") == 0
    # The history is GitHub's: rewriting the workpad, or removing the label, changes nothing.
    fake.human_remove_label(issue.number, "issuebot/rework")
    assert await fake.count_own_label_additions(issue.number, "issuebot/rework") == 2
    assert fake.calls[-1] == ("count_own_label_additions", (issue.number, "issuebot/rework"))
    with pytest.raises(GitHubError) as exc:
        await fake.count_own_label_additions(999, "issuebot/rework")
    assert exc.value.category == "not_found"


async def test_approval_evidence_records_labels_edits_and_renames() -> None:
    clock = _Clock()
    fake = FakeGitHub(GitHubSettings(repo="example/repo"), now=clock, login="bot")
    fake.add_issue("Task", body="original", number=42)
    clock.advance(60)
    fake.human_set_state(42, StateLabel.TODO, actor="maintainer")
    clock.advance(60)
    fake.human_edit_body(42, "edited", editor="reporter")
    clock.advance(60)
    fake.human_rename(42, "Renamed", actor=None)
    clock.advance(60)
    await fake.set_state(42, StateLabel.IN_PROGRESS)
    evidence = await fake.approval_evidence(42)
    assert [(e.label, e.actor) for e in evidence.label_events] == [
        ("issuebot/todo", "maintainer"),
        ("issuebot/in-progress", "bot"),
    ]
    assert evidence.label_events[0].at < evidence.label_events[1].at
    assert [(e.what, e.editor) for e in evidence.edits] == [("body", "reporter"), ("title", None)]
    assert fake.issue(42).body == "edited" and fake.issue(42).title == "Renamed"
    assert await fake.own_login() == "bot"
    assert await fake.count_own_label_additions(42, "issuebot/in-progress") == 1


def test_the_fake_renders_a_minimal_body_html() -> None:
    fake = FakeGitHub(GitHubSettings(repo="example/repo"))
    fake.add_issue("T", body="Do X <b>.\n\n```sh\nuv run pytest\n```\n<!-- hidden -->\n", number=1)
    assert fake.issue(1).body_html == (
        '<p dir="auto">Do X &lt;b&gt;.</p>\n<pre lang="sh"><code>uv run pytest\n</code></pre>\n'
    )
    fake.add_issue("U", body=None, number=2)
    assert fake.issue(2).body_html is None


# --- triage: labels, comments and sub-issues -----------------------------------------------


async def test_fake_create_sub_issue_links_both_ways(fake: FakeGitHub) -> None:
    parent = fake.add_issue("Big thing")
    sub = await fake.create_sub_issue(parent.number, title="Step one", body="Do it.")
    assert sub.state == "open" and sub.title == "Step one"
    assert fake.issue(sub.number).parent == parent.number
    assert fake.issue(sub.number).body == "Do it."
    assert [s.number for s in await fake.sub_issues(parent.number)] == [sub.number]
    # Linked at creation and carrying no label: no poll sees it parentless or on the board.
    assert fake.issue(sub.number).labels == ()


async def test_fake_create_sub_issue_needs_an_existing_parent(fake: FakeGitHub) -> None:
    with pytest.raises(GitHubError) as exc:
        await fake.create_sub_issue(99, title="Orphan", body="")
    assert exc.value.category == "not_found"


async def test_fake_fetch_comments_returns_seeded_associations(fake: FakeGitHub) -> None:
    issue = fake.add_issue("Task")
    fake.add_tracker_comment(
        issue.number, body="Cover admin.\n\nPlease.", author="owner", association="OWNER"
    )
    fake.add_tracker_comment(issue.number, body="Hi", author=None, association="NONE")
    comments = await fake.fetch_comments(issue.number)
    assert [(c.author, c.association) for c in comments] == [("owner", "OWNER"), (None, "NONE")]
    assert comments[0].body_html == ('<p dir="auto">Cover admin.</p>\n<p dir="auto">Please.</p>\n')
    assert comments[0].created_at <= comments[1].created_at


async def test_fake_fetch_comments_includes_comments_the_adapter_wrote(fake: FakeGitHub) -> None:
    issue = fake.add_issue("Task")
    await fake.comment(issue.number, "from issuebot")
    (written,) = await fake.fetch_comments(issue.number)
    assert written.author == "issuebot"


async def test_fake_edit_labels_leaves_state_labels_alone(fake: FakeGitHub) -> None:
    issue = fake.add_issue("Task", labels=["issuebot/review", "issuebot/triage-todo"])
    await fake.edit_labels(
        issue.number, add=["issuebot/triage-complete"], remove=["issuebot/triage-todo"]
    )
    assert set(fake.issue(issue.number).labels) == {"issuebot/review", "issuebot/triage-complete"}
    assert fake.issue(issue.number).state is StateLabel.REVIEW


async def test_fake_edit_labels_refuses_a_label_the_repository_lacks(fake: FakeGitHub) -> None:
    issue = fake.add_issue("Task")
    with pytest.raises(GitHubError) as exc:
        await fake.edit_labels(issue.number, add=["no-such-label"])
    assert exc.value.category == "not_found"


async def test_fake_fetch_issues_with_label_is_open_oldest_first(fake: FakeGitHub) -> None:
    first = fake.add_issue("A", labels=["issuebot/triage-todo"])
    fake.add_issue("B", labels=["other"])
    closed = fake.add_issue("C", labels=["issuebot/triage-todo"])
    fake.close_issue(closed.number)
    third = fake.add_issue("D", labels=["Issuebot/Triage-Todo"])
    found = await fake.fetch_issues_with_label("issuebot/triage-todo")
    assert [i.number for i in found] == [first.number, third.number]


async def test_fake_fetch_issues_opened_since(fake: FakeGitHub) -> None:
    base = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    fake.add_issue("A", created_at=base - timedelta(hours=1))
    closed = fake.add_issue("B", created_at=base + timedelta(minutes=30))
    newest = fake.add_issue("C", created_at=base + timedelta(hours=1))
    fake.close_issue(closed.number)

    found = await fake.fetch_issues_opened_since(base)

    assert [i.number for i in found] == [newest.number]
    assert found[0].created_at == base + timedelta(hours=1)
    assert fake.calls[-1] == ("fetch_issues_opened_since", (base,))


async def test_fail_next_on_a_method_leaves_other_calls_alone(fake: FakeGitHub) -> None:
    issue = fake.add_issue("Task")
    fake.fail_next("server_error", on="comment")
    assert (await fake.auth_status()).login == "issuebot"  # passes, and does not consume it
    with pytest.raises(GitHubError) as exc:
        await fake.comment(issue.number, "x")
    assert exc.value.category == "server_error"
    await fake.comment(issue.number, "y")  # consumed: the next one succeeds


async def test_fail_next_after_lets_matching_calls_succeed_first(fake: FakeGitHub) -> None:
    parent = fake.add_issue("Parent")
    fake.fail_next("server_error", on="create_sub_issue", after=1)
    await fake.create_sub_issue(parent.number, title="one", body="")
    await fake.comment(parent.number, "between")  # not a match, so not counted
    with pytest.raises(GitHubError):
        await fake.create_sub_issue(parent.number, title="two", body="")
    await fake.create_sub_issue(parent.number, title="three", body="")
    assert [s.title for s in await fake.sub_issues(parent.number)] == ["one", "three"]


async def test_fail_next_after_without_a_method_counts_any_call(fake: FakeGitHub) -> None:
    fake.fail_next("transport", after=1)
    await fake.auth_status()
    with pytest.raises(GitHubError):
        await fake.rate_limit()
    await fake.rate_limit()


async def test_fail_next_times_with_a_method_fails_that_many_in_a_row(fake: FakeGitHub) -> None:
    issue = fake.add_issue("Task")
    fake.fail_next("transport", times=2, on="comment")
    for _ in range(2):
        with pytest.raises(GitHubError):
            await fake.comment(issue.number, "x")
    await fake.comment(issue.number, "x")


async def test_fail_next_after_applies_once_before_a_run_of_failures(fake: FakeGitHub) -> None:
    fake.fail_next("transport", times=2, after=1)
    await fake.auth_status()
    for _ in range(2):
        with pytest.raises(GitHubError):
            await fake.auth_status()
    await fake.auth_status()


async def test_link_sub_issue_links_both_ways(fake: FakeGitHub) -> None:
    parent = fake.add_issue("Big thing")
    child = fake.add_issue("Step")
    fake.link_sub_issue(parent.number, child.number)
    assert fake.issue(child.number).parent == parent.number
    assert [s.number for s in await fake.sub_issues(parent.number)] == [child.number]
    with pytest.raises(ValueError):
        fake.link_sub_issue(parent.number, child.number)  # already has a parent


async def test_fake_parent_steps(fake: FakeGitHub) -> None:
    parent = fake.add_issue("Big thing", labels=("issuebot/triage-complete",))
    first = await fake.create_sub_issue(parent.number, title="A", body="a")
    second = await fake.create_sub_issue(parent.number, title="B", body="b")
    fake.close_issue(first.number)
    steps = await fake.parent_steps([parent.number, 999])
    assert list(steps) == [parent.number]
    assert steps[parent.number].labels == ("issuebot/triage-complete",)
    assert [(s.number, s.state) for s in steps[parent.number].sub_issues] == [
        (first.number, "closed"),
        (second.number, "open"),
    ]
    assert fake.issue(first.number).parent == parent.number


async def test_fake_parent_review_steps(fake: FakeGitHub) -> None:
    parent = fake.add_issue("Big thing", number=10)
    for n in (11, 12, 13):
        fake.add_issue(f"Step {n}", number=n)
        fake.link_sub_issue(10, n)
    pr = fake.open_pr(11, title="T", body="B")
    fake.add_pr_review(pr.number, author="alice", association="MEMBER", body="fine")
    fake.merge_pr(pr.number)
    fake.close_issue(12, reason="NOT_PLANNED")
    steps = await fake.parent_review_steps(parent.number)
    assert [(s.number, s.state, s.state_reason) for s in steps] == [
        (11, "closed", "COMPLETED"),
        (12, "closed", "NOT_PLANNED"),
        (13, "open", None),
    ]
    closer = steps[0].closed_by
    assert closer is not None and (closer.number, closer.title) == (pr.number, "T")
    assert "B" in closer.body_html
    assert [(r.author, r.association, r.state) for r in closer.reviews] == [
        ("alice", "MEMBER", "APPROVED")
    ]
    assert steps[1].closed_by is None and steps[2].closed_by is None
    assert fake.calls[-1] == ("parent_review_steps", (10,))


async def test_fake_close_as_completed(fake: FakeGitHub) -> None:
    fake.add_issue("x", number=5)
    await fake.close_as_completed(5)
    assert fake.issue(5).github_state == "closed"
    assert fake.calls[-1] == ("close_as_completed", (5,))


async def test_fake_sub_issue_count_and_parent_steps_id_and_state(fake: FakeGitHub) -> None:
    parent = fake.add_issue("Big thing")
    assert fake.issue(parent.number).sub_issue_count == 0
    await fake.create_sub_issue(parent.number, title="A", body="a")
    assert fake.issue(parent.number).sub_issue_count == 1
    steps = (await fake.parent_steps([parent.number]))[parent.number]
    assert steps.id and steps.state == "open"
    fake.close_issue(parent.number)
    assert (await fake.parent_steps([parent.number]))[parent.number].state == "closed"
