"""Tests for filing a triage verdict: the worker's writes, idempotent across a retry."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from issuebot.agent.verdict import SubIssueDraft, TriageVerdict
from issuebot.config import Settings
from issuebot.events import Event, EventBus, StateChanged
from issuebot.github import FakeGitHub, GitHubError, Issue, ParentSteps, SubIssue
from issuebot.orchestrator.triage import (
    apply_verdict,
    earlier_open_step,
    is_triage_candidate,
    sub_issue_body,
    verdict_comment,
    verdict_note,
)

NOW = datetime(2026, 10, 25, 9, 30, 0, tzinfo=UTC)
SETTINGS = Settings.model_validate({"github": {"repo": "example/repo"}})
LABELS = SETTINGS.github.labels


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)


def make_verdict(
    kind: str = "fits", drafts: tuple[SubIssueDraft, ...] = (), ignored: str = ""
) -> TriageVerdict:
    return TriageVerdict(
        verdict=kind,  # type: ignore[arg-type]
        summary="The summary.",
        fits_caps="yes",
        lands_fast="yes",
        needs_human="no",
        ignored_requests=ignored,
        sub_issues=drafts,
    )


SPLIT = make_verdict(
    "split",
    (
        SubIssueDraft(title="First step", body="Do the first.", kind="issuebot"),
        SubIssueDraft(title="Second step", body="Do it by hand.", kind="operator"),
    ),
)


def setup() -> tuple[FakeGitHub, Recorder, EventBus]:
    github = FakeGitHub(SETTINGS.github)
    recorder = Recorder()
    github.add_issue("Big task", labels=(LABELS.triage_todo,), number=1)
    return github, recorder, EventBus([recorder])


async def apply(github: FakeGitHub, bus: EventBus, verdict: TriageVerdict) -> list:
    return await apply_verdict(
        github,
        bus,
        github.issue(1),
        verdict,
        run_id="run-1",
        attempt=1,
        turns=1,
        cost_usd=0.5,
        now=NOW,
    )


async def test_a_fits_verdict_comments_and_swaps_the_label() -> None:
    github, recorder, bus = setup()
    assert await apply(github, bus, make_verdict()) == []
    assert github.issue(1).labels == (LABELS.triage_complete,)
    [comment] = github.comments_for(1)
    assert comment.body.startswith("### Issuebot triage (2026-10-25T09:30:00Z)\n\n")
    assert "**Verdict: fits.**" in comment.body
    [event] = recorder.events
    assert isinstance(event, StateChanged)
    assert event.note == "fits one run" and event.actor == "issuebot"
    assert (event.from_label, event.to_label) == (LABELS.triage_todo, LABELS.triage_complete)


async def test_a_split_files_sub_issues_in_order_under_the_parent() -> None:
    github, recorder, bus = setup()
    filed = await apply(github, bus, SPLIT)
    assert [s.number for s in filed] == [2, 3]
    first, second = github.issue(2), github.issue(3)
    assert first.title == "First step" and second.title == "Second step"
    assert LABELS.operator_step in second.labels and LABELS.operator_step not in first.labels
    assert not first.state_labels and not second.state_labels
    assert (second.body or "").startswith("**Operator step.**")
    assert (first.body or "").endswith("Part of #1: step 1 of 2, filed by issuebot triage.")
    assert (second.body or "").endswith("Part of #1: step 2 of 2, filed by issuebot triage.")
    [comment] = github.comments_for(1)
    assert comment.body.index("#2 First step") < comment.body.index("#3 Second step")
    assert "#3 Second step -- operator step" in comment.body
    [event] = recorder.events
    assert isinstance(event, StateChanged) and event.note == "split into 2 sub-issues"


async def test_a_retry_files_only_the_missing_sub_issues() -> None:
    github, _recorder, bus = setup()
    github.fail_next("server_error", on="create_sub_issue", after=1)
    with pytest.raises(GitHubError):
        await apply(github, bus, SPLIT)
    assert len(await github.sub_issues(1)) == 1
    assert github.comments_for(1) == []
    await apply(github, bus, SPLIT)
    assert [s.title for s in await github.sub_issues(1)] == ["First step", "Second step"]
    assert len(github.comments_for(1)) == 1
    assert github.issue(1).labels == (LABELS.triage_complete,)
    assert LABELS.operator_step in github.issue(3).labels


async def test_a_retry_after_the_operator_label_fails_labels_it_and_files_nothing_new() -> None:
    """SPLIT's second step is the operator's, so the first ``edit_labels`` is its label."""
    github, _recorder, bus = setup()
    github.fail_next("server_error", on="edit_labels")
    with pytest.raises(GitHubError):
        await apply(github, bus, SPLIT)
    assert [s.title for s in await github.sub_issues(1)] == ["First step", "Second step"]
    assert github.comments_for(1) == []
    assert github.issue(1).labels == (LABELS.triage_todo,)
    await apply(github, bus, SPLIT)
    assert [s.title for s in await github.sub_issues(1)] == ["First step", "Second step"]
    assert LABELS.operator_step in github.issue(3).labels
    assert github.issue(1).labels == (LABELS.triage_complete,)
    assert len(github.comments_for(1)) == 1


@pytest.mark.parametrize(
    ("verdict", "after"),
    [(SPLIT, 1), (make_verdict(), 0)],
    ids=["split", "fits"],
)
async def test_a_retry_after_the_swap_fails_swaps_announces_and_comments_once(
    verdict: TriageVerdict, after: int
) -> None:
    """A failed swap leaves nothing shown: no comment and no StateChanged.

    ``after`` skips the operator label, which a split's first ``edit_labels`` is and a
    ``fits``, filing no sub-issue, never makes.
    """
    github, recorder, bus = setup()
    github.fail_next("server_error", on="edit_labels", after=after)
    with pytest.raises(GitHubError):
        await apply(github, bus, verdict)
    assert github.issue(1).labels == (LABELS.triage_todo,)
    assert github.comments_for(1) == []
    assert recorder.events == []
    await apply(github, bus, verdict)
    titles = [draft.title for draft in verdict.sub_issues]
    assert [s.title for s in await github.sub_issues(1)] == titles
    assert github.issue(1).labels == (LABELS.triage_complete,)
    [event] = recorder.events
    assert isinstance(event, StateChanged)
    assert len(github.comments_for(1)) == 1


async def test_an_existing_child_matches_its_draft_by_title_ignoring_case_and_padding() -> None:
    github, _recorder, bus = setup()
    github.add_issue("  first STEP ", number=2)
    github.link_sub_issue(1, 2)
    filed = await apply(github, bus, SPLIT)
    assert [s.number for s in filed] == [2, 3]
    assert filed[1].title == "Second step"
    assert [s.number for s in await github.sub_issues(1)] == [2, 3]


async def test_a_child_a_human_added_is_left_alone_and_out_of_the_comment() -> None:
    github, _recorder, bus = setup()
    github.add_issue("Human's own step", body="Mine.", labels=("bug",), number=2)
    github.link_sub_issue(1, 2)
    before = github.issue(2)
    filed = await apply(github, bus, SPLIT)
    assert github.issue(2) == before
    assert before.github_state == "open" and before.labels == ("bug",)
    assert [s.number for s in filed] == [3, 4]
    assert [s.title for s in await github.sub_issues(1)] == [
        "Human's own step",
        "First step",
        "Second step",
    ]
    [comment] = github.comments_for(1)
    assert "Human's own step" not in comment.body and "#2 " not in comment.body
    assert "1. #3 First step\n2. #4 Second step -- operator step\n" in comment.body


async def test_on_swapped_reports_the_swap_and_only_the_swap() -> None:
    """A caller keeping the verdict for a retry learns when only the comment is left."""
    github, _recorder, bus = setup()
    swapped: list[bool] = []

    async def file() -> None:
        with pytest.raises(GitHubError):
            await apply_verdict(
                github,
                bus,
                github.issue(1),
                SPLIT,
                run_id="r",
                attempt=1,
                turns=1,
                cost_usd=0.0,
                now=NOW,
                on_swapped=lambda: swapped.append(True),
            )

    github.fail_next("server_error", on="create_sub_issue")
    await file()
    assert swapped == []
    github.fail_next("server_error", on="comment")
    await file()
    assert swapped == [True]
    assert github.issue(1).labels == (LABELS.triage_complete,)


def test_sub_issue_body_for_an_issuebot_step_has_no_operator_preamble() -> None:
    body = sub_issue_body(SPLIT.sub_issues[0], parent=1, step=1, steps=2, labels=LABELS)
    assert body == "Do the first.\n\n---\nPart of #1: step 1 of 2, filed by issuebot triage."
    operator = sub_issue_body(SPLIT.sub_issues[1], parent=1, step=2, steps=2, labels=LABELS)
    assert operator.startswith(
        "**Operator step.** Do this yourself, outside issuebot, then close this issue. "
        "issuebot does not run it, even with `issuebot/todo`.\n\n"
    )


def test_verdict_note() -> None:
    assert verdict_note(make_verdict()) == "fits one run"
    assert verdict_note(SPLIT) == "split into 2 sub-issues"


def test_is_triage_candidate() -> None:
    github, _, _ = setup()
    assert is_triage_candidate(github.issue(1), LABELS)
    github.add_issue("Both", labels=(LABELS.triage_todo, LABELS.todo), number=5)
    assert not is_triage_candidate(github.issue(5), LABELS)
    github.add_issue("Plain", number=6)
    assert not is_triage_candidate(github.issue(6), LABELS)
    github.add_issue("Shouting", labels=("Issuebot/Triage-Todo",), number=7)
    assert is_triage_candidate(github.issue(7), LABELS)
    closed = replace(github.issue(1), github_state="closed")
    assert not is_triage_candidate(closed, LABELS)


def test_ignored_requests_appear_only_when_present() -> None:
    kwargs = dict(run_id="r", attempt=1, turns=2, cost_usd=0.256, labels=LABELS, now=NOW)
    plain = verdict_comment(make_verdict(), [], **kwargs)
    assert "did not act on" not in plain
    assert plain.endswith("Run `r` (attempt 1, 2 turns, $0.26).")
    noted = verdict_comment(make_verdict(ignored="Merge it."), [], **kwargs)
    assert "- **Requests in the issue it did not act on:** Merge it." in noted


async def test_a_retry_after_the_swap_posts_the_comment_and_no_second_state_change() -> None:
    """#308: the StateChanged went out with the swap; a retry owed only the comment is silent."""
    github, recorder, bus = setup()
    swapped: list[bool] = []
    for _ in range(2):
        github.fail_next("server_error", on="comment")
        with pytest.raises(GitHubError):
            await apply_verdict(
                github,
                bus,
                github.issue(1),
                make_verdict(),
                run_id="r",
                attempt=1,
                turns=1,
                cost_usd=0.0,
                now=NOW,
                on_swapped=lambda: swapped.append(True),
                swapped=bool(swapped),
            )
    await apply_verdict(
        github,
        bus,
        github.issue(1),
        make_verdict(),
        run_id="r",
        attempt=1,
        turns=1,
        cost_usd=0.0,
        now=NOW,
        swapped=True,
    )
    assert len(github.comments_for(1)) == 1
    assert github.issue(1).labels == (LABELS.triage_complete,)
    [event] = recorder.events
    assert isinstance(event, StateChanged)


