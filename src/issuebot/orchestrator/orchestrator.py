"""The orchestrator: one task owning the schedule, workers as child tasks, a queue between."""

import asyncio
import math
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

from issuebot.agent import (
    AgentError,
    ClaudeAuth,
    ClaudeRunner,
    RunResult,
    TurnEvent,
    TurnRunner,
    WorkspaceManager,
    claude_auth_status,
    describe_claude_auth,
    new_run_id,
    run_session,
    settings_for_labels,
)
from issuebot.agent.runner import TERMINATE_GRACE_S, Credential, RateLimits
from issuebot.agent.scrub import DEFAULT_SCRUBBER, Scrubber
from issuebot.config import (
    ConfigError,
    GitHubSettings,
    Settings,
    Workflow,
    load_workflow,
    overlay_path_for,
)
from issuebot.events import EventBus
from issuebot.github import (
    ACTIVE_STATES,
    GhCliAdapter,
    GitHubAdapter,
    GitHubError,
    Issue,
    StateLabel,
    fetch_status_summary,
    parse_status_summary,
)
from issuebot.log import get_logger
from issuebot.orchestrator import actions
from issuebot.orchestrator.state import (
    CONTINUATION_DELAY_MS,
    TERMINAL_SWEEP_EVERY_TICKS,
    BlockedContext,
    ClaudeTotals,
    Counters,
    DispatchHold,
    DispatchHoldKind,
    RetryEntry,
    RetryKind,
    RetryRow,
    RunningEntry,
    RunningRow,
    RuntimeSnapshot,
    StopCause,
    backoff_ms,
    conflict_candidate,
    observe_transition,
    sort_candidates,
)

RunSessionFn = Callable[..., Awaitable[RunResult]]
CANDIDATE_STATES: tuple[StateLabel, ...] = (
    StateLabel.IN_PROGRESS,
    StateLabel.REWORK,
    StateLabel.TODO,
)
# Fetched instead of CANDIDATE_STATES when an on_issues observer is attached or the conflict
# bounce is on (`fetch_states`): review is polled for the history store and the bounce only; the
# dispatch loop never runs it (Phase 6 spec §8.1).
OBSERVED_STATES: tuple[StateLabel, ...] = (*CANDIDATE_STATES, StateLabel.REVIEW)
SHUTDOWN_MARGIN_S = 10.0
# How many ticks an authentication hold (#20) waits for a probe that cannot answer before it
# gives up and lets dispatch resume. Ten polls is five minutes at the default interval.
MAX_UNREADABLE_AUTH_PROBES = 10
# How many polls in a row must fail to read the board before dispatch is held (#88). `gh`
# already retries a transport error of its own, so one failure is a blip and not an outage.
# Three in a row is not: the third failure is a minute after the first at the default interval,
# and sooner when a refresh has brought ticks together, which errs towards holding.
MAX_FETCH_FAILURES = 3
# How long a tick will wait for the status page before giving up on the annotation. Longer
# than the fetch's own socket timeout, because that one does not bound the name lookup, and
# short enough that a wedged resolver cannot hold up a poll interval's worth of work.
GITHUB_STATUS_DEADLINE_S = 10.0
# The GitHub hold is one outage however differently it words itself from poll to poll, so
# `since` is keyed on this rather than on the reason (the same trick the auth hold plays with
# the probe's verdict).
GITHUB_HOLD_KEY = "github"
# How much of `gh`'s complaint the hold's reason carries. Neither `gh`'s stderr nor GitHub's
# GraphQL messages are bounded, and the reason is stored and drawn on every tick it lasts.
MAX_HOLD_ERROR_CHARS = 300

# `claude auth status --json` as the startup probe runs it: the resolved command and the parent
# environment, stdout or None. A seam like `which`, so tests never spawn a process.
ClaudeAuthProbe = Callable[[str, Mapping[str, str]], str | None]
# githubstatus.com's summary body, or None. A seam for the same reason: no test reaches the
# network, and nothing issuebot decides depends on the answer.
GitHubStatusProbe = Callable[[], str | None]


def fetch_states(*, observed: bool, conflicts: bool) -> tuple[StateLabel, ...]:
    """Which states a poll fetches: review rides along for the history store, or for the
    conflict bounce, and the dispatch loop never runs it either way."""
    return OBSERVED_STATES if observed or conflicts else CANDIDATE_STATES


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _github_hold_reason(error: str, status_note: str | None) -> str:
    """The GitHub hold's reason: this worker's own evidence, the status page as annotation.

    ``error`` is capped for the same reason the annotation is: it is ``gh``'s stderr or every
    GraphQL message GitHub sent, neither of them bounded, and this string is written to the
    snapshot on every tick the hold lasts and drawn on the dashboard's worker line.
    """
    reason = f"GitHub is not answering this worker: {_clipped(error, MAX_HOLD_ERROR_CHARS)}"
    # The note carries its own stamp, so it joins with a space: "githubstatus.com at 12:01Z: ...".
    return f"{reason} \u2014 githubstatus.com {status_note}" if status_note else reason


