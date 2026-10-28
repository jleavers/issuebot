"""Tests for filing a parent review: the due test, the record and the worker's writes."""

from datetime import UTC, datetime

import pytest

from issuebot.agent.verdict import ParentReviewVerdict, SubIssueDraft
from issuebot.config import Settings
from issuebot.events import (
    Event,
    EventBus,
    IssueCompleted,
    ParentReviewState,
    StateChanged,
)
from issuebot.github import FakeGitHub, GitHubError, ParentSteps, SubIssue
from issuebot.orchestrator.parent_review import (
    ParentRecord,
    ReviewCounts,
    apply_parent_review,
    parent_review_comment,
    parent_review_due,
    parent_review_note,
)

NOW = datetime(2026, 10, 28, 9, 30, 0, tzinfo=UTC)
SETTINGS = Settings.model_validate({"github": {"repo": "example/repo"}})
LABELS = SETTINGS.github.labels
COUNTS = ReviewCounts(review=2, max_reviews=6, gap_round=1, max_gap_rounds=2)


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


def make_verdict(kind: str, *drafts: SubIssueDraft, ignored: str = "") -> ParentReviewVerdict:
    return ParentReviewVerdict(
        verdict=kind,  # type: ignore[arg-type]
        summary="The summary.",
        evidence="The evidence.",
        ignored_requests=ignored,
        sub_issues=drafts,
    )


NEXT = make_verdict(
    "next",
    SubIssueDraft(title="Third step", body="Do the third.", kind="issuebot"),
    SubIssueDraft(title="Fourth step", body="Do it by hand.", kind="operator"),
)
GAP = make_verdict("gap", SubIssueDraft(title="The gap", body="Fill it.", kind="issuebot"))
MET = make_verdict("met")


def split(
    github: FakeGitHub,
    parent: int,
    *children: int,
    labels: tuple[str, ...] = (),
    closed: bool = True,
) -> None:
    github.add_issue("The ask", labels=labels, number=parent)
    for child in children:
        github.add_issue(f"Step {child}", number=child)
        github.link_sub_issue(parent, child)
        if closed:
            github.close_issue(child)


def setup(
    *labels: str, children: tuple[int, ...] = (11, 12)
) -> tuple[FakeGitHub, Recorder, EventBus]:
    github = FakeGitHub(SETTINGS.github)
    recorder = Recorder()
    split(github, 10, *children, labels=labels)
    return github, recorder, EventBus([recorder])


