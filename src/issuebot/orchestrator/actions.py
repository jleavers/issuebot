"""GitHub-writing actions the orchestrator takes: claim, blocked escape, terminal finish."""

from datetime import UTC, datetime
from typing import Literal

from issuebot.agent import AgentError, WorkspaceManager
from issuebot.config import GitHubLabels
from issuebot.events import Blocked, EventBus, IssueCancelled, IssueCompleted, StateChanged
from issuebot.github import (
    WORKPAD_MARKER,
    Comment,
    GitHubAdapter,
    GitHubError,
    Issue,
    StateLabel,
    classify_closed,
)
from issuebot.log import get_logger
from issuebot.orchestrator.state import (
    BlockedContext,
    claimed_snapshot,
    pr_url,
    state_label_name,
)

EscapeOutcome = Literal["applied", "skipped", "failed"]
FinishOutcome = Literal["complete", "no_change", "cancelled", "unchanged", "failed"]

CANCEL_REASON = "closed without a merged pull request"

# ``failed`` is a GitHub error the next tick may not see again (a transport error, a rate
# limit, a 5xx) and is tried then; ``gave_up`` is a ``response`` error, GitHub answering with
# something issuebot refuses to read (a page past a cap, #110, a malformed page), which is a
# property of the issue rather than of the moment, so the orchestrator remembers it until the
# issue changes rather than repeating the same bounded read on every poll.
ConflictOutcome = Literal["reworked", "limit_reached", "limit_noted", "failed", "gave_up"]

# The workpad headings the conflict bounce writes: a note for a person, never the count. The
# bounce number is read from the issue's label history (#104), which only GitHub writes.
CONFLICT_HEADING = "### Issuebot merge conflict ("
CONFLICT_LIMIT_HEADING = "### Issuebot merge conflict limit ("


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def conflict_block(
    pr_number: int, bounce: int, limit: int, now: datetime, labels: GitHubLabels
) -> str:
    """The block one bounce appends to the workpad."""
    return (
        f"{CONFLICT_HEADING}{_stamp(now)})\n\n"
        f"Pull request #{pr_number} conflicts with the default branch "
        f"(bounce {bounce} of {limit}).\n"
        f"Moved to `{labels.rework}`: the next session merges the default branch into the "
        f"branch, resolves the conflict, re-runs validation and returns the issue to "
        f"`{labels.review}`."
    )


def conflict_limit_block(pr_number: int, limit: int, now: datetime, labels: GitHubLabels) -> str:
    """The block written once when the bounces are used up; the issue stays in review."""
    times = "time" if limit == 1 else "times"
    return (
        f"{CONFLICT_LIMIT_HEADING}{_stamp(now)})\n\n"
        f"Pull request #{pr_number} conflicts with the default branch again. issuebot has "
        f"moved this issue to `{labels.rework}` {limit} {times} for it and will not again. "
        f"A human resolves the conflict on the branch, or moves the issue."
    )


async def claim(adapter: GitHubAdapter, bus: EventBus, issue: Issue) -> Issue | None:
    """Set ``in_progress`` and publish the claim; ``None`` (logged) when GitHub refuses.

    The claim also drops the markers, so each of them says what *this* session concluded. A
    no-fault marker left over from an earlier session would otherwise outlive the finding it
    records: a reworked issue that this session fixes with a pull request would still be
    labelled "no fault", and ``classify_closed`` could not tell a fresh verdict from a stale one.
    """
    log = get_logger(__name__)
    try:
        await adapter.set_state(issue.number, StateLabel.IN_PROGRESS, clear_markers=True)
    except GitHubError as exc:
        log.warning(
            "dispatch_claim_failed",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            error=str(exc),
        )
        return None
    bus.publish(
        StateChanged(
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            from_label=state_label_name(issue),
            to_label=adapter.labels.in_progress,
            actor="issuebot",
            pr_url=pr_url(issue),
        )
    )
    return claimed_snapshot(issue, adapter.labels)