def _clipped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


class OrchestratorStartupError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def fetch_preflight(settings: Settings, *, which: Callable[[str], str | None]) -> list[str]:
    """The preflight problems that stop the GitHub fetch, as opposed to only the agent."""
    problems: list[str] = []
    if which("gh") is None:
        problems.append("'gh' not found on PATH")
    if settings.github.token is None:
        problems.append("github.token not set; export GH_TOKEN or set github.token: $VAR")
    return problems


def preflight(settings: Settings, *, which: Callable[[str], str | None]) -> list[str]:
    """Problems that block dispatch: the executables and the token the worker needs."""
    problems: list[str] = []
    if which(settings.claude.command) is None:
        problems.append(f"claude.command {settings.claude.command!r} not found on PATH")
    problems.extend(fetch_preflight(settings, which=which))
    return problems


class RunObserver:
    """Feeds one running entry from the runner's turn events; satisfies TurnObserver."""

    def __init__(
        self,
        entry: RunningEntry,
        *,
        clock: Callable[[], float],
        now: Callable[[], datetime],
        on_rate_limits: Callable[[RateLimits], None] | None = None,
    ) -> None:
        self._entry = entry
        self._clock = clock
        self._now = now
        self._on_rate_limits = on_rate_limits

    def on_turn_event(self, event: TurnEvent) -> None:
        entry = self._entry
        entry.last_activity_mono = self._clock()
        entry.last_activity_at = self._now()
        # A usage reading is about the account, so it goes past the entry to the orchestrator
        # rather than into it: it has to outlive the run that happened to see it.
        if event.rate_limits is not None and self._on_rate_limits is not None:
            self._on_rate_limits(event.rate_limits)
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
class _Hold:
    """One tick's answer to "why will this worker not claim?", before it reaches the snapshot."""

    kind: DispatchHoldKind
    reason: str
    key: str | None = None


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
        claude_auth: ClaudeAuthProbe = claude_auth_status,
        github_status: GitHubStatusProbe = fetch_status_summary,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
        environ: Mapping[str, str] | None = None,
        initial_rate_limits: RateLimits | None = None,
        on_snapshot: Callable[[RuntimeSnapshot], None] | None = None,
        on_issues: Callable[[Sequence[Issue]], None] | None = None,
        scrubber: Scrubber = DEFAULT_SCRUBBER,
    ) -> None:
        self._workflow = workflow
        # What the blocked escape writes on the public issue goes through this (#91): the
        # reason is scrubbed at its source already, but the run's log directory names the
        # operator's home, which only the deployment's scrubber (the `cli` passes it) can
        # read as `~`.
        self._scrubber = scrubber
        self._bus = bus
        self._adapter_factory = adapter_factory
        self._workspaces_factory = workspaces_factory
        self._runner_factory = runner_factory
        self._run_session = run_session
        self._which = which
        self._claude_auth = claude_auth
        self._github_status = github_status
        self._clock = clock
        self._now = now
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._on_snapshot = on_snapshot
        self._on_issues = on_issues
        self._adapter = adapter_factory(workflow.config.github)
        self._workspaces = workspaces_factory(workflow.config)
        self._running: dict[str, RunningEntry] = {}
        self._retries: dict[str, RetryEntry] = {}
        self._totals = ClaudeTotals()
        self._counters = Counters()
        self._tick_count = 0
        self._last_tick_at: datetime | None = None
        self._config_error: str | None = None
        self._dispatch_hold: DispatchHold | None = None
        self._hold_identity: tuple[str, str] | None = None
        self._reported_reload_error: str | None = None
        self._reported_preflight: str | None = None
        self._auth_block: str | None = None
        self._reported_auth_block: str | None = None
        self._credential: Credential = "unknown"
        # Seeded from the last stored snapshot by the caller that has a database, so a restart
        # keeps the last reading instead of blanking the limits tile until the next dispatch.
        self._rate_limits: RateLimits | None = initial_rate_limits
        self._unreadable_auth_probes = 0
        # The GitHub hold of #88: consecutive failed reads of the board, the reason they hold
        # dispatch (None while they do not), and the status page's annotation on it, read once
        # when the hold engages rather than on every poll.
        self._fetch_failures = 0
        self._github_block: str | None = None
        self._github_note: str | None = None
        self._reported_github_block: str | None = None
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
            workflow_overlay_path=_overlay_name(self._workflow),
            config_valid=self._config_error is None,
            config_error=self._config_error,
            dispatch_hold=self._dispatch_hold,
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
            credential=self._credential,
            rate_limits=self._rate_limits,
        )

    # --- startup ----------------------------------------------------------------------

    async def startup(self) -> None:
        """Symphony §6.3 startup validation plus the three probes; raises on any problem.

        The Claude probe fails startup only on a definite "not logged in". A probe that gives
        no usable answer (a timeout, no output, an older ``claude`` without ``auth status``)
        is logged as a warning and the worker starts, so a slow ``claude`` cannot keep a
        worker down; a real credential problem then surfaces per run as it did before.
        """
        settings = self._workflow.config
        problems = preflight(settings, which=self._which)
        if problems:
            self._startup_failed(problems)
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
        auth = await self._probe_claude_auth(settings.claude.command)
        self._credential = auth.credential
        if auth.verdict == "logged_out":
            problems.append(f"claude auth: {auth.detail}")
        elif auth.verdict != "ok":
            self._log.warning("orchestrator_startup_warning", claude_auth=auth.detail)
        if problems:
            self._startup_failed(problems)
        self._log.info(
            "orchestrator_started",
            claude_auth=auth.detail,
            credential=auth.credential,
            repo=settings.github.repo,
            workflow=str(self._workflow.path),
            poll_interval_ms=settings.polling.interval_ms,
            max_concurrent_agents=settings.agent.max_concurrent_agents,
            max_turns=settings.agent.max_turns,
            max_attempts=settings.agent.max_attempts,
            turn_timeout_ms=settings.claude.turn_timeout_ms,
            stall_timeout_ms=settings.claude.stall_timeout_ms,
            workspace_root=str(settings.workspace.root),
        )

    def _startup_failed(self, problems: list[str]) -> NoReturn:
        self._log.error("orchestrator_startup_failed", problems=problems)
        raise OrchestratorStartupError(problems)

    def _record_rate_limits(self, limits: RateLimits) -> None:
        """Keep the newest reading. Sessions run concurrently, so they can arrive out of order."""
        current = self._rate_limits
        if current is None or limits.observed_at >= current.observed_at:
            self._rate_limits = limits

    async def _probe_claude_auth(self, command: str) -> ClaudeAuth:
        """``claude auth status`` under the agent's environment, off the event loop."""
        found = self._which(command)
        assert found is not None, "preflight resolves claude.command before the probe runs"
        output = await asyncio.to_thread(self._claude_auth, found, self._environ)
        return describe_claude_auth(output)

    async def _auth_hold(self) -> _Hold | None:
        """The hold an authentication failure puts on dispatch, or None; re-probes once per tick.

        A run that failed to authenticate (#20) is evidence the worker cannot work any issue,
        so it stops claiming rather than escalating one issue after another with an opaque
        blocker. A probe that reports a login (``ok`` or ``ambiguous``) lifts the hold, and an
        answer showing nothing has changed does not, because a failure has already happened --
        except that a probe which cannot answer at all is given only
        ``MAX_UNREADABLE_AUTH_PROBES`` ticks: a ``claude`` too old for ``auth status``, or a
        wedged one, would otherwise hold dispatch for good, and #17's rule that such a
        ``claude`` must not keep a worker down applies here too. Giving up falls back to the
        per-run escalation, which costs one issue per hold rather than one per attempt. The
        caller has just run preflight, so ``claude.command`` resolves.
        """
        if self._auth_block is None:
            return None
        auth = await self._probe_claude_auth(self._workflow.config.claude.command)
        if auth.verdict in ("ok", "ambiguous"):
            self._log.info(
                "dispatch_auth_recovered", claude_auth=auth.detail, error=self._auth_block
            )
            self._release_hold()
            return None
        if auth.verdict == "logged_out":
            self._unreadable_auth_probes = 0
        else:
            self._unreadable_auth_probes += 1
            if self._unreadable_auth_probes >= MAX_UNREADABLE_AUTH_PROBES:
                self._log.warning(
                    "dispatch_auth_hold_abandoned",
                    claude_auth=auth.detail,
                    probes=self._unreadable_auth_probes,
                    error=self._auth_block,
                )
                self._release_hold()
                return None
        if self._auth_block != self._reported_auth_block:
            self._log.error("dispatch_auth_held", claude_auth=auth.detail, error=self._auth_block)
            self._reported_auth_block = self._auth_block
        else:
            # An idle worker says nothing else, so the hold keeps reporting itself.
            self._log.warning("dispatch_auth_held", claude_auth=auth.detail, error=self._auth_block)
        return _Hold("auth", f"claude authentication unavailable: {auth.detail}", key=auth.verdict)

    async def _note_fetch_failure(self, error: str) -> None:
        """Count a failed read of the board and, past the threshold, hold dispatch (#88).

        This is first-party evidence that *this* worker cannot reach GitHub: it needs nobody to
        declare an incident, and it fails safe, since a worker that cannot read the board has no
        business claiming from it. One failure is a blip -- ``gh`` retries a transport error of
        its own before issuebot ever sees it -- so a hold waits for ``MAX_FETCH_FAILURES`` polls
        in a row, which is the third failure rather than the third interval.
        """
        self._fetch_failures += 1
        if self._fetch_failures < MAX_FETCH_FAILURES:
            return
        if self._github_block is None:
            # The hold engages now, which is the one moment the status page is read: once per
            # outage, off the event loop, and nowhere that dispatch depends on the answer.
            self._github_note = self._stamped(await self._probe_github_status())
        self._github_block = _github_hold_reason(error, self._github_note)
        context = {
            "error": error,
            "failures": self._fetch_failures,
            "github_status": self._github_note,
        }
        if self._github_block != self._reported_github_block:
            self._log.error("dispatch_github_held", **context)
            self._reported_github_block = self._github_block
        else:
            # An idle worker says nothing else, so the hold keeps reporting itself.
            self._log.warning("dispatch_github_held", **context)

    def _note_fetch_success(self) -> None:
        """The board answered, so any GitHub hold lifts at once (#88)."""
        if self._github_block is not None:
            self._log.info(
                "dispatch_github_recovered", failures=self._fetch_failures, error=self._github_block
            )
        self._forget_fetch_failures()

    def _note_fetch_skipped(self) -> None:
        """This tick asked GitHub nothing, so it has nothing to say about GitHub.

        A hold is a claim about now. When a preflight problem covers the fetch itself, or an
        authentication hold has no observer and no conflict bounce to poll for, no request is
        made at all -- and a block left over from before it would have ``_fire`` requeue a retry
        blaming GitHub while the snapshot names preflight. The outranking hold is what is
        reported either way; this only stops the stale one contradicting it.
        """
        self._forget_fetch_failures()

    def _forget_fetch_failures(self) -> None:
        self._fetch_failures = 0
        self._github_block = None
        self._github_note = None
        self._reported_github_block = None

    def _stamped(self, note: str | None) -> str | None:
        """When the reading was taken, which the reading itself does not say.

        It is taken once, at the moment the hold engages, and then sits on ``issuebot status``,
        ``<repo.api>/state`` and the dashboard for the whole outage. The page lags -- it read
        "All Systems Operational" for the first twenty-three minutes of the 2026-09-13 incident
        -- so an hour in, "operational" without a time on it is actively misleading.
        """
        return None if note is None else f"at {self._now():%H:%M}Z: {note}"

    async def _probe_github_status(self) -> str | None:
        """What githubstatus.com says, as annotation only; None when it does not answer.

        Fails open in every direction. The page is a lagging indicator -- it answered "All
        Systems Operational" through the first twenty-three minutes of the 2026-09-13 incident
        -- so it can name an outage the worker has already found, and nothing else. An
        operational answer is still worth carrying: it tells the operator to look at their own
        network rather than at GitHub's.

        Bounded on the wall clock as well as on the socket. ``fetch_status_summary``'s own
        timeout does not reach the name lookup, and a host whose nameservers are unreachable --
        one of the ways the ``gh`` polls come to fail in the first place -- can spend its
        resolver's whole budget there. Awaiting that inline would stall the tick, and with it
        the worker exits and the refresh the same loop is waiting on. The thread is not
        cancellable and runs on to its own end, but nothing waits for it: the probe is an
        annotation, and one it does not get in time is one it does not get.
        """
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(self._github_status), GITHUB_STATUS_DEADLINE_S
            )
            status = parse_status_summary(payload)
        except Exception as exc:
            # The timeout, the read *and* the parse: `tick` is not the place to find out that a
            # third party's body was the one shape its reader did not survive. TimeoutError is
            # an Exception, so the one handler covers all three.
            self._log.debug("github_status_probe_failed", error=f"{type(exc).__name__}: {exc}")
            return None
        return None if status is None else status.detail

    def _hold_dispatch(self, error: str) -> None:
        """Stop claiming issues until a probe reports the credential works again."""
        self._auth_block = error
        self._unreadable_auth_probes = 0

    def _release_hold(self) -> None:
        self._auth_block = None
        self._reported_auth_block = None
        self._unreadable_auth_probes = 0

    def _hold_snapshot(
        self, kind: DispatchHoldKind, reason: str, *, key: str | None = None
    ) -> None:
        """Carry why dispatch is held into the snapshot; a hold that lasts keeps its ``since``.

        ``key`` is what makes two holds the same one when the wording is not: an unreadable
        ``claude`` can garble its output differently on every probe, and the operator should
        still see how long the hold has really lasted.
        """
        identity = (kind, reason if key is None else key)
        if self._dispatch_hold is not None and identity == self._hold_identity:
            self._dispatch_hold = replace(self._dispatch_hold, reason=reason)
            return
        self._hold_identity = identity
        self._dispatch_hold = DispatchHold(kind=kind, reason=reason, since=self._now())

    def _release_snapshot_hold(self) -> None:
        self._dispatch_hold = None
        self._hold_identity = None

    # --- tick -------------------------------------------------------------------------

    async def tick(self) -> None:
        """Symphony §8.1: reconcile, reload, preflight, fetch, dispatch, snapshot."""
        await self.reconcile()
        self._reload_workflow()
        dispatched = 0
        hold: _Hold | None = None
        problems = preflight(self._workflow.config, which=self._which)
        if problems:
            message = "; ".join(problems)
            hold = _Hold("preflight", message)
            if message != self._reported_preflight:
                self._log.error("dispatch_preflight_failed", problems=problems)
                self._reported_preflight = message
            if fetch_preflight(self._workflow.config, which=self._which):
                # `gh` or the token is what is missing, so nothing is asked of GitHub (#88).
                self._note_fetch_skipped()
            else:
                # Only `claude` is missing, so the board can still be kept current (#29).
                await self._poll_issues()
        else:
            self._reported_preflight = None
            hold = await self._auth_hold()
            if hold is not None:
                # The fetch still works, so the board stays fresh while nothing is claimed (#29).
                await self._poll_issues()
            else:
                dispatched = await self._dispatch_candidates()
        self._settle_dispatch_hold(hold)
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

    def _settle_dispatch_hold(self, hold: _Hold | None) -> None:
        """Record this tick's one hold: the caller's if it has one, else GitHub's, else none.

        One place decides, and it decides after the fetch, because the snapshot carries a
        single hold whose ``since`` has to survive a tick that re-derives the same reason:
        releasing and re-holding would restart the clock on a hold that never lifted.
        ``preflight`` and ``auth`` outrank ``github`` -- both name something the operator can
        fix on this host, and a ``gh`` that will not run is why the fetch failed rather than a
        second, independent fault.
        """
        if hold is None and self._github_block is not None:
            hold = _Hold("github", self._github_block, key=GITHUB_HOLD_KEY)
        if hold is None:
            self._release_snapshot_hold()
        else:
            self._hold_snapshot(hold.kind, hold.reason, key=hold.key)

    def _slots(self) -> int:
        return max(self._workflow.config.agent.max_concurrent_agents - len(self._running), 0)

    def _publish_snapshot(self) -> None:
        if self._on_snapshot is None:
            return
        try:
            self._on_snapshot(self.snapshot())
        except Exception:
            self._log.exception("snapshot_consumer_failed")

    def _report_issues(self, issues: Sequence[Issue]) -> None:
        """Hand every fetched snapshot to the observer (the history store); never raises."""
        if self._on_issues is None or not issues:
            return
        try:
            self._on_issues(issues)
        except Exception:
            self._log.exception("issues_consumer_failed", count=len(issues))

    def _reload_workflow(self) -> None:
        workflow = self._workflow
        path = workflow.path
        try:
            source = path.stat()
        except OSError as exc:
            self._report_reload_failure(f"workflow file unreadable: {exc}")
            return
        overlay_path = overlay_path_for(path)
        try:
            overlay: os.stat_result | None = overlay_path.stat()
        except FileNotFoundError:
            # The normal case: no overlay.
            overlay = None
        except OSError as exc:
            # A file that exists and cannot be stat'ed is not something to guess about.
            self._report_reload_failure(f"workflow overlay unreadable: {exc}")
            return
        loaded = (
            workflow.source_identity,
            workflow.overlay_identity if workflow.overlay_path is not None else None,
        )
        found = (
            _identity(source),
            _identity(overlay) if overlay is not None else None,
        )
        if loaded == found:
            # Nothing to load: neither identity has moved and the overlay's presence has not
            # changed. The one thing left worth saying is that this deployment may not be
            # in a position to tell (#46), about either file. On this branch the files on
            # disk are exactly what was loaded, so the error state is exactly the complaint:
            # an error an overlay caused must clear once the overlay is gone.
            complaint = _pinned_mount_complaint(path, source)
            if complaint is None and overlay is not None:
                complaint = _pinned_mount_complaint(overlay_path, overlay)
            if complaint is not None:
                self._report_reload_failure(complaint)
            else:
                self._config_error = None
                self._reported_reload_error = None
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
        self._log.info(
            "workflow_reloaded", path=str(path), overlay=_overlay_name(workflow), changed=changed
        )

    def _report_reload_failure(self, message: str) -> None:
        self._config_error = message
        if message == self._reported_reload_error:
            return
        self._reported_reload_error = message
        self._log.error("workflow_reload_failed", path=str(self._workflow.path), error=message)

    def _conflict_limit(self) -> int:
        return self._workflow.config.agent.max_conflict_reworks

    async def _fetch_issues(self) -> Sequence[Issue] | None:
        """The polled issues, reported to the observer; None when the fetch failed."""
        states = fetch_states(
            observed=self._on_issues is not None, conflicts=self._conflict_limit() > 0
        )
        try:
            issues = await self._adapter.fetch_issues_by_states(states)
        except GitHubError as exc:
            self._log.warning("candidates_fetch_failed", error=str(exc))
            await self._note_fetch_failure(str(exc))
            return None
        self._note_fetch_success()
        self._report_issues(issues)
        await self._bounce_conflicts(issues)
        return issues

    async def _bounce_conflicts(self, issues: Sequence[Issue]) -> None:
        """Move each review issue whose pull request conflicts to rework (spec §3, §4).

        Skipped for an issue the orchestrator still holds: a running entry in its review
        grace would read the move as a human's and stop for the wrong reason, and a retry is
        an in-flight decision about the same issue. The next poll gets it.
        """
        limit = self._conflict_limit()
        if limit <= 0:
            return
        for issue in issues:
            if not conflict_candidate(issue):
                continue
            if issue.id in self._running or issue.id in self._retries:
                continue
            outcome = await actions.conflict_rework(
                self._adapter, self._bus, issue, limit=limit, now=self._now()
            )
            if outcome == "limit_noted":
                self._log.debug(
                    "conflict_rework_limit_noted",
                    issue_number=issue.number,
                    issue_identifier=issue.identifier,
                )

    async def _poll_issues(self) -> None:
        """Keep the history store current while dispatch is held, so the board does not go stale.

        An authentication hold stops ``claude``, not ``gh``, and a preflight hold may name only
        ``claude.command``; either way the fetch still works and the board can stay current. A
        hold that ``fetch_preflight`` reports on skips this, since the request would only fail.
        The request is worth making at all only when an observer is watching or the conflict
        bounce is on: a conflict is about the board, not about dispatch.
        """
        if self._on_issues is None and self._conflict_limit() <= 0:
            self._note_fetch_skipped()
            return
        await self._fetch_issues()

    async def _dispatch_candidates(self) -> int:
        issues = await self._fetch_issues()
        if issues is None:
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
            runner=self._runner_factory(settings_for_labels(workflow.config, entry.issue.labels)),
            attempt=entry.attempt,
            rework=entry.rework,
            resume_session_id=resume_session_id,
            cancel=entry.cancel,
            observer=RunObserver(
                entry, clock=self._clock, now=self._now, on_rate_limits=self._record_rate_limits
            ),
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
        """Re-read the issues this worker is running, to notice moves and closures.

        Deliberately not counted towards the GitHub hold (#88), even though this read fails in
        an outage too. The hold is about whether the board can be *claimed from*, and that is
        the poll's question: a poll that fails offers nothing to claim, and a poll that answers
        means the board is readable whatever this one did. Counting both into one threshold
        would make "three in a row" mean two different things and could hold dispatch while the
        poll is answering perfectly well.
        """
        ids = list(self._running)
        try:
            refreshed = await self._adapter.fetch_issues_by_ids(ids)
        except GitHubError as exc:
            self._log.warning("reconcile_refresh_failed", error=str(exc))
            return
        self._report_issues(refreshed)
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
            entry.terminal_issue = None
            if current.state is StateLabel.IN_PROGRESS and current.dispatchable:
                continue
            if current.state is StateLabel.REVIEW:
                if entry.review_seen_mono is None:
                    entry.review_seen_mono = self._clock()
                    self._log.info(
                        "reconcile_review_grace",
                        issue_number=current.number,
                        issue_identifier=current.identifier,
                        run_id=entry.run_id,
                        grace_ms=self._workflow.config.polling.interval_ms,
                    )
                elif self._clock() - entry.review_seen_mono >= self._review_grace_s():
                    self._stop_entry(entry, "moved", "review")
                continue
            detail = current.state.value if current.state is not None else "unlabelled"
            self._stop_entry(entry, "moved", detail)

    def _review_grace_s(self) -> float:
        """One poll interval: the grace before a worker whose issue reached review is stopped.

        Measured on the monotonic clock rather than in ticks, so a refresh-driven tick (a
        NOTIFY, Phase 6) cannot cut it short (Phase 6 spec §8.2).
        """
        return self._workflow.config.polling.interval_ms / 1000

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
        self._report_issues(issues)
        for issue in issues:
            if issue.id in self._running:
                continue
            self._retries.pop(issue.id, None)
            await self._finish(issue)

    async def _finish(self, issue: Issue) -> None:
        outcome = await actions.finish_terminal(self._adapter, self._bus, self._workspaces, issue)
        if outcome in ("complete", "no_change"):
            # A no-fault close is a completion here too: the investigation is the delivered work.
            self._counters = self._counters.bump(issues_completed=1)
        elif outcome == "cancelled":
            self._counters = self._counters.bump(issues_cancelled=1)

    # --- worker exits and retries -----------------------------------------------------

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
        if result is not None and result.outcome == "succeeded":
            self._publish_final_transition(entry, result)
        if task.cancelled() or entry.stop_cause in ("moved", "missing", "shutdown", "closed"):
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
            if result.stop_reason == "blocked" and result.final_state is StateLabel.IN_PROGRESS:
                # The agent said so on its final line (spec 2026-09-13-blocked-escape-design.md);
                # an external blocker does not clear by retrying, so escape now.
                reason = result.blocker or "the session reported a blocker"
                await self._escape(entry, reason, result)
                return
            self._schedule(
                entry.issue,
                attempt=1,
                kind="continuation",
                delay_ms=CONTINUATION_DELAY_MS,
                error=None,
            )
            return
        if result.error_category == "auth_failed":
            await self._auth_failed(entry, result)
            return
        await self._after_failure(entry, f"{result.error_category}: {result.error}", result)

    async def _auth_failed(self, entry: RunningEntry, result: RunResult) -> None:
        """#20: hold dispatch, then escalate this issue with a blocker that names the cause.

        Retrying is pointless while the credential is the problem, and the attempts would only
        spread opaque blockers over every issue in the queue, so the escape happens at once.
        """
        error = result.error or "claude could not authenticate"
        self._log.error(
            "dispatch_auth_failed",
            issue_number=entry.issue.number,
            issue_identifier=entry.identifier,
            run_id=entry.run_id,
            attempt=entry.attempt,
            error=error,
        )
        self._hold_dispatch(error)
        reason = (
            f"Claude could not authenticate in attempt {entry.attempt}, so the run failed: "
            f"{error}. The worker has stopped claiming issues and re-checks the credential "
            "every poll; it resumes on its own once `claude auth status` reports a login."
        )
        await self._escape(entry, reason, result)

    def _publish_final_transition(self, entry: RunningEntry, result: RunResult) -> None:
        """What changed between the entry's snapshot and the session's last refresh (§4.2).

        Published before the release rows so a move that lands while the worker is being
        stopped (shutdown, or a human move seen by reconcile) still reaches the bus.
        """
        final = result.final_issue
        if final is not None and final.github_state == "open":
            for event in observe_transition(entry.issue, final):
                self._bus.publish(event)

    def _add_elapsed(self, entry: RunningEntry) -> None:
        elapsed = self._clock() - entry.started_mono
        self._totals = replace(
            self._totals, seconds_running=round(self._totals.seconds_running + elapsed, 3)
        )

    async def _after_failure(
        self, entry: RunningEntry, error: str, result: RunResult | None
    ) -> None:
        # The retry's `error` reaches the snapshot (`issuebot status`, `/state`, the dashboard)
        # and the escape's reason reaches the issue; a `worker crashed: <exc>` names whatever
        # the exception did, so every message leaves through the scrubber once, here (#91).
        error = self._scrubber.scrub(error)
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
        log_dir = str(result.log_dir) if result is not None and result.log_dir else None
        context = BlockedContext(
            reason=self._scrubber.scrub(reason),
            run_id=entry.run_id,
            attempt=entry.attempt,
            turns=result.turns if result is not None else entry.turns,
            log_dir=self._scrubber.scrub(log_dir) if log_dir else None,
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
            title=issue.title,
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
        if self._auth_block is not None:
            self._requeue(
                entry,
                kind="auth",
                delay_ms=settings.polling.interval_ms,
                error=f"claude authentication unavailable: {self._auth_block}",
            )
            return
        if self._github_block is not None:
            # Claiming is a write to the board this worker has just failed to read (#88), so a
            # retry waits with dispatch rather than spending an attempt on a request that is
            # going to fail. The escape above goes first for the same reason it does under an
            # authentication hold: it is the one retry whose whole job is to leave a note.
            self._requeue(
                entry,
                kind="github",
                delay_ms=settings.polling.interval_ms,
                error=self._github_block,
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
        self._report_issues(issues)
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

    # --- the loop -----------------------------------------------------------------------

    async def run(self) -> None:
        """Startup, then tick and wait until stopped; shutdown on the way out."""
        await self.startup()
        try:
            while not self._stopping:
                await self.tick()
                await self._wait_for_next_tick()
        finally:
            await self.shutdown()

    async def _wait_for_next_tick(self) -> None:
        deadline = self._clock() + self._workflow.config.polling.interval_ms / 1000
        while not self._stopping:
            await self.fire_due_retries()
            now = self._clock()
            if now >= deadline:
                return
            timeout = min(deadline, self._earliest_due()) - now
            if timeout <= 0:
                continue
            try:
                message = await asyncio.wait_for(self._queue.get(), timeout)
            except TimeoutError:
                continue
            if message is _REFRESH:
                self._refresh_pending = False
                return
            if message is _STOP:
                return
            if isinstance(message, _WorkerExited):
                await self.handle_worker_exit(message.issue_id)

    def request_refresh(self) -> None:
        """Ask for a tick now; coalesced while one is already pending."""
        if self._refresh_pending:
            return
        self._refresh_pending = True
        self._queue.put_nowait(_REFRESH)

    def request_stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._log.info("stop_requested")
        self._queue.put_nowait(_STOP)

    async def shutdown(self) -> None:
        """Cancel every worker, wait for after_run, drain the exits, drop the retries."""
        self._stopping = True
        settings = self._workflow.config
        if self._running:
            self._log.info("shutdown_started", running=len(self._running))
            tasks: list[asyncio.Task[RunResult]] = []
            for entry in self._running.values():
                entry.stop("shutdown", "worker stopping")
                if entry.task is not None:
                    tasks.append(entry.task)
            timeout = settings.hooks.timeout_ms / 1000 + TERMINATE_GRACE_S + SHUTDOWN_MARGIN_S
            _done, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                self._log.warning("shutdown_timeout", pending=len(pending), timeout_s=timeout)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            for issue_id in list(self._running):
                await self.handle_worker_exit(issue_id)
        self._retries.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
        counters = self._counters
        self._log.info(
            "orchestrator_stopped",
            runs_started=counters.runs_started,
            runs_ended=counters.runs_ended,
            issues_completed=counters.issues_completed,
            issues_cancelled=counters.issues_cancelled,
            blocked=counters.blocked,
            cost_usd=self._totals.cost_usd,
        )
        self._publish_snapshot()


def _identity(source: os.stat_result) -> tuple[int, int, int]:
    return (source.st_dev, source.st_ino, source.st_mtime_ns)


def _overlay_name(workflow: Workflow) -> str | None:
    return str(workflow.overlay_path) if workflow.overlay_path is not None else None


def _changed_sections(old: Workflow, new: Workflow) -> list[str]:
    changed = [
        name
        for name in type(old.config).model_fields
        if getattr(old.config, name) != getattr(new.config, name)
    ]
    if old.prompt_template != new.prompt_template:
        changed.append("prompt")
    return changed


MOUNT_ADVICE = (
    "Mount the directory that holds it instead, and name the file with ISSUEBOT_WORKFLOW "
    "(compose.yaml mounts ./configs at /configs); recreate the container to pick up a file "
    "it is already stale on."
)


def _pinned_mount_complaint(path: Path, source: os.stat_result) -> str | None:
    """Why this workflow file may be one the host can no longer reach, or None (#46).

    A single-file bind mount resolves to the inode, not the path, so an editor that saves by
    writing a temporary file and renaming it over the original -- most of them, and `sed -i`
    too -- gives the host path a new inode and leaves the container pinned to the old one.
    Its mtime never moves again, so a watcher sees an unchanging file forever and says
    nothing, which is the silence #46 was really about: the settings in force are valid, just
    not the ones on disk.

    Two signals, weakest claim first:

    * ``st_nlink == 0`` -- the file has already been replaced. A name that still resolves to
      an inode no directory entry points at is not something a filesystem lookup can normally
      produce; once the last link goes, the name goes with it. A mount holding the inode open
      is what makes it reachable.
    * the file's device differs from its own directory's -- the file *is* a mount point, so
      it will go stale the first time anyone saves it. A directory entry can only name an
      inode on its own filesystem, so a difference means a mount and nothing else. This is
      the signal that catches what ``st_nlink`` cannot: a save that left the old inode with a
      link (a retained backup, a hard link), and Docker Desktop's virtiofs, where the guest's
      link count need not follow the host's rename at all. The supported arrangement -- the
      directory mounted, the file inside it -- shares a device with its parent and is silent
      here.

    Called only when the file's identity has not changed, so it can never suppress a reload:
    a wrong answer costs a log line and a ``config_error``, never a setting. That also bounds
    the one race it has. ``stat`` resolves the name and then reads the inode, so a host-side
    rename committing in between can be seen as ``nlink == 0`` on a perfectly healthy
    directory mount; the next tick reloads and clears it.
    """
    if source.st_nlink == 0:
        return (
            f"stale mount: {path} resolves to an unlinked inode, so it is mounted as a file "
            "and the host has replaced it. Edits made there cannot be seen from here, and "
            f"the running configuration is the one this file held at start. {MOUNT_ADVICE}"
        )
    try:
        parent = path.parent.stat()
    except OSError:
        return None
    if source.st_dev != parent.st_dev:
        return (
            f"single-file mount: {path} is a mount point rather than an entry in "
            f"{path.parent}, so it is pinned to one inode. Saving it on the host the way "
            "most editors do, by writing a temporary file and renaming it over the original, "
            f"will leave this process reading the old file with nothing to show for it. "
            f"{MOUNT_ADVICE}"
        )
    return None