async def apply(
    github: FakeGitHub,
    bus: EventBus,
    verdict: ParentReviewVerdict,
    *,
    seen: tuple[int, ...] = (11, 12),
    **kwargs: object,
) -> list[SubIssue]:
    return await apply_parent_review(
        github,
        bus,
        github.issue(10),
        verdict,
        seen=seen,
        counts=COUNTS,
        run_id="run-1",
        attempt=1,
        turns=3,
        cost_usd=0.5,
        now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def steps(
    *,
    state: str = "open",
    labels: tuple[str, ...] = ("issuebot/triage-complete",),
    sub: list[tuple[str]],
) -> ParentSteps:
    subs = tuple(
        SubIssue(number=11 + i, title=f"s{i}", url=f"u{i}", state=s)  # type: ignore[arg-type]
        for i, (s,) in enumerate(sub)
    )
    return ParentSteps(
        number=10,
        id="I_10",
        state=state,  # type: ignore[arg-type]
        labels=labels,
        sub_issues=subs,
    )


def test_due_when_every_step_has_closed() -> None:
    assert parent_review_due(steps(sub=[("closed",), ("closed",)]), LABELS)


def test_due_compares_labels_lower_cased() -> None:
    assert parent_review_due(
        steps(labels=("issuebot/triage-complete", "bug"), sub=[("closed",)]), LABELS
    )


@pytest.mark.parametrize(
    "change",
    [
        "a step open",
        "no steps",
        "parent closed",
        "no triage-complete",
        "triage-todo",
        "a state label",
    ],
)
def test_not_due(change: str) -> None:
    complete = "issuebot/triage-complete"
    kwargs: dict = {"sub": [("closed",), ("closed",)]}
    match change:
        case "a step open":
            kwargs["sub"] = [("closed",), ("open",)]
        case "no steps":
            kwargs["sub"] = []
        case "parent closed":
            kwargs["state"] = "closed"
        case "no triage-complete":
            kwargs["labels"] = ()
        case "triage-todo":
            kwargs["labels"] = (complete, "issuebot/triage-todo")
        case "a state label":
            kwargs["labels"] = (complete, "issuebot/review")
    assert not parent_review_due(steps(**kwargs), LABELS)


def test_the_record_is_watched_until_handed_over_or_finished() -> None:
    assert ParentRecord(identifier="example/repo#10").watched
    assert not ParentRecord(identifier="x", handed_over="why").watched
    assert not ParentRecord(identifier="x", finished=True).watched


def test_the_record_is_an_event() -> None:
    record = ParentRecord(identifier="example/repo#10", reviews=2, gap_rounds=1, handed_over="r")
    event = record.state_event(10)
    assert isinstance(event, ParentReviewState)
    assert (event.issue_number, event.issue_identifier) == (10, "example/repo#10")
    assert (event.reviews, event.gap_rounds, event.handed_over, event.finished) == (
        2,
        1,
        "r",
        False,
    )


def test_parent_review_note() -> None:
    assert parent_review_note(NEXT) == "next: 2 sub-issues"
    assert parent_review_note(GAP) == "gap: 1 sub-issue"
    assert parent_review_note(MET) == "met"


async def test_next_files_the_batch_after_the_others() -> None:
    github, recorder, bus = setup(LABELS.triage_complete)
    filed = await apply(github, bus, NEXT)
    assert github.calls[-1][0] == "comment"
    assert [s.number for s in filed] == [13, 14]
    assert [s.number for s in await github.sub_issues(10)] == [11, 12, 13, 14]
    assert (github.issue(13).body or "").endswith(
        "Part of #10: step 3 of 4, filed by issuebot's parent review."
    )
    assert (github.issue(14).body or "").endswith(
        "Part of #10: step 4 of 4, filed by issuebot's parent review."
    )
    assert LABELS.operator_step in github.issue(14).labels
    assert LABELS.operator_step not in github.issue(13).labels
    assert github.issue(10).labels == (LABELS.triage_complete,)
    [event] = recorder.events
    assert isinstance(event, StateChanged)
    assert (event.from_label, event.to_label) == (LABELS.triage_complete, LABELS.triage_complete)
    assert event.note == "next: 2 sub-issues"
    [comment] = github.comments_for(10)
    assert comment.body.startswith("### Issuebot parent review (2026-10-28T09:30:00Z)\n\n")
    assert "1. #13 Third step\n2. #14 Fourth step -- operator step\n" in comment.body
    assert "- **Evidence:** The evidence." in comment.body
    assert comment.body.endswith("Run `run-1` (attempt 1, 3 turns, $0.50). Review 2 of at most 6.")


async def test_a_label_triggered_review_swaps_triage_todo_off() -> None:
    github, recorder, bus = setup(LABELS.triage_todo)
    await apply(github, bus, NEXT)
    assert github.issue(10).labels == (LABELS.triage_complete,)
    [event] = recorder.events
    assert isinstance(event, StateChanged)
    assert event.from_label == LABELS.triage_todo


async def test_a_new_step_may_reuse_an_earlier_steps_title() -> None:
    github, _recorder, bus = setup(LABELS.triage_complete)
    github.human_rename(11, "Write the plan")
    verdict = make_verdict("next", SubIssueDraft(title="Write the plan", body="b", kind="issuebot"))
    [filed] = await apply(github, bus, verdict)
    assert filed.number not in (11, 12)
    assert [s.number for s in await github.sub_issues(10)] == [11, 12, filed.number]


async def test_a_retry_after_a_failed_creation_files_only_what_is_missing() -> None:
    github, recorder, bus = setup(LABELS.triage_complete)
    github.fail_next("network", on="create_sub_issue", after=1)
    with pytest.raises(GitHubError):
        await apply(github, bus, NEXT)
    assert len(await github.sub_issues(10)) == 3
    assert github.comments_for(10) == [] and recorder.events == []
    await apply(github, bus, NEXT)
    assert [s.title for s in await github.sub_issues(10)][2:] == ["Third step", "Fourth step"]
    assert len(await github.sub_issues(10)) == 4
    assert len(recorder.events) == 1
    assert len(github.comments_for(10)) == 1


async def test_a_retry_after_the_move_publishes_nothing_again() -> None:
    github, recorder, bus = setup(LABELS.triage_complete)
    moved: list[bool] = []
    github.fail_next("network", on="comment")
    with pytest.raises(GitHubError):
        await apply(github, bus, NEXT, on_moved=lambda: moved.append(True))
    assert moved == [True] and len(recorder.events) == 1
    await apply(github, bus, NEXT, moved=True)
    assert len(recorder.events) == 1
    assert len(github.comments_for(10)) == 1
    assert len(await github.sub_issues(10)) == 4


async def test_gap_says_which_round_it_is() -> None:
    github, recorder, bus = setup(LABELS.triage_complete)
    await apply(github, bus, GAP)
    [comment] = github.comments_for(10)
    assert "**Verdict: gap.**" in comment.body
    assert "This is gap round 1 of 2." in comment.body
    [event] = recorder.events
    assert isinstance(event, StateChanged) and event.note == "gap: 1 sub-issue"


async def test_met_closes_the_parent_as_complete() -> None:
    github, recorder, bus = setup(LABELS.triage_complete)
    github.calls.clear()
    assert await apply(github, bus, MET) == []
    parent = github.issue(10)
    assert parent.github_state == "closed"
    assert LABELS.complete in parent.labels and LABELS.triage_complete in parent.labels
    assert [
        name for name, _ in github.calls if name in ("edit_labels", "close_as_completed", "comment")
    ] == [
        "edit_labels",
        "close_as_completed",
        "comment",
    ]
    assert recorder.kinds == ["state_changed", "issue_completed"]
    changed, completed = recorder.events
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label, changed.note) == (None, LABELS.complete, "met")
    assert isinstance(completed, IssueCompleted)
    assert completed.resolution == "parent_review" and completed.pr_url is None
    [comment] = github.comments_for(10)
    assert "**Verdict: met.**" in comment.body and "- 1." not in comment.body


async def test_met_retried_after_the_close_only_comments() -> None:
    github, recorder, bus = setup(LABELS.triage_complete)
    github.fail_next("network", on="comment")
    with pytest.raises(GitHubError):
        await apply(github, bus, MET)
    await apply(github, bus, MET, moved=True)
    assert len(github.comments_for(10)) == 1
    assert len([n for n, _ in github.calls if n == "close_as_completed"]) == 1
    assert recorder.kinds == ["state_changed", "issue_completed"]


def test_comment_lists_ignored_requests_only_when_present() -> None:
    kwargs = dict(
        counts=COUNTS, run_id="r", attempt=1, turns=1, cost_usd=0.1, labels=LABELS, now=NOW
    )
    assert "did not act on" not in parent_review_comment(MET, [], **kwargs)  # type: ignore[arg-type]
    noted = parent_review_comment(make_verdict("met", ignored="Merge it."), [], **kwargs)  # type: ignore[arg-type]
    assert "- **Requests in the issue it did not act on:** Merge it." in noted
