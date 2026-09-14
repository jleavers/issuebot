"""One worker session: workspace, before_run, turns with refresh between them, RunResult."""

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from issuebot.agent.errors import AgentError, AgentErrorCategory, outcome_for
from issuebot.agent.prompt import PromptContext, PromptRenderer
from issuebot.agent.runner import TurnObserver, TurnResult, TurnRunner
from issuebot.agent.workspace import SessionRecord, WorkspaceManager, run_log_dir
from issuebot.config import Workflow
from issuebot.events import EventBus, RunEnded, RunOutcome, RunStarted
from issuebot.github import Comment, GitHubAdapter, GitHubError, Issue, StateLabel
from issuebot.log import bind_issue_context, bind_session_context, clear_context, get_logger

StopReason = Literal["issue_moved", "max_turns", "issue_missing", "failure", "cancelled", "blocked"]

# The first line of a blocked turn's final message (prompt ground rule 2). Read by the session,
# so the escape happens at the end of that turn rather than after max_turns.
BLOCKED_MARKER = "BLOCKED:"

# The reason is one line by contract and the workpad's Blockers section is the long-form
# brief, so the escape block, the Slack line and the log never carry more than this.
BLOCKER_LIMIT = 500


def blocker_from(result_text: str | None) -> str | None:
    """The blocker line's reason when the turn's final message begins with the marker.

    Only the first non-empty line counts, and only when it starts with the marker: a message
    that mentions the word later is a report, not a stop. An empty reason reads as no marker,
    so a bare ``BLOCKED:`` cannot escape an issue with an empty block. The reason is capped at
    ``BLOCKER_LIMIT`` characters.
    """
    if not result_text:
        return None
    for line in result_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith(BLOCKED_MARKER):
            return None
        reason = stripped[len(BLOCKED_MARKER) :].strip()[:BLOCKER_LIMIT]
        return reason or None
    return None


@dataclass(frozen=True, kw_only=True, slots=True)
class RunResult:
    """What one worker session did and why it stopped."""

    run_id: str
    issue_number: int
    issue_identifier: str
    attempt: int
    session_id: str
    outcome: RunOutcome
    stop_reason: StopReason
    error_category: AgentErrorCategory | None
    error: str | None
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_s: float
    final_state: StateLabel | None
    final_issue: Issue | None
    workspace_path: Path | None
    log_dir: Path | None
    # The reason after ``BLOCKED:`` on a blocked turn's final message; None for every other stop.
    blocker: str | None = None


