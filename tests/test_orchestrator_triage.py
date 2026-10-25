"""Tests for filing a triage verdict: the worker's writes, idempotent across a retry."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from issuebot.agent.verdict import SubIssueDraft, TriageVerdict
from issuebot.config import Settings
from issuebot.events import Event, EventBus, StateChanged
from issuebot.github import FakeGitHub, GitHubError
from issuebot.orchestrator.triage import (
    apply_verdict,
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