def blocked_block(context: BlockedContext, now: datetime, labels: GitHubLabels) -> str:
    """The Markdown block the escape appends to the workpad."""
    stamp = _stamp(now)
    turns = "turn" if context.turns == 1 else "turns"
    logs = f"; logs: `{context.log_dir}`" if context.log_dir else ""
    return (
        f"### Issuebot blocked ({stamp})\n\n"
        f"{context.reason}\n"
        f"{_run_marker(context)} (attempt {context.attempt}, {context.turns} {turns}){logs}.\n"
        f"Moved to `{labels.review}` for a human to look at."
    )


def _run_marker(context: BlockedContext) -> str:
    return f"Run `{context.run_id}`"


async def _append_workpad(
    adapter: GitHubAdapter, number: int, workpad: Comment | None, block: str
) -> None:
    """Append ``block`` to the workpad, creating the workpad when there is none."""
    if workpad is None:
        await adapter.comment(number, f"{WORKPAD_MARKER}\n\n{block}\n")
    else:
        await adapter.update_comment(workpad.id, workpad.body.rstrip("\n") + "\n\n" + block + "\n")


async def blocked_escape(
    adapter: GitHubAdapter,
    bus: EventBus,
    issue_id: str,
    context: BlockedContext,
    *,
    now: datetime,
) -> EscapeOutcome:
    """Roadmap §1's blocked escape: workpad block, then ``review``; retried by the caller."""
    log = get_logger(__name__)
    try:
        issues = await adapter.fetch_issues_by_ids([issue_id])
        if not issues:
            log.info("blocked_escape_skipped", issue_id=issue_id, reason="issue missing")
            return "skipped"
        issue = issues[0]
        if issue.github_state == "closed" or issue.state is not StateLabel.IN_PROGRESS:
            state = "closed" if issue.github_state == "closed" else (issue.state or "unlabelled")
            log.info(
                "blocked_escape_skipped",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                reason=f"issue is {state}",
            )
            return "skipped"
        block = blocked_block(context, now, adapter.labels)
        workpad = await adapter.find_workpad_comment(issue.number)
        if workpad is None or _run_marker(context) not in workpad.body:
            await _append_workpad(adapter, issue.number, workpad, block)
        await adapter.set_state(issue.number, StateLabel.REVIEW)
    except GitHubError as exc:
        log.warning(
            "blocked_escape_failed", issue_id=issue_id, run_id=context.run_id, error=str(exc)
        )
        return "failed"
    bus.publish(
        StateChanged(
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            from_label=state_label_name(issue),
            to_label=adapter.labels.review,
            actor="issuebot",
            pr_url=pr_url(issue),
        )
    )
    bus.publish(
        Blocked(issue_number=issue.number, issue_identifier=issue.identifier, reason=context.reason)
    )
    log.info(
        "blocked_escape_applied",
        issue_number=issue.number,
        issue_identifier=issue.identifier,
        run_id=context.run_id,
        reason=context.reason,
    )
    return "applied"


