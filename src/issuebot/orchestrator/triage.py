"""Filing a triage verdict: every GitHub write the session is not trusted to make.

The session ends on a verdict and moves nothing (spec 2026-10-25, section 3); this module is
the worker acting on it. The agent never chooses a label: ``operator_step`` is applied here,
only to drafts of kind ``operator``, and no state label is ever added to a sub-issue.
"""

from collections.abc import Callable, Sequence
from datetime import datetime

from issuebot.agent.verdict import SubIssueDraft, TriageVerdict
from issuebot.config import GitHubLabels
from issuebot.events import EventBus, StateChanged
from issuebot.github import GitHubAdapter, Issue, SubIssue
from issuebot.orchestrator.actions import _stamp


def is_triage_candidate(issue: Issue, labels: GitHubLabels) -> bool:
    """Open, carrying ``triage_todo`` and no state label (``Issue.labels`` is lower-cased)."""
    return (
        issue.github_state == "open"
        and not issue.state_labels
        and labels.triage_todo.lower() in issue.labels
    )


def sub_issue_body(
    draft: SubIssueDraft, *, parent: int, step: int, steps: int, labels: GitHubLabels
) -> str:
    """The draft's body, with the operator preamble when it is one and the parent footer."""
    preamble = ""
    if draft.kind == "operator":
        preamble = (
            "**Operator step.** Do this yourself, outside issuebot, then close this issue. "
            f"issuebot does not run it, even with `{labels.todo}`.\n\n"
        )
    return (
        f"{preamble}{draft.body}\n\n---\n"
        f"Part of #{parent}: step {step} of {steps}, filed by issuebot triage."
    )


def verdict_note(verdict: TriageVerdict) -> str:
    if verdict.verdict == "fits":
        return "fits one run"
    return f"split into {len(verdict.sub_issues)} sub-issues"


def verdict_comment(
    verdict: TriageVerdict,
    filed: Sequence[SubIssue],
    *,
    run_id: str,
    attempt: int,
    turns: int,
    cost_usd: float,
    labels: GitHubLabels,
    now: datetime,
) -> str:
    lines = [f"### Issuebot triage ({_stamp(now)})", ""]
    if verdict.verdict == "fits":
        lines.append(
            f"**Verdict: fits.** One issuebot run should finish this. Add `{labels.todo}` "
            f"to start it."
        )
    else:
        lines.append(
            f"**Verdict: split.** Too big for one run, so it is filed as "
            f"{len(verdict.sub_issues)} sub-issues, in order. Start each issuebot step with "
            f"`{labels.todo}` once the step before it has closed. Operator steps "
            f"(`{labels.operator_step}`) are yours to do and close."
        )
        lines.append("")
        for number, (sub, draft) in enumerate(zip(filed, verdict.sub_issues, strict=True), 1):
            suffix = " -- operator step" if draft.kind == "operator" else ""
            lines.append(f"{number}. #{sub.number} {sub.title}{suffix}")
    lines += ["", verdict.summary, ""]
    lines += [
        f"- **Fits the limits:** {verdict.fits_caps}",
        f"- **Lands fast:** {verdict.lands_fast}",
        f"- **Needs a human first:** {verdict.needs_human}",
    ]
    if verdict.ignored_requests:
        lines.append(f"- **Requests in the issue it did not act on:** {verdict.ignored_requests}")
    lines += [
        "",
        f"Run `{run_id}` (attempt {attempt}, {turns} turns, ${cost_usd:.2f}).",
    ]
    return "\n".join(lines)


def _key(title: str) -> str:
    return title.strip().casefold()


async def apply_verdict(
    adapter: GitHubAdapter,
    bus: EventBus,
    issue: Issue,
    verdict: TriageVerdict,
    *,
    run_id: str,
    attempt: int,
    turns: int,
    cost_usd: float,
    now: datetime,
    on_swapped: Callable[[], None] | None = None,
    swapped: bool = False,
) -> list[SubIssue]:
    """File ``verdict``; raises ``GitHubError`` from any step, so the caller retries.

    Each step is idempotent, and the order is what makes a retry safe: sub-issues are matched
    by title against the parent's children, the label swap is a set operation, and the comment
    -- the one write that would show twice -- is last, so a failure before it leaves at most a
    label move to redo. ``on_swapped`` is called once the swap has landed, so a caller keeping
    the verdict for a retry knows when only the comment is left, and passes ``swapped`` on that
    retry: the swap is still made, being a set operation, but the StateChanged that announced
    it is not published again (#308), since a comment that fails on every tick would otherwise
    send Slack the same move once per tick.
    """
    labels = adapter.labels
    filed: list[SubIssue] = []
    if verdict.verdict == "split":
        existing = {_key(child.title): child for child in await adapter.sub_issues(issue.number)}
        steps = len(verdict.sub_issues)
        for step, draft in enumerate(verdict.sub_issues, 1):
            child = existing.get(_key(draft.title))
            if child is None:
                child = await adapter.create_sub_issue(
                    issue.number,
                    title=draft.title,
                    body=sub_issue_body(
                        draft, parent=issue.number, step=step, steps=steps, labels=labels
                    ),
                )
                existing[_key(draft.title)] = child
            filed.append(child)
            if draft.kind == "operator":
                await adapter.edit_labels(child.number, add=[labels.operator_step])
    await adapter.edit_labels(
        issue.number, add=[labels.triage_complete], remove=[labels.triage_todo]
    )
    if on_swapped is not None:
        on_swapped()
    if not swapped:
        bus.publish(
            StateChanged(
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                from_label=labels.triage_todo,
                to_label=labels.triage_complete,
                actor="issuebot",
                note=verdict_note(verdict),
            )
        )
    await adapter.comment(
        issue.number,
        verdict_comment(
            verdict,
            filed,
            run_id=run_id,
            attempt=attempt,
            turns=turns,
            cost_usd=cost_usd,
            labels=labels,
            now=now,
        ),
    )
    return filed