def new_run_id(now: datetime | None = None) -> str:
    """A sortable, readable run id: ``20260903T081200Z-a1b2c3``."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


@dataclass
class _State:
    run_id: str
    session_id: str
    attempt: int
    issue: Issue
    started: float
    workspace_path: Path | None = None
    log_dir: Path | None = None
    final_issue: Issue | None = None
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    outcome: RunOutcome = "succeeded"
    stop_reason: StopReason | None = None
    error_category: AgentErrorCategory | None = None
    error: str | None = None
    blocker: str | None = None
    workpad: Comment | None = None

    def fail(self, category: AgentErrorCategory, message: str | None) -> None:
        self.error_category = category
        self.error = message or category
        self.outcome = outcome_for(category)
        self.stop_reason = "cancelled" if category == "cancelled" else "failure"

    def stop(self, reason: StopReason) -> None:
        self.stop_reason = reason
        self.outcome = "succeeded"

    def record_turn(self, turn: TurnResult) -> None:
        self.turns += 1
        self.input_tokens += turn.total_input_tokens
        self.output_tokens += turn.output_tokens
        self.cost_usd += turn.cost_usd

    def session_record(self, turn_number: int, last_outcome: RunOutcome | None) -> SessionRecord:
        return SessionRecord(
            issue_number=self.issue.number,
            issue_identifier=self.issue.identifier,
            run_id=self.run_id,
            session_id=self.session_id,
            attempt=self.attempt,
            turn_number=turn_number,
            last_outcome=last_outcome,
            updated_at=datetime.now(UTC),
            workpad_comment_id=self.workpad.id if self.workpad is not None else None,
        )

    def result(self) -> RunResult:
        return RunResult(
            run_id=self.run_id,
            issue_number=self.issue.number,
            issue_identifier=self.issue.identifier,
            attempt=self.attempt,
            session_id=self.session_id,
            outcome=self.outcome,
            stop_reason=self.stop_reason or "failure",
            error_category=self.error_category,
            error=self.error,
            turns=self.turns,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=round(self.cost_usd, 6),
            duration_s=round(time.monotonic() - self.started, 3),
            final_state=self.issue.state,
            final_issue=self.final_issue,
            workspace_path=self.workspace_path,
            log_dir=self.log_dir,
            blocker=self.blocker,
        )


async def run_session(
    issue: Issue,
    workflow: Workflow,
    adapter: GitHubAdapter,
    bus: EventBus,
    *,
    workspaces: WorkspaceManager,
    runner: TurnRunner,
    attempt: int = 1,
    rework: bool = False,
    resume_session_id: str | None = None,
    cancel: asyncio.Event | None = None,
    observer: TurnObserver | None = None,
    run_id: str | None = None,
) -> RunResult:
    """Run one worker session for ``issue`` (roadmap §2.4) and report what happened."""
    state = _State(
        run_id=run_id or new_run_id(),
        session_id=resume_session_id or str(uuid.uuid4()),
        attempt=attempt,
        issue=issue,
        started=time.monotonic(),
    )
    log = get_logger(__name__)
    bind_issue_context(issue_number=issue.number, issue_identifier=issue.identifier)
    bind_session_context(session_id=state.session_id)
    try:
        try:
            state.workspace_path = workspaces.path_for(issue.identifier)
        except AgentError as exc:
            state.fail(exc.category, exc.message)
        bus.publish(
            RunStarted(
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                run_id=state.run_id,
                attempt=attempt,
                session_id=state.session_id,
                workspace_path=str(state.workspace_path or ""),
            )
        )
        log.info(
            "run_started",
            run_id=state.run_id,
            attempt=attempt,
            rework=rework,
            resuming=resume_session_id is not None,
        )
        if state.stop_reason is None:
            await _execute(
                state,
                workflow,
                adapter,
                workspaces,
                runner,
                rework=rework,
                resuming=resume_session_id is not None,
                cancel=cancel,
                observer=observer,
            )
        result = state.result()
        bus.publish(
            RunEnded(
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                run_id=result.run_id,
                outcome=result.outcome,
                error=_error_text(result),
                turns=result.turns,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=result.cost_usd,
                duration_s=result.duration_s,
                log_dir=str(result.log_dir) if result.log_dir is not None else None,
            )
        )
        log.info(
            "run_finished",
            run_id=result.run_id,
            outcome=result.outcome,
            stop_reason=result.stop_reason,
            turns=result.turns,
            cost_usd=result.cost_usd,
            error=_error_text(result),
            blocker=result.blocker,
        )
        return result
    finally:
        clear_context()


def _error_text(result: RunResult) -> str | None:
    if result.error_category is None:
        return None
    return f"{result.error_category}: {result.error}"


async def _execute(
    state: _State,
    workflow: Workflow,
    adapter: GitHubAdapter,
    workspaces: WorkspaceManager,
    runner: TurnRunner,
    *,
    rework: bool,
    resuming: bool,
    cancel: asyncio.Event | None,
    observer: TurnObserver | None,
) -> None:
    try:
        workspace = await workspaces.create_or_reuse(state.issue)
    except AgentError as exc:
        state.fail(exc.category, exc.message)
        return
    state.workspace_path = workspace.path
    state.log_dir = run_log_dir(workspace.path, state.run_id)
    # Before any claude turn: a prior session in this or another repository shares the account's
    # ~/.claude, so clear the config surfaces it could have planted there (#101).
    await workspaces.sweep_agent_home()
    try:
        hook = await workspaces.run_hook("before_run", workspace.path)
        if hook is not None and not hook.ok:
            state.fail("hook_error", f"before_run hook failed: {hook.summary}")
            return
        try:
            renderer = PromptRenderer(workflow.prompt_template)
        except AgentError as exc:
            state.fail(exc.category, exc.message)
            return
        _save(workspaces, workspace.path, state.session_record(0, None))
        await _turn_loop(
            state,
            workflow,
            renderer,
            adapter,
            workspaces,
            runner,
            workspace.path,
            rework=rework,
            resuming=resuming,
            cancel=cancel,
            observer=observer,
        )
    finally:
        await workspaces.run_hook("after_run", workspace.path)
        _save(workspaces, workspace.path, state.session_record(state.turns, state.outcome))


async def _turn_loop(
    state: _State,
    workflow: Workflow,
    renderer: PromptRenderer,
    adapter: GitHubAdapter,
    workspaces: WorkspaceManager,
    runner: TurnRunner,
    workspace: Path,
    *,
    rework: bool,
    resuming: bool,
    cancel: asyncio.Event | None,
    observer: TurnObserver | None,
) -> None:
    settings = workflow.config
    max_turns = settings.agent.max_turns
    for turn_number in range(1, max_turns + 1):
        # The workpad is resolved here, by author, and handed to the prompt (#77): the agent
        # follows this id rather than finding the comment by a first line anyone can write.
        # Every turn, not once: the agent creates it in turn 1 and a resumed session's is
        # whatever the last one left. A lookup that fails fails the run, as a failed refresh
        # does, since rendering without it would have the agent open a second workpad.
        try:
            state.workpad = await adapter.find_workpad_comment(state.issue.number)
        except GitHubError as exc:
            state.fail("github_error", f"could not find the workpad: {exc}")
            return
        context = PromptContext(
            issue=state.issue,
            repo=settings.github.repo,
            labels=settings.github.labels,
            attempt=state.attempt,
            turn_number=turn_number,
            max_turns=max_turns,
            rework=rework,
            self_review=settings.agent.self_review,
            workpad=state.workpad,
        )
        resume = turn_number > 1 or resuming
        try:
            prompt = renderer.render_continuation(context) if resume else renderer.render(context)
        except AgentError as exc:
            state.fail(exc.category, exc.message)
            return
        turn = await runner.run_turn(
            prompt=prompt,
            workspace=workspace,
            session_id=state.session_id,
            resume=resume,
            turn_number=turn_number,
            log_dir=run_log_dir(workspace, state.run_id),
            observer=observer,
            cancel=cancel,
        )
        state.record_turn(turn)
        _save(workspaces, workspace, state.session_record(turn_number, None))
        if not turn.ok:
            if turn.error_category != "budget_exceeded":
                state.fail(turn.error_category or "turn_failed", turn.error)
                return
            # `--max-budget-usd` caps one `claude -p` process, so the next turn starts a fresh
            # ledger: the cap is a turn boundary, not a failure. Failing here instead would end
            # the run, and the retry after it never resumes (orchestrator `_schedule`), so the
            # replacement session would re-read the repository from cold and pay the cap again
            # to reach the point this one had already committed and pushed.
            get_logger(__name__).warning(
                "turn_budget_exhausted",
                run_id=state.run_id,
                turn_number=turn_number,
                max_turns=max_turns,
                cost_usd=turn.cost_usd,
                error=turn.error,
            )
        if cancel is not None and cancel.is_set():
            state.fail("cancelled", "cancelled between turns")
            return
        try:
            refreshed = await adapter.fetch_issues_by_ids([state.issue.id])
        except GitHubError as exc:
            state.fail("github_error", f"could not refresh the issue: {exc}")
            return
        if not refreshed:
            state.stop("issue_missing")
            return
        state.issue = refreshed[0]
        state.final_issue = refreshed[0]
        if state.issue.state is not StateLabel.IN_PROGRESS or not state.issue.dispatchable:
            state.stop("issue_moved")
            return
        # A budget_exceeded turn continues past `turn.ok` and reaches this check; its
        # result_text is claude's own cap message, which never starts with the marker.
        blocker = blocker_from(turn.result_text)
        if blocker is not None:
            state.blocker = blocker
            state.stop("blocked")
            return
        if turn_number == max_turns:
            state.stop("max_turns")
            return


def _save(workspaces: WorkspaceManager, workspace: Path, record: SessionRecord) -> None:
    try:
        workspaces.write_session(workspace, record)
    except OSError as exc:
        get_logger(__name__).warning(
            "session_file_write_failed", workspace=str(workspace), error=str(exc)
        )
