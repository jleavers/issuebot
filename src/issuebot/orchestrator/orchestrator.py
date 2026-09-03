"""The orchestrator: one task owning the schedule, workers as child tasks, a queue between."""

import asyncio
import math
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

from issuebot.agent import (
    AgentError,
    ClaudeRunner,
    RunResult,
    TurnEvent,
    TurnRunner,
    WorkspaceManager,
    new_run_id,
    run_session,
)
from issuebot.config import ConfigError, GitHubSettings, Settings, Workflow, load_workflow
from issuebot.events import EventBus
from issuebot.github import (
    ACTIVE_STATES,
    GhCliAdapter,
    GitHubAdapter,
    GitHubError,
    Issue,
    StateLabel,
)
from issuebot.log import get_logger
from issuebot.orchestrator import actions
from issuebot.orchestrator.state import (
    CONTINUATION_DELAY_MS,
    REVIEW_GRACE_TICKS,
    TERMINAL_SWEEP_EVERY_TICKS,
    BlockedContext,
    ClaudeTotals,
    Counters,
    RetryEntry,
    RetryKind,
    RetryRow,
    RunningEntry,
    RunningRow,
    RuntimeSnapshot,
    StopCause,
    backoff_ms,
    observe_transition,
    sort_candidates,
)

RunSessionFn = Callable[..., Awaitable[RunResult]]
CANDIDATE_STATES: tuple[StateLabel, ...] = (
    StateLabel.IN_PROGRESS,
    StateLabel.REWORK,
    StateLabel.TODO,
)
SHUTDOWN_MARGIN_S = 10.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OrchestratorStartupError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def preflight(settings: Settings, *, which: Callable[[str], str | None]) -> list[str]:
    """Problems that block dispatch: the executables and the token the worker needs."""
    problems: list[str] = []
    if which(settings.claude.command) is None:
        problems.append(f"claude.command {settings.claude.command!r} not found on PATH")
    if which("gh") is None:
        problems.append("'gh' not found on PATH")
    if settings.github.token is None:
        problems.append("github.token not set; export GH_TOKEN or set github.token: $VAR")
    return problems


class RunObserver:
    """Feeds one running entry from the runner's turn events; satisfies TurnObserver."""

    def __init__(
        self,
        entry: RunningEntry,
        *,
        clock: Callable[[], float],
        now: Callable[[], datetime],
    ) -> None:
        self._entry = entry
        self._clock = clock
        self._now = now

    def on_turn_event(self, event: TurnEvent) -> None:
        entry = self._entry
        entry.last_activity_mono = self._clock()
        entry.last_activity_at = self._now()
        suffix = event.tool_name or event.message_type
        if event.kind == "turn_activity" and suffix:
            entry.last_event = f"{event.kind}:{suffix}"
        else:
            entry.last_event = event.kind
        if event.kind == "session_started" and event.session_id:
            entry.session_id = event.session_id
        if event.kind in ("turn_completed", "turn_failed", "turn_timeout"):
            entry.turns = event.turn_number


@dataclass(frozen=True, slots=True)
class _WorkerExited:
    issue_id: str


_REFRESH = object()
_STOP = object()