async def conflict_rework(
    adapter: GitHubAdapter,
    bus: EventBus,
    issue: Issue,
    *,
    limit: int,
    now: datetime,
) -> ConflictOutcome:
    """Move a review issue whose pull request conflicts to ``rework``, at most ``limit`` times.

    The bound is read from a record only issuebot writes and nobody edits (#104): the issue's
    label history, where every ``rework`` the adapter's own account added is one bounce.
    The workpad block is the note a person reads, not the count -- the session rewrites the
    workpad's body in full, so a count kept there was the session's to zero. Label first,
    note second: the label is what the next tick counts, and a label without the note still
    gets the conflict resolved by the rework session (Step 6 of the prompt). The transition is
    published between the label and the note, so the Slack line goes out even when the note
    fails.
    """
    log = get_logger(__name__)
    pr = issue.linked_pr
    if pr is None:
        log.warning(
            "conflict_rework_failed",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            error="issue has no linked pull request",
        )
        return "failed"
    try:
        bounces = await adapter.count_own_label_additions(issue.number, adapter.labels.rework)
        workpad = await adapter.find_workpad_comment(issue.number)
        body = workpad.body if workpad is not None else ""
        if bounces >= limit:
            if CONFLICT_LIMIT_HEADING in body:
                return "limit_noted"
            block = conflict_limit_block(pr.number, limit, now, adapter.labels)
            await _append_workpad(adapter, issue.number, workpad, block)
            log.warning(
                "conflict_rework_limit",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                pr_number=pr.number,
                limit=limit,
            )
            return "limit_reached"
        await adapter.set_state(issue.number, StateLabel.REWORK)
    except GitHubError as exc:
        log.warning(
            "conflict_rework_failed",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            pr_number=pr.number,
            error=str(exc),
            retryable=exc.retryable,
        )
        return "gave_up" if exc.category == "response" else "failed"
    bus.publish(
        StateChanged(
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            from_label=state_label_name(issue),
            to_label=adapter.labels.rework,
            actor="issuebot",
            pr_url=pr.url,
        )
    )
    log.info(
        "conflict_rework",
        issue_number=issue.number,
        issue_identifier=issue.identifier,
        pr_number=pr.number,
        bounce=bounces + 1,
        limit=limit,
    )
    try:
        block = conflict_block(pr.number, bounces + 1, limit, now, adapter.labels)
        await _append_workpad(adapter, issue.number, workpad, block)
    except GitHubError as exc:
        # The label moved, so the session will resolve it and the bounce is counted; only
        # the note a person would read is missing.
        log.warning(
            "conflict_rework_note_failed",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            pr_number=pr.number,
            error=str(exc),
        )
    return "reworked"


async def finish_terminal(
    adapter: GitHubAdapter,
    bus: EventBus,
    workspaces: WorkspaceManager,
    issue: Issue,
) -> FinishOutcome:
    """A closed issue: ``complete``, no-change or cancelled, the events, workspace removed."""
    log = get_logger(__name__)
    outcome: FinishOutcome
    if issue.state is StateLabel.COMPLETE:
        outcome = "unchanged"
    else:
        outcome = classify_closed(issue, adapter.labels)
        try:
            if outcome in ("complete", "no_change"):
                # Both rest in `complete`: the resolution, not the state, is what differs.
                await adapter.set_state(issue.number, StateLabel.COMPLETE)
                bus.publish(
                    StateChanged(
                        issue_number=issue.number,
                        issue_identifier=issue.identifier,
                        from_label=state_label_name(issue),
                        to_label=adapter.labels.complete,
                        actor="issuebot",
                        pr_url=pr_url(issue),
                    )
                )
                bus.publish(
                    IssueCompleted(
                        issue_number=issue.number,
                        issue_identifier=issue.identifier,
                        pr_url=pr_url(issue),
                        resolution="merged_pr" if outcome == "complete" else "no_change",
                    )
                )
            else:
                await adapter.clear_state(issue.number)
                bus.publish(
                    StateChanged(
                        issue_number=issue.number,
                        issue_identifier=issue.identifier,
                        from_label=state_label_name(issue),
                        to_label=None,
                        actor="issuebot",
                        pr_url=pr_url(issue),
                    )
                )
                bus.publish(
                    IssueCancelled(
                        issue_number=issue.number,
                        issue_identifier=issue.identifier,
                        reason=CANCEL_REASON,
                    )
                )
        except GitHubError as exc:
            log.warning(
                "issue_finish_failed",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                outcome=outcome,
                error=str(exc),
            )
            outcome = "failed"
        else:
            log.info(
                "issue_finished",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                outcome=outcome,
                pr_url=pr_url(issue),
            )
    await remove_workspace(workspaces, issue.identifier)
    return outcome


async def remove_workspace(workspaces: WorkspaceManager, identifier: str) -> bool:
    """``workspaces.remove`` with the AgentError contained and logged."""
    try:
        return await workspaces.remove(identifier)
    except AgentError as exc:
        get_logger(__name__).warning(
            "workspace_remove_failed", issue_identifier=identifier, error=exc.message
        )
        return False
