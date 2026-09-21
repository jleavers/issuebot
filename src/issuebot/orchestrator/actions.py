"""GitHub-writing actions the orchestrator takes: claim, blocked escape, terminal finish."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from issuebot.agent import AgentError, WorkspaceManager
from issuebot.config import GitHubLabels
from issuebot.events import Blocked, EventBus, IssueCancelled, IssueCompleted, StateChanged
from issuebot.github import (
    ACTIVE_STATES,
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

# ``gave_up`` is a ``response`` error and only that one: GitHub answering with something
# issuebot refuses to read (a page past a cap, #110, a malformed page), which is a property of
# the issue rather than of the moment, so the orchestrator remembers it until the issue changes
# rather than repeating the same bounded read on every poll. Every other category is ``failed``
# and tried again next tick -- including the ones that will fail the same way until an operator
# acts (``auth``, ``config``, ``not_found``), because a retry there costs one request and says
# so in the log, while a memo would have the bounce stay quiet about a broken token. The split
# is on the category, not on ``retryable``: a 5xx is worth another tick, a capped read is not,
# and both of those are answers about *this* issue's size rather than about the deployment.
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


@dataclass(frozen=True)
class _NoteFailure:
    """Why an escape has no note, and which half of writing it failed: the read or the write."""

    phase: Literal["read", "write"]
    error: GitHubError


async def _escape_note(
    adapter: GitHubAdapter, number: int, block: str, written: Callable[[str], bool]
) -> _NoteFailure | None:
    """Write an escape's block, before the label moves; what failed when there is no note.

    Both escapes are label-first (#128, #157): the escape's purpose is the label move, and the
    block is only the note explaining it, so a failure that would keep answering the same way
    for the life of the process costs the note rather than the hand-over. A retryable failure
    -- ``transport``, ``rate_limited`` -- is worth another tick and is raised, since the same
    call a minute later is likely to answer. A non-retryable one is *returned*, and the caller
    moves the label and then reports it.

    Both halves can fail that way, and they are not the same failure. The read is the block's
    idempotence -- the run marker or the budget reason already in the workpad's body -- so a
    read that will not answer (a page past ``MAX_COMMENT_PAGES`` (#110), a malformed page)
    leaves the caller with a note it can only write blind, as a fresh marker comment, trading
    a possible duplicate for an issue that never leaves the state the escape found it in. A
    *write* that fails non-retryably (a ``response`` error on the POST, a ``not_found`` on a
    comment deleted between the read and the write) has already been attempted at the one
    moment it could have been idempotent, so the caller reports it and leaves it there.

    The split is on ``retryable`` where ``conflict_rework``'s is on the category, and the two
    rules answer different questions about the same read. The bounce's is "is this worth
    asking again *on every poll*", where a broken token is, because nothing else in that path
    would report it. This one is "does the label move now", and it moves for every answer that
    is not going to change in a minute -- an ``auth`` or ``config`` failure among them, since
    the ``set_state`` right behind it fails on the same fault and takes the escape back to
    ``failed`` and its retry anyway.
    """
    try:
        workpad = await adapter.find_workpad_comment(number)
    except GitHubError as exc:
        if exc.retryable:
            raise
        return _NoteFailure("read", exc)
    if workpad is None or not written(workpad.body):
        try:
            await _append_workpad(adapter, number, workpad, block)
        except GitHubError as exc:
            if exc.retryable:
                raise
            return _NoteFailure("write", exc)
    return None


async def _report_note_failure(
    adapter: GitHubAdapter,
    issue: Issue,
    block: str,
    failure: _NoteFailure,
    *,
    prefix: str,
    **fields: object,
) -> None:
    """Log an escape's missing note, and write it blind when it was the *read* that failed.

    The label has moved and the issue is a human's now; the note is what is left. After a
    failed read it is written blind, because the read that would have made it idempotent is
    the one that failed, and both halves are logged since either can be why there is no block
    to find. After a failed write there is nothing to retry here -- the append has just been
    refused non-retryably, and asking again in the same tick would only cost a second request.
    """
    log = get_logger(__name__)
    common = {"issue_number": issue.number, "issue_identifier": issue.identifier, **fields}
    error = failure.error
    if failure.phase == "read":
        log.warning(
            f"{prefix}_workpad_unreadable", **common, error=str(error), category=error.category
        )
        try:
            await _append_workpad(adapter, issue.number, None, block)
        except GitHubError as exc:
            error = exc
        else:
            return
    log.warning(f"{prefix}_note_failed", **common, error=str(error), category=error.category)


async def blocked_escape(
    adapter: GitHubAdapter,
    bus: EventBus,
    issue_id: str,
    context: BlockedContext,
    *,
    now: datetime,
) -> EscapeOutcome:
    """Roadmap §1's blocked escape: workpad block, then ``review``; retried by the caller.

    Label-first whenever the note cannot be written (#128, #157). ``_escape_note`` above
    raises what is worth retrying and returns what is not, so a non-retryable failure of
    either half -- the lookup that makes the block idempotent, or the append itself -- leaves
    this function with the label to move and no note to move it with. It moves the label and
    then reports the note, best effort. The trade-off is deliberate and stated in #128: a
    possible duplicate note against an issue that never leaves ``in_progress``.
    """
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
        note_failure = await _escape_note(
            adapter, issue.number, block, lambda body: _run_marker(context) in body
        )
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
    if note_failure is not None:
        await _report_note_failure(
            adapter,
            issue,
            block,
            note_failure,
            prefix="blocked_escape",
            run_id=context.run_id,
        )
    log.info(
        "blocked_escape_applied",
        issue_number=issue.number,
        issue_identifier=issue.identifier,
        run_id=context.run_id,
        reason=context.reason,
    )
    return "applied"


# The heading of the block a budget escape writes. Both ceilings share it, so it is not what
# makes two blocks the same block: the *reason* is, since it names the ceiling, the figure and
# the counts. A bounce that returns an over-budget issue runs no session, so it reproduces the
# reason exactly and writes nothing; an issue escalated on `attempts` that later runs up
# `max_issue_cost_usd`, or one whose operator raised the ceiling and relabelled, has a new
# reason and gets its own block -- which matters because the way out differs by ceiling, and
# the first block would name the wrong one.
BUDGET_HEADING = "### Issuebot budget limit ("
BUDGET_REASON_PREFIX = "issuebot has stopped claiming this issue: "

BudgetLimit = Literal["attempts", "spend"]

# What actually gets the issue moving again, which is not the same for the two ceilings: the
# escape clears the failure chain on its way out, so relabelling is enough for `attempts` and
# is not for `spend`, where the figure the ceiling compares against never resets. A note that
# got this backwards would have an operator raise a setting and wait for nothing.
_BUDGET_RECOVERY: dict[BudgetLimit, str] = {
    "attempts": (
        "Fix what the runs kept failing on, then label the issue `{rework}` or `{todo}`: "
        "handing it over here ends the chain of failures, so the next label move starts the "
        "run budget again."
    ),
    "spend": (
        "Raise `agent.max_issue_cost_usd` (or close the issue), *then* relabel. Relabelling "
        "on its own only brings the issue back here: what this ceiling counts is what the "
        "issue has already cost, and that never resets."
    ),
}


def budget_block(limit: BudgetLimit, reason: str, now: datetime, labels: GitHubLabels) -> str:
    """The block the budget escape appends to the workpad."""
    recovery = _BUDGET_RECOVERY[limit].format(rework=labels.rework, todo=labels.todo)
    return (
        f"{BUDGET_HEADING}{_stamp(now)})\n\n"
        f"{BUDGET_REASON_PREFIX}{reason}.\n"
        f"Moved to `{labels.review}` for a human to look at. {recovery}"
    )


def _has_budget_block(body: str, reason: str) -> bool:
    """Has this issue already been told *this*? Anchored to a line of its own, so prose that
    mentions the reason is not a block; surrounding whitespace is tolerated, since a body
    fetched from GitHub may carry CRLF line endings. A quotation indented to look like the
    line would suppress the block, which costs a note and never an announcement: those are
    now two different identities, which is the point of splitting them."""
    line = f"{BUDGET_REASON_PREFIX}{reason}."
    return any(candidate.strip() == line for candidate in body.split("\n"))


async def budget_escape(
    adapter: GitHubAdapter,
    bus: EventBus,
    issue_id: str,
    limit: BudgetLimit,
    reason: str,
    *,
    now: datetime,
    announce: bool = True,
) -> EscapeOutcome:
    """Hand an issue that has spent its per-issue budget to a human (#112).

    The blocked escape above is something a *run* does, and this is the one escalation with no
    run behind it: the admission gate refused the claim before there was one. Without it a
    refused issue would sit on the board with nothing said about it anywhere a human looks,
    which is worse than having no ceiling at all. Moving it also stops the refusal repeating,
    since the gate reads a ``review`` issue as one this worker does not claim -- unless the
    conflict bounce moves it back to ``rework``, which `agent.max_conflict_reworks` bounds.

    ``announce`` is that round trip's answer, and it is the caller's to give: the ``Blocked``
    event is a Slack line and a count on the dashboard's blocked tile, and an issue bouncing
    between ``review`` and ``rework`` is one escalation being returned, not several being
    made. The decision cannot be read off the workpad here, because the block landing and the
    event going out are two writes with a failure point between them -- an escape whose
    ``set_state`` fails has left the block behind and announced nothing, and the tick that
    retries it must still be able to. So the orchestrator remembers on the issue's ledger
    entry, where only a *run* clears it, and this function returns ``applied`` exactly when it
    published. The label move is published either way: it happened, and the board should say so.

    The note is label-first for the same reason the blocked escape's is (#157): a workpad
    that cannot be read or written non-retryably would keep answering that way for the life
    of the process, and the refused issue would stay where the gate found it -- ``todo``,
    ``rework``, an orphaned ``in_progress`` -- to be refused again on every tick with the
    escalation a human would read never written. So ``_escape_note`` reports rather than
    raises there, the label moves, and the block is written blind. That is independent of
    ``announce``, which is about a second *report* of one escalation and not about whether
    the block landed, so the note is reported on both exits.

    Unlike ``blocked_escape`` it accepts the issue in any of ``ACTIVE_STATES``, because a
    refused issue is wherever the gate found it -- usually ``todo`` or ``rework``, but an
    orphaned ``in_progress`` candidate is gated before it is resumed, and a continuation retry
    fires on one too. What keeps this off a *running* issue is not the state but the gate:
    ``admit`` answers ``busy`` long before it reaches the budget.
    """
    log = get_logger(__name__)
    try:
        issues = await adapter.fetch_issues_by_ids([issue_id])
        if not issues:
            log.info("budget_escape_skipped", issue_id=issue_id, reason="issue missing")
            return "skipped"
        issue = issues[0]
        if issue.github_state == "closed" or issue.state not in ACTIVE_STATES:
            state = "closed" if issue.github_state == "closed" else (issue.state or "unlabelled")
            log.info(
                "budget_escape_skipped",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                reason=f"issue is {state}",
            )
            return "skipped"
        block = budget_block(limit, reason, now, adapter.labels)
        note_failure = await _escape_note(
            adapter, issue.number, block, lambda body: _has_budget_block(body, reason)
        )
        await adapter.set_state(issue.number, StateLabel.REVIEW)
    except GitHubError as exc:
        log.warning("budget_escape_failed", issue_id=issue_id, error=str(exc))
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
    if note_failure is not None:
        await _report_note_failure(
            adapter, issue, block, note_failure, prefix="budget_escape", reason=reason
        )
    if not announce:
        log.info(
            "budget_escape_returned",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            reason=reason,
        )
        return "skipped"
    bus.publish(
        Blocked(issue_number=issue.number, issue_identifier=issue.identifier, reason=reason)
    )
    log.warning(
        "budget_escape_applied",
        issue_number=issue.number,
        issue_identifier=issue.identifier,
        reason=reason,
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
        outcome: ConflictOutcome = "gave_up" if exc.category == "response" else "failed"
        log.warning(
            "conflict_rework_failed",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            pr_number=pr.number,
            error=str(exc),
            retryable=exc.retryable,
            outcome=outcome,
        )
        return outcome
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