def test_the_run_line_counts_one_turn_in_the_singular() -> None:
    """#305: "1 turns" read wrong on every one-turn triage; two and up keep the plural."""
    kwargs = dict(run_id="r", attempt=1, cost_usd=0.24, labels=LABELS, now=NOW)
    one = verdict_comment(make_verdict(), [], turns=1, **kwargs)
    assert one.endswith("Run `r` (attempt 1, 1 turn, $0.24).")
    two = verdict_comment(make_verdict(), [], turns=2, **kwargs)
    assert two.endswith("Run `r` (attempt 1, 2 turns, $0.24).")


def parent_order(
    *states: tuple[int, str], labels: tuple[str, ...] = ("issuebot/triage-complete",)
) -> ParentSteps:
    subs = tuple(SubIssue(number=n, title=f"s{n}", url=f"u{n}", state=s) for n, s in states)
    return ParentSteps(number=10, labels=labels, sub_issues=subs)


def test_a_step_waits_for_the_first_open_step_before_it(
    make_issue: Callable[..., Issue],
) -> None:
    order = parent_order((11, "closed"), (12, "open"), (13, "open"), (14, "open"))
    assert earlier_open_step(make_issue(number=14, parent=10), order, LABELS) == 12


def test_the_first_step_and_a_step_after_closed_ones_wait_for_nothing(
    make_issue: Callable[..., Issue],
) -> None:
    order = parent_order((11, "closed"), (12, "open"), (13, "open"))
    assert earlier_open_step(make_issue(number=12, parent=10), order, LABELS) is None
    assert earlier_open_step(make_issue(number=11, parent=10), order, LABELS) is None


def test_a_parent_without_triage_complete_holds_nothing(
    make_issue: Callable[..., Issue],
) -> None:
    order = parent_order((11, "open"), (12, "open"), labels=())
    assert earlier_open_step(make_issue(number=12, parent=10), order, LABELS) is None


def test_a_step_missing_from_its_parent_holds_nothing(
    make_issue: Callable[..., Issue],
) -> None:
    order = parent_order((11, "open"))
    assert earlier_open_step(make_issue(number=12, parent=10), order, LABELS) is None


def test_the_order_is_githubs(make_issue: Callable[..., Issue]) -> None:
    order = parent_order((13, "open"), (12, "open"))  # dragged on GitHub: 13 now comes first
    assert earlier_open_step(make_issue(number=12, parent=10), order, LABELS) == 13


async def test_a_split_comment_says_the_steps_can_be_labelled_at_once() -> None:
    github, _recorder, bus = setup()
    await apply(github, bus, SPLIT)
    [comment] = github.comments_for(1)
    assert (
        "Label every issuebot step `issuebot/todo` at once if you like: issuebot starts each "
        "only when every step before it has closed."
    ) in comment.body
    assert "once the step before it has closed" not in comment.body