class Orchestrator:
    """Symphony §7 and §8 over GitHub labels: poll, claim, dispatch, retry, reconcile, recover."""

    def __init__(
        self,
        workflow: Workflow,
        *,
        bus: EventBus,
        adapter_factory: Callable[[GitHubSettings], GitHubAdapter] = GhCliAdapter,
        workspaces_factory: Callable[[Settings], WorkspaceManager] = WorkspaceManager,
        runner_factory: Callable[[Settings], TurnRunner] = ClaudeRunner,
        run_session: RunSessionFn = run_session,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
        environ: Mapping[str, str] | None = None,
        on_snapshot: Callable[[RuntimeSnapshot], None] | None = None,
    ) -> None:
        self._workflow = workflow
        self._bus = bus
        self._adapter_factory = adapter_factory
        self._workspaces_factory = workspaces_factory
        self._runner_factory = runner_factory
        self._run_session = run_session
        self._which = which
        self._clock = clock
        self._now = now
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._on_snapshot = on_snapshot
        self._adapter = adapter_factory(workflow.config.github)
        self._workspaces = workspaces_factory(workflow.config)
        self._running: dict[str, RunningEntry] = {}
        self._retries: dict[str, RetryEntry] = {}
        self._totals = ClaudeTotals()
        self._counters = Counters()
        self._tick_count = 0
        self._last_tick_at: datetime | None = None
        self._config_error: str | None = None
        self._reported_reload_error: str | None = None
        self._reported_preflight: str | None = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._refresh_pending = False
        self._stopping = False
        self._log = get_logger(__name__)

    # --- views ------------------------------------------------------------------------

    @property
    def workflow(self) -> Workflow:
        return self._workflow

    @property
    def running(self) -> Mapping[str, RunningEntry]:
        return MappingProxyType(self._running)

    @property
    def retries(self) -> Mapping[str, RetryEntry]:
        return MappingProxyType(self._retries)

    @property
    def stopping(self) -> bool:
        return self._stopping

    def snapshot(self) -> RuntimeSnapshot:
        now_mono = self._clock()
        active = sum(now_mono - entry.started_mono for entry in self._running.values())
        settings = self._workflow.config
        return RuntimeSnapshot(
            at=self._now(),
            workflow_path=str(self._workflow.path),
            workflow_mtime_ns=self._workflow.source_mtime_ns,
            config_valid=self._config_error is None,
            config_error=self._config_error,
            poll_interval_ms=settings.polling.interval_ms,
            max_concurrent_agents=settings.agent.max_concurrent_agents,
            tick_count=self._tick_count,
            last_tick_at=self._last_tick_at,
            running=tuple(RunningRow.from_entry(entry) for entry in self._running.values()),
            retrying=tuple(
                RetryRow.from_entry(entry)
                for entry in sorted(self._retries.values(), key=lambda entry: entry.due_mono)
            ),
            totals=replace(
                self._totals, seconds_running=round(self._totals.seconds_running + active, 3)
            ),
            counters=self._counters,
        )

    # --- startup ----------------------------------------------------------------------

    async def startup(self) -> None:
        """Symphony §6.3 startup validation plus the two probes; raises on any problem."""
        settings = self._workflow.config
        problems = preflight(settings, which=self._which)
        if not problems:
            try:
                await self._adapter.auth_status()
            except GitHubError as exc:
                problems.append(f"gh auth: {exc.message}; run gh auth login or set GH_TOKEN")
            try:
                missing = await self._adapter.missing_labels()
            except GitHubError as exc:
                problems.append(f"github.labels: {exc.message}")
            else:
                if missing:
                    names = ", ".join(missing)
                    problems.append(f"labels missing: {names}; run issuebot labels ensure")
        if problems:
            self._log.error("orchestrator_startup_failed", problems=problems)
            raise OrchestratorStartupError(problems)
        self._log.info(
            "orchestrator_started",
            repo=settings.github.repo,
            workflow=str(self._workflow.path),
            poll_interval_ms=settings.polling.interval_ms,
            max_concurrent_agents=settings.agent.max_concurrent_agents,
            max_turns=settings.agent.max_turns,
            max_attempts=settings.agent.max_attempts,
            stall_timeout_ms=settings.claude.stall_timeout_ms,
            workspace_root=str(settings.workspace.root),
        )

    # --- tick -------------------------------------------------------------------------

    async def tick(self) -> None:
        """Symphony §8.1: reconcile, reload, preflight, fetch, dispatch, snapshot."""
        await self.reconcile()
        self._reload_workflow()
        dispatched = 0
        problems = preflight(self._workflow.config, which=self._which)
        if problems:
            message = "; ".join(problems)
            if message != self._reported_preflight:
                self._log.error("dispatch_preflight_failed", problems=problems)
                self._reported_preflight = message
        else:
            self._reported_preflight = None
            dispatched = await self._dispatch_candidates()
        self._tick_count += 1
        self._last_tick_at = self._now()
        self._log.debug(
            "tick_finished",
            tick=self._tick_count,
            running=len(self._running),
            retrying=len(self._retries),
            dispatched=dispatched,
            slots=self._slots(),
        )
        self._publish_snapshot()

    def _slots(self) -> int:
        return max(self._workflow.config.agent.max_concurrent_agents - len(self._running), 0)

    def _publish_snapshot(self) -> None:
        if self._on_snapshot is None:
            return
        try:
            self._on_snapshot(self.snapshot())
        except Exception:
            self._log.exception("snapshot_consumer_failed")

    def _reload_workflow(self) -> None:
        path = self._workflow.path
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError as exc:
            self._report_reload_failure(f"workflow file unreadable: {exc}")
            return
        if mtime_ns == self._workflow.source_mtime_ns:
            return
        try:
            workflow = load_workflow(path, environ=self._environ)
        except ConfigError as exc:
            self._report_reload_failure(str(exc))
            return
        changed = _changed_sections(self._workflow, workflow)
        self._workflow = workflow
        self._config_error = None
        self._reported_reload_error = None
        self._adapter = self._adapter_factory(workflow.config.github)
        self._workspaces = self._workspaces_factory(workflow.config)
        self._log.info("workflow_reloaded", path=str(path), changed=changed)

    def _report_reload_failure(self, message: str) -> None:
        self._config_error = message
        if message == self._reported_reload_error:
            return
        self._reported_reload_error = message
        self._log.error("workflow_reload_failed", path=str(self._workflow.path), error=message)

    async def _dispatch_candidates(self) -> int:
        try:
            issues = await self._adapter.fetch_issues_by_states(CANDIDATE_STATES)
        except GitHubError as exc:
            self._log.warning("candidates_fetch_failed", error=str(exc))
            return 0
        dispatched = 0
        for issue in sort_candidates(issues):
            if self._slots() <= 0:
                break
            if not issue.dispatchable or issue.state not in ACTIVE_STATES:
                continue
            if issue.id in self._running or issue.id in self._retries:
                continue
            attempt, resume_session_id = 1, None
            if issue.state is StateLabel.IN_PROGRESS:
                plan = self._resume_plan(issue)
                if plan is None:
                    continue
                attempt, resume_session_id = plan
            if await self._dispatch(issue, attempt=attempt, resume_session_id=resume_session_id):
                dispatched += 1
        return dispatched

    def _resume_plan(self, issue: Issue) -> tuple[int, str | None] | None:
        """How to dispatch an orphaned in_progress issue: resume, fresh, or not at all."""
        try:
            path = self._workspaces.path_for(issue.identifier)
        except AgentError as exc:
            self._log.warning(
                "dispatch_skipped",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                error=exc.message,
            )
            return None
        record = self._workspaces.read_session(path)
        if (
            record is not None
            and record.issue_number == issue.number
            and record.last_outcome in (None, "cancelled")
        ):
            return record.attempt, record.session_id
        return 1, None

    async def _dispatch(self, issue: Issue, *, attempt: int, resume_session_id: str | None) -> bool:
        rework = issue.state is StateLabel.REWORK
        if issue.state is not StateLabel.IN_PROGRESS:
            claimed = await actions.claim(self._adapter, self._bus, issue)
            if claimed is None:
                return False
            issue = claimed
        workflow = self._workflow
        entry = RunningEntry(
            issue=issue,
            attempt=attempt,
            rework=rework,
            resumed=resume_session_id is not None,
            run_id=new_run_id(self._now()),
            started_mono=self._clock(),
            started_at=self._now(),
            cancel=asyncio.Event(),
        )
        entry.task = asyncio.create_task(
            self._worker(entry, workflow, resume_session_id),
            name=f"issuebot-worker-{issue.number}",
        )
        entry.task.add_done_callback(
            lambda _task, issue_id=issue.id: self._queue.put_nowait(_WorkerExited(issue_id))
        )
        self._running[issue.id] = entry
        self._retries.pop(issue.id, None)
        self._counters = self._counters.bump(runs_started=1)
        self._log.info(
            "dispatched",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            attempt=attempt,
            rework=rework,
            resumed=entry.resumed,
            run_id=entry.run_id,
            slots_left=self._slots(),
        )
        return True

    async def _worker(
        self, entry: RunningEntry, workflow: Workflow, resume_session_id: str | None
    ) -> RunResult:
        return await self._run_session(
            entry.issue,
            workflow,
            self._adapter,
            self._bus,
            workspaces=self._workspaces,
            runner=self._runner_factory(workflow.config),
            attempt=entry.attempt,
            rework=entry.rework,
            resume_session_id=resume_session_id,
            cancel=entry.cancel,
            observer=RunObserver(entry, clock=self._clock, now=self._now),
            run_id=entry.run_id,
        )

    # --- reconcile ----------------------------------------------------------------------

    async def reconcile(self) -> None:
        """Symphony §8.5: stalls, then the label refresh; plus the terminal sweep on schedule."""
        self._check_stalls()
        if self._running:
            await self._refresh_running()
        if self._tick_count % TERMINAL_SWEEP_EVERY_TICKS == 0:
            await self.terminal_sweep()

    def _check_stalls(self) -> None:
        timeout_ms = self._workflow.config.claude.stall_timeout_ms
        if timeout_ms <= 0:
            return
        now = self._clock()
        for entry in self._running.values():
            if entry.stop_cause is not None:
                continue
            since = (
                entry.last_activity_mono
                if entry.last_activity_mono is not None
                else entry.started_mono
            )
            elapsed = now - since
            if elapsed * 1000 > timeout_ms:
                self._log.warning(
                    "reconcile_stalled",
                    issue_number=entry.issue.number,
                    issue_identifier=entry.identifier,
                    run_id=entry.run_id,
                    elapsed_s=round(elapsed),
                    stall_timeout_ms=timeout_ms,
                )
                entry.stop("stalled", f"no activity for {elapsed:.0f} s")

    async def _refresh_running(self) -> None:
        ids = list(self._running)
        try:
            refreshed = await self._adapter.fetch_issues_by_ids(ids)
        except GitHubError as exc:
            self._log.warning("reconcile_refresh_failed", error=str(exc))
            return
        by_id = {issue.id: issue for issue in refreshed}
        for issue_id in ids:
            entry = self._running.get(issue_id)
            if entry is None:
                continue
            current = by_id.get(issue_id)
            if current is None:
                self._stop_entry(entry, "missing", "issue no longer found")
                continue
            if current.github_state == "closed":
                entry.terminal_issue = current
                self._stop_entry(entry, "closed", "issue closed")
                continue
            for event in observe_transition(entry.issue, current):
                self._bus.publish(event)
            entry.issue = current
            if current.state is StateLabel.IN_PROGRESS and current.dispatchable:
                continue
            if current.state is StateLabel.REVIEW:
                if entry.review_seen_tick is None:
                    entry.review_seen_tick = self._tick_count
                    self._log.info(
                        "reconcile_review_grace",
                        issue_number=current.number,
                        issue_identifier=current.identifier,
                        run_id=entry.run_id,
                    )
                elif self._tick_count - entry.review_seen_tick >= REVIEW_GRACE_TICKS:
                    self._stop_entry(entry, "moved", "review")
                continue
            detail = current.state.value if current.state is not None else "unlabelled"
            self._stop_entry(entry, "moved", detail)

    def _stop_entry(self, entry: RunningEntry, cause: StopCause, detail: str) -> None:
        if entry.stop_cause is None:
            self._log.info(
                "reconcile_stop",
                issue_number=entry.issue.number,
                issue_identifier=entry.identifier,
                run_id=entry.run_id,
                cause=cause,
                detail=detail,
            )
        entry.stop(cause, detail)

    async def terminal_sweep(self) -> None:
        """Symphony §8.6, repeated: closed issues still carrying a state label."""
        try:
            issues = await self._adapter.fetch_terminal_issues()
        except GitHubError as exc:
            self._log.warning("terminal_sweep_failed", error=str(exc))
            return
        for issue in issues:
            if issue.id in self._running:
                continue
            self._retries.pop(issue.id, None)
            await self._finish(issue)

    async def _finish(self, issue: Issue) -> None:
        outcome = await actions.finish_terminal(self._adapter, self._bus, self._workspaces, issue)
        if outcome == "complete":
            self._counters = self._counters.bump(issues_completed=1)
        elif outcome == "cancelled":
            self._counters = self._counters.bump(issues_cancelled=1)

    # --- worker exits and retries ------------------------------------------------

    async def handle_worker_exit(self, issue_id: str) -> None:
        """Symphony §16.6 with the blocked escape: totals, then retry, escape or release."""
        entry = self._running.pop(issue_id, None)
        if entry is None or entry.task is None:
            return
        task = entry.task
        self._counters = self._counters.bump(runs_ended=1)
        result: RunResult | None = None
        error: str | None = None
        if task.cancelled():
            error = "worker task cancelled"
            self._add_elapsed(entry)
        elif task.exception() is not None:
            exc = task.exception()
            self._log.error(
                "worker_crashed",
                issue_number=entry.issue.number,
                issue_identifier=entry.identifier,
                run_id=entry.run_id,
                error=str(exc),
                exc_info=exc,
            )
            error = f"worker crashed: {exc}"
            self._add_elapsed(entry)
        else:
            result = task.result()
            self._totals = self._totals.add(result)
        self._log.info(
            "worker_exited",
            issue_number=entry.issue.number,
            issue_identifier=entry.identifier,
            run_id=entry.run_id,
            attempt=entry.attempt,
            outcome=result.outcome if result is not None else None,
            stop_reason=result.stop_reason if result is not None else None,
            cause=entry.stop_cause,
            turns=result.turns if result is not None else entry.turns,
            cost_usd=result.cost_usd if result is not None else 0.0,
            error=error or (result.error if result is not None else None),
        )
        if entry.terminal_issue is not None:
            await self._finish(entry.terminal_issue)
            return
        if task.cancelled() or entry.stop_cause in ("moved", "missing", "shutdown"):
            self._log.info(
                "issue_released",
                issue_number=entry.issue.number,
                issue_identifier=entry.identifier,
                reason=entry.stop_cause or "task cancelled",
            )
            return
        if entry.stop_cause == "stalled":
            await self._after_failure(entry, f"stalled: {entry.stop_detail}", result)
            return
        if result is None:
            await self._after_failure(entry, error or "worker crashed", None)
            return
        if result.outcome == "succeeded":
            if result.stop_reason == "max_turns" and result.final_state is StateLabel.IN_PROGRESS:
                review = self._workflow.config.github.labels.review
                reason = (
                    f"Turn budget exhausted: {result.turns} turns in attempt {entry.attempt} "
                    f"without reaching `{review}`."
                )
                await self._escape(entry, reason, result)
                return
            final = result.final_issue
            if final is not None and final.github_state == "open":
                for event in observe_transition(entry.issue, final):
                    self._bus.publish(event)
            self._schedule(
                entry.issue,
                attempt=1,
                kind="continuation",
                delay_ms=CONTINUATION_DELAY_MS,
                error=None,
            )
            return
        await self._after_failure(entry, f"{result.error_category}: {result.error}", result)

    def _add_elapsed(self, entry: RunningEntry) -> None:
        elapsed = self._clock() - entry.started_mono
        self._totals = replace(
            self._totals, seconds_running=round(self._totals.seconds_running + elapsed, 3)
        )

    async def _after_failure(
        self, entry: RunningEntry, error: str, result: RunResult | None
    ) -> None:
        agent = self._workflow.config.agent
        if entry.attempt >= agent.max_attempts:
            reason = (
                f"{agent.max_attempts} consecutive worker sessions failed; last error: {error}."
            )
            await self._escape(entry, reason, result)
            return
        self._schedule(
            entry.issue,
            attempt=entry.attempt + 1,
            kind="failure",
            delay_ms=backoff_ms(entry.attempt + 1, agent.max_retry_backoff_ms),
            error=error,
        )

    async def _escape(self, entry: RunningEntry, reason: str, result: RunResult | None) -> None:
        context = BlockedContext(
            reason=reason,
            run_id=entry.run_id,
            attempt=entry.attempt,
            turns=result.turns if result is not None else entry.turns,
            log_dir=str(result.log_dir) if result is not None and result.log_dir else None,
        )
        outcome = await actions.blocked_escape(
            self._adapter, self._bus, entry.issue_id, context, now=self._now()
        )
        if outcome == "applied":
            self._counters = self._counters.bump(blocked=1)
        elif outcome == "failed":
            self._schedule(
                entry.issue,
                attempt=1,
                kind="escape",
                delay_ms=backoff_ms(1, self._workflow.config.agent.max_retry_backoff_ms),
                error="blocked escape failed",
                escape=context,
            )

    def _schedule(
        self,
        issue: Issue,
        *,
        attempt: int,
        kind: RetryKind,
        delay_ms: int,
        error: str | None,
        escape: BlockedContext | None = None,
    ) -> None:
        entry = RetryEntry(
            issue_id=issue.id,
            identifier=issue.identifier,
            issue_number=issue.number,
            issue_url=issue.url,
            attempt=attempt,
            kind=kind,
            due_mono=self._clock() + delay_ms / 1000,
            due_at=self._now() + timedelta(milliseconds=delay_ms),
            error=error,
            escape=escape,
        )
        self._retries[issue.id] = entry
        self._log.info(
            "retry_scheduled",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            kind=kind,
            attempt=attempt,
            due_in_ms=delay_ms,
            error=error,
        )

    def _requeue(self, entry: RetryEntry, *, delay_ms: int, error: str, kind: RetryKind) -> None:
        self._retries[entry.issue_id] = replace(
            entry,
            kind=kind,
            due_mono=self._clock() + delay_ms / 1000,
            due_at=self._now() + timedelta(milliseconds=delay_ms),
            error=error,
        )
        self._log.info(
            "retry_scheduled",
            issue_number=entry.issue_number,
            issue_identifier=entry.identifier,
            kind=kind,
            attempt=entry.attempt,
            due_in_ms=delay_ms,
            error=error,
        )

    def _release(self, entry: RetryEntry, reason: str) -> None:
        self._log.info(
            "retry_released",
            issue_number=entry.issue_number,
            issue_identifier=entry.identifier,
            kind=entry.kind,
            reason=reason,
        )

    def _earliest_due(self) -> float:
        return min((entry.due_mono for entry in self._retries.values()), default=math.inf)

    async def fire_due_retries(self) -> None:
        """Symphony §16.6's retry timer, for every entry whose time has come."""
        now = self._clock()
        due = sorted(
            (entry for entry in self._retries.values() if entry.due_mono <= now),
            key=lambda entry: entry.due_mono,
        )
        for entry in due:
            if self._stopping:
                return
            if self._retries.get(entry.issue_id) is not entry:
                continue
            del self._retries[entry.issue_id]
            await self._fire(entry)

    async def _fire(self, entry: RetryEntry) -> None:
        settings = self._workflow.config
        self._log.info(
            "retry_fired",
            issue_number=entry.issue_number,
            issue_identifier=entry.identifier,
            kind=entry.kind,
            attempt=entry.attempt,
        )
        if entry.kind == "escape" and entry.escape is not None:
            outcome = await actions.blocked_escape(
                self._adapter, self._bus, entry.issue_id, entry.escape, now=self._now()
            )
            if outcome == "applied":
                self._counters = self._counters.bump(blocked=1)
            elif outcome == "failed":
                self._requeue(
                    replace(entry, attempt=entry.attempt + 1),
                    kind="escape",
                    delay_ms=backoff_ms(entry.attempt + 1, settings.agent.max_retry_backoff_ms),
                    error="blocked escape failed",
                )
            return
        try:
            issues = await self._adapter.fetch_issues_by_ids([entry.issue_id])
        except GitHubError as exc:
            self._requeue(
                entry,
                kind=entry.kind,
                delay_ms=settings.polling.interval_ms,
                error=f"retry refresh failed: {exc.message}",
            )
            return
        if not issues:
            self._release(entry, "missing")
            return
        issue = issues[0]
        if issue.github_state == "closed":
            await self._finish(issue)
            return
        if not issue.dispatchable or issue.state not in ACTIVE_STATES:
            self._release(entry, "not_active")
            return
        if self._slots() <= 0:
            self._requeue(
                entry,
                kind="slots",
                delay_ms=settings.polling.interval_ms,
                error="no available orchestrator slots",
            )
            return
        attempt = entry.attempt if issue.state is StateLabel.IN_PROGRESS else 1
        await self._dispatch(issue, attempt=attempt, resume_session_id=None)


def _changed_sections(old: Workflow, new: Workflow) -> list[str]:
    changed = [
        name
        for name in type(old.config).model_fields
        if getattr(old.config, name) != getattr(new.config, name)
    ]
    if old.prompt_template != new.prompt_template:
        changed.append("prompt")
    return changed
