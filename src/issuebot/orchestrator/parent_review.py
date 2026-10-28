"""Filing a parent review's verdict: the worker acting on what the session decided.

When every step of a split parent has closed, a review session reads the parent against its
original ask and ends on a verdict (spec 2026-10-25, section 6): ``next`` files the following
batch, ``gap`` files one step per gap, ``met`` closes the parent. Like triage's, the session
moves nothing; this module is the worker doing it, in an order that makes a retry safe.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from issuebot.agent.verdict import ParentReviewVerdict
from issuebot.config import GitHubLabels
from issuebot.events import EventBus, IssueCompleted, ParentReviewState, StateChanged
from issuebot.github import GitHubAdapter, Issue, ParentSteps, SubIssue
from issuebot.orchestrator.stamps import utc_stamp
from issuebot.orchestrator.triage import _key, plural, sub_issue_body

FILED_BY = "issuebot's parent review"


@dataclass(frozen=True, kw_only=True, slots=True)
class ParentRecord:
    """What the worker remembers of one split parent: reviews acted on, gap rounds used, the
    hand-over (its reason) and whether the parent is finished. Each change is published whole
    as a ``ParentReviewState``, which is how a restarted worker reads it back."""

    identifier: str
    reviews: int = 0
    gap_rounds: int = 0
    handed_over: str | None = None
    finished: bool = False

    @property
    def watched(self) -> bool:
        return self.handed_over is None and not self.finished

    def state_event(self, number: int) -> ParentReviewState:
        return ParentReviewState(
            issue_number=number,
            issue_identifier=self.identifier,
            reviews=self.reviews,
            gap_rounds=self.gap_rounds,
            handed_over=self.handed_over,
            finished=self.finished,
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class ParentHandOver:
    """A parent hand-over GitHub would not let the worker write, kept to write again: its
    title, its reason (already scrubbed) and the way back the note gives."""

    title: str
    reason: str
    way_back: str


@dataclass(frozen=True, kw_only=True, slots=True)
class ReviewCounts:
    """The counts after this review: a ``gap`` has already added one to the gap rounds."""

    review: int
    max_reviews: int
    gap_round: int
    max_gap_rounds: int


def parent_review_due(steps: ParentSteps, labels: GitHubLabels) -> bool:
    """Open, ``triage_complete``, no ``triage_todo`` and no state label, and every step closed.

    A parent with no steps is not due: there is nothing to have finished.
    """
    held = {label.lower() for label in (labels.triage_todo, *labels.as_tuple())}
    return (
        steps.state == "open"
        and labels.triage_complete.lower() in steps.labels
        and not held.intersection(steps.labels)
        and bool(steps.sub_issues)
        and all(step.state == "closed" for step in steps.sub_issues)
    )


def steps_as_seen(steps: ParentSteps, seen: Sequence[int], verdict: ParentReviewVerdict) -> bool:
    """Whether ``steps`` are still the ones a review saw (``seen``), every one closed.

    Steps after those pass only when ``verdict`` files them, matched by title as
    ``apply_parent_review`` matches them: an earlier try at filing it created them before it
    failed, and the verdict is owed once its first write has landed. Anything else -- a step
    reopened, added or unlinked -- makes the verdict stale.
    """
    head, rest = steps.sub_issues[: len(seen)], steps.sub_issues[len(seen) :]
    drafts = {_key(draft.title) for draft in verdict.sub_issues}
    return (
        tuple(step.number for step in head) == tuple(seen)
        and all(step.state == "closed" for step in head)
        and all(_key(step.title) in drafts for step in rest)
    )


def parent_review_note(verdict: ParentReviewVerdict) -> str:
    if verdict.verdict == "met":
        return "met"
    return f"{verdict.verdict}: {plural(len(verdict.sub_issues), 'sub-issue')}"


def parent_review_comment(
    verdict: ParentReviewVerdict,
    filed: Sequence[SubIssue],
    *,
    counts: ReviewCounts,
    run_id: str,
    attempt: int,
    turns: int,
    cost_usd: float,
    labels: GitHubLabels,
    now: datetime,
) -> str:
    lines = [f"### Issuebot parent review ({utc_stamp(now)})", ""]
    if verdict.verdict == "next":
        lines.append(
            f"**Verdict: next.** The steps so far were a phase, so issuebot has filed the "
            f"next {len(verdict.sub_issues)} sub-issues, in order, after the others. Label "
            f"every issuebot step `{labels.todo}` at once if you like: issuebot starts each "
            f"only when every step before it has closed."
        )
    elif verdict.verdict == "gap":
        lines.append(
            f"**Verdict: gap.** Something the original ask needs is missing, so issuebot has "
            f"filed one sub-issue per gap. This is gap round {counts.gap_round} of "
            f"{counts.max_gap_rounds}."
        )
    else:
        lines.append(
            f"**Verdict: met.** The original ask is met, so issuebot has closed this issue "
            f"and labelled it `{labels.complete}`."
        )
    if filed:
        lines.append("")
        for number, (sub, draft) in enumerate(zip(filed, verdict.sub_issues, strict=True), 1):
            suffix = " -- operator step" if draft.kind == "operator" else ""
            lines.append(f"{number}. #{sub.number} {sub.title}{suffix}")
    lines += ["", verdict.summary, ""]
    lines.append(f"- **Evidence:** {verdict.evidence}")
    if verdict.ignored_requests:
        lines.append(f"- **Requests in the issue it did not act on:** {verdict.ignored_requests}")
    lines += [
        "",
        f"Run `{run_id}` (attempt {attempt}, {plural(turns, 'turn')}, ${cost_usd:.2f}). "
        f"Review {counts.review} of at most {counts.max_reviews}.",
    ]
    return "\n".join(lines)


async def apply_parent_review(
    adapter: GitHubAdapter,
    bus: EventBus,
    issue: Issue,
    verdict: ParentReviewVerdict,
    *,
    seen: Sequence[int],
    counts: ReviewCounts,
    run_id: str,
    attempt: int,
    turns: int,
    cost_usd: float,
    now: datetime,
    on_moved: Callable[[], None] | None = None,
    moved: bool = False,
) -> list[SubIssue]:
    """File ``verdict``; raises ``GitHubError`` from any step, so the caller retries.

    The shape is ``apply_verdict``'s: idempotent writes, the one that would show twice (the
    comment) last, and ``on_moved`` called once the move has landed -- the label swap for a
    ``next`` or ``gap``, the close for a ``met`` -- so a caller keeping the verdict knows when
    only the comment is left. It passes ``moved`` on that retry, and the events that announced
    the move are not published again (#308). A retry matches drafts by title only against
    children the review did not see (``seen``), so a new step may reuse an earlier step's title.
    """
    labels = adapter.labels
    filed: list[SubIssue] = []
    if verdict.verdict == "met":
        if not moved:
            await adapter.edit_labels(
                issue.number,
                add=[labels.complete, labels.triage_complete],
                remove=[labels.triage_todo],
            )
            await adapter.close_as_completed(issue.number)
            if on_moved is not None:
                on_moved()
            bus.publish(
                StateChanged(
                    issue_number=issue.number,
                    issue_identifier=issue.identifier,
                    from_label=None,
                    to_label=labels.complete,
                    actor="issuebot",
                    note="met",
                )
            )
            bus.publish(
                IssueCompleted(
                    issue_number=issue.number,
                    issue_identifier=issue.identifier,
                    pr_url=None,
                    resolution="parent_review",
                )
            )
    else:
        known = set(seen)
        existing = {
            _key(child.title): child
            for child in await adapter.sub_issues(issue.number)
            if child.number not in known
        }
        before = len(seen)
        total = before + len(verdict.sub_issues)
        for step, draft in enumerate(verdict.sub_issues, before + 1):
            child = existing.get(_key(draft.title))
            if child is None:
                child = await adapter.create_sub_issue(
                    issue.number,
                    title=draft.title,
                    body=sub_issue_body(
                        draft,
                        parent=issue.number,
                        step=step,
                        steps=total,
                        labels=labels,
                        filed_by=FILED_BY,
                    ),
                )
                existing[_key(draft.title)] = child
            filed.append(child)
            if draft.kind == "operator":
                await adapter.edit_labels(child.number, add=[labels.operator_step])
        await adapter.edit_labels(
            issue.number, add=[labels.triage_complete], remove=[labels.triage_todo]
        )
        if on_moved is not None:
            on_moved()
        if not moved:
            bus.publish(
                StateChanged(
                    issue_number=issue.number,
                    issue_identifier=issue.identifier,
                    from_label=(
                        labels.triage_todo
                        if labels.triage_todo.lower() in issue.labels
                        else labels.triage_complete
                    ),
                    to_label=labels.triage_complete,
                    actor="issuebot",
                    note=parent_review_note(verdict),
                )
            )
    await adapter.comment(
        issue.number,
        parent_review_comment(
            verdict,
            filed,
            counts=counts,
            run_id=run_id,
            attempt=attempt,
            turns=turns,
            cost_usd=cost_usd,
            labels=labels,
            now=now,
        ),
    )
    return filed
