"""One triage session: a throwaway clone, a read-only agent, a verdict out (spec sections 4, 5)."""

import asyncio
import dataclasses
import time
import uuid
from collections.abc import Awaitable, Callable

from issuebot.agent.errors import AgentError
from issuebot.agent.instructions import read_repository_instructions
from issuebot.agent.prompt import (
    TRIAGE_CONTINUATION_TEMPLATE,
    PromptRenderer,
    TriagePromptContext,
)
from issuebot.agent.runner import TurnObserver, TurnRunner
from issuebot.agent.session import RunResult, RunState, error_text, new_run_id
from issuebot.agent.workspace import WorkspaceManager, run_log_dir
from issuebot.config import Workflow
from issuebot.events import EventBus, RunEnded, RunStarted
from issuebot.github import GitHubAdapter, GitHubError, Issue
from issuebot.log import bind_issue_context, bind_session_context, clear_context, get_logger

RunTriageFn = Callable[..., Awaitable[RunResult]]


async def run_triage(
    issue: Issue,
    workflow: Workflow,
    adapter: GitHubAdapter,
    bus: EventBus,
    *,
    workspaces: WorkspaceManager,
    runner: TurnRunner,
    attempt: int = 1,
    cancel: asyncio.Event | None = None,
    observer: TurnObserver | None = None,
    run_id: str | None = None,
) -> RunResult:
    """Run one triage session for ``issue`` and report what it did.

    It moves no label and writes nothing to the tracker: a run that ends on a verdict hands
    the raw ``structured_output`` back and the worker files it. The shape is ``run_session``'s,
    but the clone is a fresh read-only one, there is no hook, no refresh between turns and no
    ``BLOCKED:`` escape, and the run stops as soon as a turn returns a verdict.
    """
    state = RunState(
        run_id=run_id or new_run_id(),
        session_id=str(uuid.uuid4()),
        attempt=attempt,
        issue=issue,
        started=time.monotonic(),
    )
    log = get_logger(__name__)
    bind_issue_context(issue_number=issue.number, issue_identifier=issue.identifier)
    bind_session_context(session_id=state.session_id)
    try:
        try:
            state.workspace_path = workspaces.triage_path_for(issue.identifier)
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
                run_kind="triage",
            )
        )
        log.info("run_started", run_id=state.run_id, attempt=attempt, run_kind="triage")
        crash: Exception | None = None
        if state.stop_reason is None:
            try:
                await _execute(state, workflow, adapter, workspaces, runner, cancel, observer)
            except Exception as exc:
                # Whatever the run did not classify (an OSError reading the instructions, a
                # runner that raised): the run still ends in the store as failed, then the
                # exception goes on to the orchestrator's own crash handling. Cancellation is
                # a BaseException and passes through, as it does in `run_session`.
                state.fail("turn_failed", f"unexpected {type(exc).__name__}: {exc}")
                crash = exc
        # A triage run moves no label, so it has no state to report and no refreshed issue.
        result = dataclasses.replace(state.result(), final_state=None, final_issue=None)
        bus.publish(
            RunEnded(
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                run_id=result.run_id,
                outcome=result.outcome,
                error=error_text(result),
                turns=result.turns,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=result.cost_usd,
                duration_s=result.duration_s,
                log_dir=str(result.log_dir) if result.log_dir is not None else None,
                run_kind="triage",
            )
        )
        # The verdict itself stays out of the log: it is the agent's raw, unscrubbed output.
        log.info(
            "run_finished",
            run_id=result.run_id,
            run_kind="triage",
            outcome=result.outcome,
            stop_reason=result.stop_reason,
            turns=result.turns,
            cost_usd=result.cost_usd,
            error=error_text(result),
        )
        if crash is not None:
            raise crash
        return result
    finally:
        clear_context()


async def _execute(
    state: RunState,
    workflow: Workflow,
    adapter: GitHubAdapter,
    workspaces: WorkspaceManager,
    runner: TurnRunner,
    cancel: asyncio.Event | None,
    observer: TurnObserver | None,
) -> None:
    settings = workflow.config
    try:
        workspace = await workspaces.create_triage(state.issue)
    except AgentError as exc:
        state.fail(exc.category, exc.message)
        return
    state.workspace_path = workspace.path
    state.log_dir = run_log_dir(workspace.path, state.run_id)
    try:
        try:
            renderer = PromptRenderer(
                workflow.triage_template, continuation=TRIAGE_CONTINUATION_TEMPLATE
            )
        except AgentError as exc:
            state.fail(exc.category, exc.message)
            return
        instructions = await asyncio.to_thread(
            read_repository_instructions, workspace.path, boundary=workspaces.boundary
        )
        # The account's own comments are filtered out of the prompt unless it administers the
        # repository (``comment_variables``), so both are read exactly as ``run_session`` does.
        try:
            comments = tuple(await adapter.fetch_comments(state.issue.number))
        except GitHubError as exc:
            state.fail("github_error", f"could not read the comments: {exc}")
            return
        try:
            login = await adapter.own_login()
        except GitHubError as exc:
            state.fail("github_error", f"could not read the account's login: {exc}")
            return
        try:
            admin = (await adapter.repo_info()).admin
        except GitHubError as exc:
            state.fail("github_error", f"could not read the account's repository role: {exc}")
            return
        max_turns = settings.triage.max_turns
        deadline = state.run_deadline(settings.agent)
        for turn_number in range(1, max_turns + 1):
            if state.past_deadline(settings.agent, turn_number):
                return
            context = TriagePromptContext.for_issue(
                state.issue,
                settings,
                login=login,
                admin=admin,
                comments=comments,
                repo_instructions=instructions,
                turn_number=turn_number,
            )
            resume = turn_number > 1
            try:
                prompt = (
                    renderer.render_continuation(context) if resume else renderer.render(context)
                )
            except AgentError as exc:
                state.fail(exc.category, exc.message)
                return
            # Before every `claude -p`, as the work loop does (#101): `--setting-sources user`
            # still loads the session account's home, and a prior session may have planted in it.
            await workspaces.sweep_agent_home()
            turn = await runner.run_turn(
                prompt=prompt,
                workspace=workspace.path,
                session_id=state.session_id,
                resume=resume,
                turn_number=turn_number,
                log_dir=run_log_dir(workspace.path, state.run_id),
                observer=observer,
                cancel=cancel,
                deadline=deadline,
            )
            state.record_turn(turn)
            if state.fails_on(
                turn, turn_number=turn_number, max_turns=max_turns, log=get_logger(__name__)
            ):
                return
            if turn.structured_output is not None:
                state.structured_output = turn.structured_output
                state.stop("verdict")
                return
            if state.cancelled(cancel):
                return
        state.stop("max_turns")
    finally:
        # The worker removes the clone once it has filed the verdict; until then it is closed
        # to every account, as a work workspace is after its run (#121).
        workspaces.seal(workspace.path)
