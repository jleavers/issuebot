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
from typing import Any, NoReturn, Protocol

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
    workspace_key,
)
from issuebot.agent.accounts import (
    AccountRegistry,
    credential_complaint,
    group_complaint,
    pool_complaint,
    session_account,
    settings_with_run_as,
)
from issuebot.agent.runas import RunAs
from issuebot.agent.runner import TERMINATE_GRACE_S, Credential, RateLimits, agent_environment
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
from issuebot.orchestrator.admission import (
    Admission,
    AdmissionRequest,
    Hold,
    IssueLedger,
    Ledger,
    RefusalKind,
    Refused,
    admit,
    seeded_chain,
)
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
# And the same for the account registry (#121): one unreadable record, however the
# complaint is worded from tick to tick.
ACCOUNTS_HOLD_KEY = "accounts"
# The least time between the end of one tick and the start of the next when a refresh asks
# for it (#110). A NOTIFY is one row on the store away from any client with its DSN, and
# every tick polls GitHub with this worker's token, so the NOTIFY rate must not set the tick
# rate: a refresh inside the interval is admitted when the interval is up, one tick for
# however many asked, and never dropped. The web throttles its own POST to the same figure.
MIN_REFRESH_INTERVAL_S = 5.0
# How much of `gh`'s complaint the hold's reason carries. Neither `gh`'s stderr nor GitHub's
# GraphQL messages are bounded, and the reason is stored and drawn on every tick it lasts.
MAX_HOLD_ERROR_CHARS = 300


# `claude auth status --json` as the startup probe runs it: the resolved command and the parent
# environment, stdout or None. A seam like `which`, so tests never spawn a process.
class ClaudeAuthProbe(Protocol):
    def __call__(
        self, command: str, environ: Mapping[str, str], /, *, run_as: str | None = None
    ) -> str | None: ...


class RunAsProbe(Protocol):
    def __call__(self, accounts: Sequence[str], environ: Mapping[str, str], /) -> list[str]: ...


def probe_run_as(accounts: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    """Every reason these accounts cannot be the sessions' own (#75, #111, #121), or ``[]``.

    Three questions, and the first that fails is the one worth reporting: can the worker run a
    command as the account *and* land at a uid that is not its own (`RunAs.probe`, which is
    where #111's separation check lives, so every member of a pool is held to it), can it give
    a workspace to that account's group, and -- for a pool -- do the accounts have groups of
    their own, without which each could enter the others' workspaces. The account is named only
    when there is more than one, since a single account's own error already says which it is.
    """
    env = agent_environment(environ, token=None)
    problems = []
    for account in accounts:
        error = RunAs(account).probe(env) or group_complaint(account)
        if error is not None:
            problems.append(f"{account}: {error}" if len(accounts) > 1 else error)
    if problems:
        return problems
    try:
        shared = pool_complaint(accounts)
    except AgentError as exc:
        return [exc.message]
    return [] if shared is None else [shared]


# githubstatus.com's summary body, or None. A seam for the same reason: no test reaches the
# network, and nothing issuebot decides depends on the answer.
GitHubStatusProbe = Callable[[], str | None]


def fetch_states(*, observed: bool, conflicts: bool) -> tuple[StateLabel, ...]:
    """Which states a poll fetches: review rides along for the history store, or for the
    conflict bounce, and the dispatch loop never runs it either way."""
    return OBSERVED_STATES if observed or conflicts else CANDIDATE_STATES


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _pool_for(settings: Settings) -> AccountRegistry | None:
    """The account registry a pooled ``agent.run_as`` needs, or ``None``.

    One account -- or none -- binds nothing: every workspace would answer the same name, and
    an existing deployment gains no file it did not have (#121).
    """
    if not settings.agent.run_as_pooled:
        return None
    return AccountRegistry(settings.workspace.root, settings.agent.run_as)


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


# What a refusal with nothing to wait for is called in the `retry_released` line.
_RELEASE_REASONS: dict[RefusalKind, str] = {
    "busy": "already claimed",
    "inactive": "not_active",
    "attempts": "attempts exhausted",
    "spend": "spend exhausted",
}


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
        run_as_probe: RunAsProbe = probe_run_as,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
        environ: Mapping[str, str] | None = None,
        initial_rate_limits: RateLimits | None = None,
        initial_ledger: Mapping[str, IssueLedger] | None = None,
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
        self._run_as_probe = run_as_probe
        self._clock = clock
        self._now = now
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._on_snapshot = on_snapshot
        self._on_issues = on_issues
        self._adapter = adapter_factory(workflow.config.github)
        # The manager the orchestrator itself reads workspaces through: `path_for` and
        # `read_session` ask nothing about the session's account, and everything that does --
        # the clone, the hooks, a removal -- goes through one narrowed to the workspace's own
        # bound account (#121).
        self._workspaces = workspaces_factory(workflow.config)
        self._pool = _pool_for(workflow.config)
        self._running: dict[str, RunningEntry] = {}
        self._retries: dict[str, RetryEntry] = {}
        self._log = get_logger(__name__)
        self._totals = ClaudeTotals()
        # The admission gate's durable half (#112): what each issue has cost this worker,
        # keyed by identifier and outliving every label it wears. Seeded from the store by the
        # caller that has one, the way `initial_rate_limits` is, because restarting is how
        # this worker is deployed and a budget that a deployment resets is not a ceiling.
        self._ledger = Ledger(
            _seeded(initial_ledger, workflow.config.agent.max_attempts),
            on_evict=self._note_ledger_eviction,
        )
        self._counters = Counters()
        self._tick_count = 0
        self._last_tick_at: datetime | None = None
        self._config_error: str | None = None
        self._dispatch_hold: DispatchHold | None = None
        self._hold_identity: tuple[str, str] | None = None
        self._reported_reload_error: str | None = None
        # The three holds are now all state on this object, in the order they outrank each
        # other (#112). The preflight one used to be a local inside `tick`, which is why the
        # retry timer could honour the other two and not it: there was nothing to consult.
        self._preflight_block: str | None = None
        self._auth_block: str | None = None
        # How the authentication hold words itself, and what makes two of them the same one.
        # Both callers read this, so neither can refuse on terms the other would not.
        self._auth_reason: str | None = None
        self._auth_key: str | None = None
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
        # The account registry of #121: why this tick could not bind a workspace to an account,
        # which is a hold rather than a silent skip -- without it `issuebot status`, the
        # dashboard and `/healthz` would all read as healthy while nothing was ever claimed.
        self._accounts_block: str | None = None
        # Why the pool `agent.run_as` now names would not work (#121). Startup refuses one that
        # does not, but a reload can introduce the same faults, so a changed setting is probed
        # again and a failure holds dispatch rather than failing every session it claims for.
        self._run_as_block: str | None = None
        self._run_as_pending = False
        self._github_block: str | None = None
        # Issues whose conflict-bounce limit this process has already noted on the workpad,
        # and the limit it noted (#104): the note's presence in the workpad is the session's
        # to erase, so without this a stripped note would be rewritten on every tick.
        self._conflict_limit_noted: dict[str, int] = {}
        # Issues whose bounce failed on a `response` error, keyed to the issue's `updated_at`
        # when it did (#110): the reads the bounce makes are bounded, but a bound repeated
        # every poll for as long as the answer stays the same is the tick rate set by the
        # thread's length. The next change to the issue is what earns another try.
        self._conflict_gave_up: dict[str, datetime] = {}
        self._github_note: str | None = None
        self._reported_github_block: str | None = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._refresh_pending = False
        self._stopping = False

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
        # The boundary the deployment asked for has to exist before an issue is claimed
        # (#75): a delegation that does not work would fail every run instead.
        if settings.agent.run_as:
            unusable = await asyncio.to_thread(
                self._run_as_probe, settings.agent.run_as, self._environ
            )
            problems.extend(f"agent.run_as: {error}" for error in unusable)
        # A pool shares no login between its accounts on purpose (#121), so the credential has
        # to be one `claude` needs no file for. Refusing here rather than per run: every
        # session would fail to authenticate, which is #17's rule for a definite logged-out.
        complaint = credential_complaint(settings, self._environ)
        if complaint is not None:
            problems.append(f"agent.run_as: {complaint}")
        auth = await self._probe_claude_auth(settings.claude.command)
        self._credential = auth.credential
        if auth.verdict == "logged_out":
            problems.append(f"claude auth: {auth.detail}")
        elif auth.verdict != "ok":
            self._log.warning("orchestrator_startup_warning", claude_auth=auth.detail)
        if problems:
            self._startup_failed(problems)
        # Nothing is running, so every workspace on disk is idle: a worker that was killed
        # outright could have left one open to its account (#121). Close them all; the next
        # dispatch opens the one it needs.
        self._workspaces.seal_idle()
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
            session_accounts=list(settings.agent.run_as),
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
        output = await asyncio.to_thread(
            self._claude_auth, found, self._environ, run_as=session_account(self._workflow.config)
        )
        return describe_claude_auth(output)

    async def _refresh_auth_hold(self) -> bool:
        """True while an authentication failure holds dispatch; re-probes once per tick.

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
            return False
        auth = await self._probe_claude_auth(self._workflow.config.claude.command)
        if auth.verdict in ("ok", "ambiguous"):
            self._log.info(
                "dispatch_auth_recovered", claude_auth=auth.detail, error=self._auth_block
            )
            self._release_hold()
            return False
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
                return False
        if self._auth_block != self._reported_auth_block:
            self._log.error("dispatch_auth_held", claude_auth=auth.detail, error=self._auth_block)
            self._reported_auth_block = self._auth_block
        else:
            # An idle worker says nothing else, so the hold keeps reporting itself.
            self._log.warning("dispatch_auth_held", claude_auth=auth.detail, error=self._auth_block)
        # The probe's own words, so the snapshot's reason and the reason a waiting retry
        # carries are the same string (#112); the verdict keys the hold's `since`.
        self._auth_reason = f"claude authentication unavailable: {auth.detail}"
        self._auth_key = auth.verdict
        return True

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
        """Stop claiming issues until a probe reports the credential works again.

        The reason starts as the run's own error and is re-worded by the next tick's probe;
        either way it is the one string every caller refuses on.
        """
        self._auth_block = error
        self._auth_reason = f"claude authentication unavailable: {error}"
        self._auth_key = None
        self._unreadable_auth_probes = 0

    def _release_hold(self) -> None:
        self._auth_block = None
        self._auth_reason = None
        self._auth_key = None
        self._reported_auth_block = None
        self._unreadable_auth_probes = 0

    def _current_hold(self) -> Hold | None:
        """This worker's one live reason not to claim anything, whoever is asking (#112).

        ``preflight`` and ``auth`` outrank ``accounts`` and ``github`` -- all three name
        something the operator can fix on this host, and a ``gh`` that will not run is why the
        fetch failed rather than a second, independent fault. One function composes it, so the
        snapshot the operator reads and the answer the gate gives a caller can never be two
        different claims -- which is why the account hold (#121) is asked here rather than
        only where the snapshot is written: a record that will not name the account a session
        would run as is a reason to claim nothing, whichever door the claim arrives at.
        """
        if self._preflight_block is not None:
            return Hold("preflight", self._preflight_block)
        if self._auth_reason is not None:
            return Hold("auth", self._auth_reason, key=self._auth_key)
        accounts = self._accounts_hold()
        if accounts is not None:
            # Keyed on *which* fault, not on the constant: an unusable setting and an unreadable
            # record are different holds, and a move from one to the other should restart
            # `since` rather than inherit the other's.
            key = f"{ACCOUNTS_HOLD_KEY}:{'run_as' if self._run_as_block else 'record'}"
            return Hold("accounts", accounts, key=key)
        if self._github_block is not None:
            return Hold("github", self._github_block, key=GITHUB_HOLD_KEY)
        return None

    def _note_ledger_eviction(self, identifier: str, entry: IssueLedger) -> None:
        """An evicted entry is a budget reset, so it is never silent."""
        self._log.info(
            "ledger_evicted",
            issue_identifier=identifier,
            runs=entry.runs,
            failures=entry.failures,
            cost_usd=entry.cost_usd,
        )

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
        await self._settle_run_as()
        # Before any hold is decided, and not inside `_dispatch_candidates`, which a preflight
        # or auth hold skips: a reason `fire_due_retries` quotes has to be about this tick.
        self._read_accounts()
        dispatched = 0
        problems = preflight(self._workflow.config, which=self._which)
        if problems:
            message = "; ".join(problems)
            if message != self._preflight_block:
                self._log.error("dispatch_preflight_failed", problems=problems)
            # State, not a local (#112): the retry timer has to be able to consult it too.
            self._preflight_block = message
            if fetch_preflight(self._workflow.config, which=self._which):
                # `gh` or the token is what is missing, so nothing is asked of GitHub (#88).
                self._note_fetch_skipped()
            else:
                # Only `claude` is missing, so the board can still be kept current (#29).
                await self._poll_issues()
        else:
            self._preflight_block = None
            if await self._refresh_auth_hold():
                # The fetch still works, so the board stays fresh while nothing is claimed (#29).
                await self._poll_issues()
            else:
                dispatched = await self._dispatch_candidates()
        self._settle_dispatch_hold()
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

    def _settle_dispatch_hold(self) -> None:
        """Carry this tick's one hold, from ``_current_hold``, into the snapshot.

        It settles after the fetch, because the snapshot carries a single hold whose ``since``
        has to survive a tick that re-derives the same reason: releasing and re-holding would
        restart the clock on a hold that never lifted.
        """
        hold = self._current_hold()
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
        self._run_as_pending = workflow.config.agent.run_as != self._workflow.config.agent.run_as
        self._workflow = workflow
        self._config_error = None
        self._reported_reload_error = None
        self._adapter = self._adapter_factory(workflow.config.github)
        self._workspaces = self._workspaces_factory(workflow.config)
        self._pool = _pool_for(workflow.config)
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
            if self._conflict_limit_noted.get(issue.id) == limit:
                continue
            if self._conflict_gave_up.get(issue.id) == issue.updated_at:
                continue
            outcome = await actions.conflict_rework(
                self._adapter, self._bus, issue, limit=limit, now=self._now()
            )
            if outcome == "gave_up":
                self._conflict_gave_up[issue.id] = issue.updated_at
                self._log.warning(
                    "conflict_rework_abandoned",
                    issue_number=issue.number,
                    issue_identifier=issue.identifier,
                    updated_at=issue.updated_at.isoformat(),
                )
            else:
                self._conflict_gave_up.pop(issue.id, None)
            if outcome in ("limit_reached", "limit_noted"):
                self._conflict_limit_noted[issue.id] = limit
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

    def _admit(self, identifier: str, issue_id: str, issue: Issue | None) -> Admission:
        """Ask the one gate (#112). Every precondition is its, and it reads the live holds.

        Live rather than ``self._dispatch_hold``, which only settles at the end of a tick: a
        GitHub hold that this tick's successful fetch has just lifted must not refuse the
        claim that fetch produced.
        """
        agent = self._workflow.config.agent
        return admit(
            AdmissionRequest(
                issue=issue,
                ledger=self._ledger.get(identifier),
                hold=self._current_hold(),
                slots=self._slots(),
                busy=issue_id in self._running or issue_id in self._retries,
                stopping=self._stopping,
                max_attempts=agent.max_attempts,
                max_issue_cost_usd=agent.max_issue_cost_usd,
            )
        )

    async def _handle_refusal(self, issue: Issue, verdict: Refused) -> None:
        """What a refusal about *this issue* costs it: a log line, and for a budget, the escape.

        ``busy`` and ``inactive`` are the loop passing over an issue it has already claimed or
        does not want, which is every tick's normal business and says nothing. A budget refusal
        is the board having stopped moving for that issue, and a refusal nobody can see would
        be worse than having no ceiling at all -- so it is written on the issue and the issue
        is handed to a human, which is also what stops it being refused again every tick.

        An escape that GitHub refuses is retried by the next tick rather than by a queued
        entry with a backoff: the issue is still a candidate, so the loop comes back to it on
        its own, one poll interval later. That is the same cadence the other holds report
        themselves at, and it costs nothing while GitHub is answering.

        The escalation is announced once, and the ledger is what remembers that: the conflict
        bounce can return an over-budget ``review`` issue to ``rework`` for the gate to refuse
        again, and one escalation must not become a Slack line and a count on the blocked tile
        per bounce. The entry is marked only *after* the escape landed, so an escape GitHub
        refused -- which has written the block but published nothing -- is still announced by
        the tick that retries it; and only a run clears the mark, so the next refusal after one
        is a new fact and is announced again.
        """
        if verdict.kind not in ("attempts", "spend"):
            return
        if self._ledger.refused(issue.identifier, verdict.reason):
            self._log.warning(
                "dispatch_refused",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                refusal=verdict.kind,
                reason=verdict.reason,
            )
        outcome = await actions.budget_escape(
            self._adapter,
            self._bus,
            issue.id,
            verdict.kind,
            verdict.reason,
            now=self._now(),
            announce=not self._ledger.get(issue.identifier).escalated,
        )
        if outcome == "applied":
            self._ledger.escalate(issue.identifier)
            self._counters = self._counters.bump(blocked=1)
        # `applied` and `skipped` both end the chain: the issue is a human's now. A `failed`
        # one leaves it, so the next tick tries again -- the issue is still a candidate.
        self._record_escape(issue.identifier, outcome)

    async def _dispatch_candidates(self) -> int:
        issues = await self._fetch_issues()
        if issues is None:
            return 0
        dispatched = 0
        for issue in sort_candidates(issues):
            verdict = self._admit(issue.identifier, issue.id, issue)
            if isinstance(verdict, Refused):
                if verdict.kind in ("slots", "hold", "stopping"):
                    # Nothing about this issue; the next candidate would be told the same.
                    break
                await self._handle_refusal(issue, verdict)
                continue
            resume_session_id = None
            if issue.state is StateLabel.IN_PROGRESS:
                plan = self._resume_plan(issue)
                if plan is None:
                    continue
                resume_session_id, recorded = plan
                if recorded > verdict.attempt:
                    # The workspace record is durable per-issue history too, and the only kind
                    # a worker without a store has. Fold it in and ask the gate again rather
                    # than taking the attempt number from it here: that is how the two call
                    # sites came to disagree in the first place.
                    self._ledger.observed(
                        issue.identifier,
                        attempt=recorded,
                        max_attempts=self._workflow.config.agent.max_attempts,
                    )
                    # `observed` caps the chain below `max_attempts` and touches nothing else,
                    # so this cannot refuse today. It is asked anyway because the attempt
                    # number is the gate's to give: a cap that ever moves must not quietly
                    # start handing out a number the gate would have refused.
                    verdict = self._admit(issue.identifier, issue.id, issue)
                    if isinstance(verdict, Refused):
                        await self._handle_refusal(issue, verdict)
                        continue
            if await self._dispatch(
                issue, attempt=verdict.attempt, resume_session_id=resume_session_id
            ):
                dispatched += 1
        return dispatched

    def _resume_plan(self, issue: Issue) -> tuple[str | None, int] | None:
        """How to dispatch an orphaned in_progress issue: the session to resume and the
        attempt its workspace last recorded, or None when it cannot be dispatched at all."""
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
            return record.session_id, record.attempt
        return None, 1

    async def _settle_run_as(self) -> None:
        """Re-run the pool's admission checks when a reload changed ``agent.run_as`` (#121).

        Startup refuses a delegation that does not work, a worker outside a session account's
        group, two accounts sharing one, and a pool with no credential in the environment,
        because each of those fails every run rather than one. `agent.run_as` is a setting like
        any other, so a reload can introduce all four -- and a worker that reloaded into one
        would go on claiming issues no session could work. The same checks therefore run again,
        once per change, and a failure holds dispatch as ``accounts`` instead of ending the
        process: putting the file back lifts it on the next reload, which is what a running
        deployment wants of a typo. Nothing is sealed here, unlike at startup: sessions are
        running, and their workspaces are open to the accounts they are running as.

        It re-probes while the hold lasts, not only when the setting moves, because the faults
        are properties of the *host* as much as of the file, and the operator who fixes one
        never touches ``WORKFLOW.md``: a hold keyed on the file alone would then last until the
        worker was restarted, which is the very thing `_read_accounts` is written to avoid.
        Once a tick while held, which is one poll interval, and not at all when there is
        nothing to re-check.

        Not every fault clears without a restart, and the log says so rather than this pretending
        otherwise. An account that does not exist is read through NSS on each probe, so
        ``useradd`` lifts the hold on the next tick. A worker outside the account's group does
        not: `group_complaint` asks `os.getgroups()`, which is what the kernel authorises
        `share_with`'s ``chgrp`` by and is fixed when the process is exec'd, so a
        ``usermod --append`` reaches only the *next* worker -- and the complaint names the
        restart. A credential missing from the environment is the same. Re-probing is still
        right: it costs one bounded probe per account per tick (at most
        ``len(run_as) * SUDO_TIMEOUT_S`` where sudo is wedged rather than merely
        refusing), it lifts what can be lifted, and the
        alternative is a hold that outlives its cause.
        """
        if not self._run_as_pending and self._run_as_block is None:
            return
        self._run_as_pending = False
        settings = self._workflow.config
        problems: list[str] = []
        if settings.agent.run_as:
            problems.extend(
                await asyncio.to_thread(self._run_as_probe, settings.agent.run_as, self._environ)
            )
        complaint = credential_complaint(settings, self._environ)
        if complaint is not None:
            problems.append(complaint)
        previous = self._run_as_block
        self._run_as_block = f"agent.run_as: {'; '.join(problems)}" if problems else None
        if problems:
            # ERROR on the first and on a changed reason, WARNING after: a held worker says
            # nothing else, and an unchanged fault should not fill the log with it.
            if self._run_as_block != previous:
                self._log.error("dispatch_run_as_unusable", problems=problems)
            else:
                self._log.warning("dispatch_run_as_unusable", problems=problems)
        elif previous is not None:
            self._log.info("dispatch_run_as_recovered", run_as=list(settings.agent.run_as))

    def _accounts_hold(self) -> str | None:
        """Why no workspace can be bound to a session account: the setting first, then the
        record, since a pool that cannot be used at all makes the record's state moot."""
        return self._run_as_block or self._accounts_block

    def _read_accounts(self) -> None:
        """Re-derive the accounts hold from the record itself, once a tick (#121).

        A statement about the record rather than about a candidate, so it can neither stick
        after the file is fixed nor vanish on a tick whose only dispatchable work was a retry
        the candidate loop skips. `_bind_account` may still set it, for a failure the plain
        read did not see.
        """
        if self._pool is None:
            self._accounts_block = None
            return
        try:
            self._pool.bindings()
        except AgentError as exc:
            self._accounts_block = exc.message
        else:
            self._accounts_block = None

    def _pool_keys(self) -> set[str]:
        """The workspace keys a binding must survive whether or not the directory exists yet:
        every running session's and every pending retry's."""
        return {
            workspace_key(identifier)
            for identifier in (
                *(entry.identifier for entry in self._running.values()),
                *(entry.identifier for entry in self._retries.values()),
            )
        }

    def _bind_account(self, issue: Issue) -> tuple[str | None, bool]:
        """The account this issue's session runs as, and whether it can be dispatched now.

        Without a pool there is one account (or none) and nothing to wait for, exactly as
        before. With one (#121), the answer is the workspace's *recorded* binding -- so a
        rework lands back in a clone its uid can still write -- and a candidate whose account
        is already running waits for a later tick rather than sharing a uid with it. Never
        derived from the directory: that would let whoever writes an issue choose which
        honest session it sits beside.
        """
        if self._run_as_block is not None:
            # The setting itself is unusable after a reload (#121): no account can be bound,
            # pool or not, so nothing is claimed until it is put back. `_current_hold` is
            # already reporting why, to the snapshot and to the admission gate alike.
            return None, False
        if self._pool is None:
            return session_account(self._workflow.config), True
        key = workspace_key(issue.identifier)
        try:
            # This worker's own sessions, and any other process's: an open workspace is what a
            # running session looks like from outside, which is what `run-once` beside a live
            # worker would otherwise be invisible as (#121).
            busy = {
                entry.account for entry in self._running.values() if entry.account is not None
            } | self._pool.busy_accounts()
            account = self._pool.bound(key) or self._pool.allocate(key, busy=busy)
        except AgentError as exc:
            # A record that will not read is not "busy": it stops every candidate, so it holds
            # dispatch and says so rather than leaving a healthy-looking worker idle (#121).
            self._accounts_block = exc.message
            self._log.warning("account_bind_failed", issue_number=issue.number, error=exc.message)
            return None, False
        if account is None or account in busy:
            self._log.debug("dispatch_deferred", issue_number=issue.number, reason="account_busy")
            return None, False
        return account, True

    async def _dispatch(self, issue: Issue, *, attempt: int, resume_session_id: str | None) -> bool:
        # Before the claim: an issue whose account is busy must not be moved to in_progress
        # only to sit there until a slot opens.
        account, ready = self._bind_account(issue)
        if not ready:
            return False
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
            account=account,
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
        ledger = self._ledger.dispatched(issue.identifier, at=self._now())
        self._log.info(
            "dispatched",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            attempt=attempt,
            rework=rework,
            resumed=entry.resumed,
            run_id=entry.run_id,
            account=account,
            slots_left=self._slots(),
            issue_runs=ledger.runs,
            issue_cost_usd=ledger.cost_usd,
        )
        return True

    async def _worker(
        self, entry: RunningEntry, workflow: Workflow, resume_session_id: str | None
    ) -> RunResult:
        # The session's own settings: its model label's model, and the one account bound to
        # its workspace (#121), so neither the runner nor the workspace manager below here
        # has a pool to reason about.
        settings = settings_with_run_as(
            settings_for_labels(workflow.config, entry.issue.labels), entry.account
        )
        return await self._run_session(
            entry.issue,
            workflow,
            self._adapter,
            self._bus,
            workspaces=self._workspaces_factory(settings),
            runner=self._runner_factory(settings),
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
        # After the removals above, so a workspace this sweep deleted gives its account back
        # now rather than on the next one (#121).
        self._prune_accounts()

    async def _finish(self, issue: Issue) -> None:
        """Close the issue out, and drop what this worker remembered about it.

        Both conflict memos go whatever GitHub answered: a closed issue is never a bounce
        candidate again. The ledger entry goes only when the close actually landed, since a
        ``failed`` one leaves the issue open and its budget still in force. A reopened issue
        starts from zero either way. Dropping ``_conflict_gave_up`` here is what keeps it a
        memo rather than a leak: an issue that hit a capped read once would otherwise hold an
        entry for the life of the process, which is the growth this issue is about (#110).
        """
        # Removing a workspace means unlinking what the session wrote, which only the session's
        # own account can do (#75) -- and under a pool that is the account bound to *this*
        # workspace, not the pool's first member (#121).
        outcome = await actions.finish_terminal(
            self._adapter, self._bus, self._workspaces_for(issue), issue
        )
        self._conflict_limit_noted.pop(issue.id, None)
        self._conflict_gave_up.pop(issue.id, None)
        if outcome != "failed":
            self._ledger.forget(issue.identifier)
        if outcome in ("complete", "no_change"):
            # A no-fault close is a completion here too: the investigation is the delivered work.
            self._counters = self._counters.bump(issues_completed=1)
        elif outcome == "cancelled":
            self._counters = self._counters.bump(issues_cancelled=1)

    def _prune_accounts(self) -> None:
        """Forget the bindings of workspaces that are gone, on the sweep that removes them.

        The binding itself is never derived from the directory (#121); only its expiry is, and
        a key with a session running or a retry pending is kept whether its clone exists yet
        or not. A record that will not read is the dispatch's problem to report, not the
        sweep's: it logs and leaves the file alone.
        """
        if self._pool is None:
            return
        try:
            self._pool.prune(self._pool_keys())
        except AgentError as exc:
            self._log.warning("accounts_prune_failed", error=exc.message)

    def _workspaces_for(self, issue: Issue) -> WorkspaceManager:
        """A manager narrowed to the account this issue's workspace belongs to.

        A pool never narrows to *no* account: the removal this is built for is a delegated
        unlink of files a session owns, and the host route would skip it and leave a tree the
        worker cannot remove either (#121). An unknown binding -- pruned, or a record that
        will not read -- therefore falls back to the pool's first member, which the manager
        uses as the starting point for a removal that also runs as whoever owns what is
        actually there.

        The fallback is for the unlink, but `before_remove` rides along on the same account,
        which for an unknown binding may be one that owns nothing in the workspace. That is
        the right trade: the hook is the operator's own script and a wrong uid makes it fail,
        while no account at all makes the *removal* fail and leaves the workspace for ever.
        """
        settings = self._workflow.config
        account = session_account(settings)
        if self._pool is not None:
            try:
                account = self._pool.bound(workspace_key(issue.identifier)) or account
            except AgentError as exc:
                # The record is unreadable, so the binding is unknown; the first member still
                # delegates, and the manager finds the files' real owner from the tree.
                self._log.warning(
                    "account_lookup_failed", issue_number=issue.number, error=exc.message
                )
        return self._workspaces_factory(settings_with_run_as(settings, account))

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
            # What the issue cost, however the run ended: a release is still spend.
            self._ledger.spent(entry.identifier, turns=result.turns, cost_usd=result.cost_usd)
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
            # A run that actually succeeded is the one thing besides the escape that ends a
            # failure chain (#112). A label move is not, whoever made it.
            ledger = self._ledger.cleared(entry.identifier)
            self._schedule(
                entry.issue,
                attempt=ledger.attempt,
                kind="continuation",
                delay_ms=CONTINUATION_DELAY_MS,
                error=None,
            )
            return
        if result.error_category == "auth_failed":
            await self._auth_failed(entry, result)
            return
        if result.error_category == "run_timeout" and result.final_state is StateLabel.IN_PROGRESS:
            # The run's wall clock is spent (#110). A retry never resumes the session, so it
            # would re-read the repository from cold and spend the same clock again, up to
            # max_attempts times over: the case max_turns escapes for, and it escapes the same
            # way. The issue's ceiling is therefore agent.run_timeout_ms, not a multiple of it.
            review = self._workflow.config.github.labels.review
            reason = (
                f"Wall clock exhausted: {result.turns} turns in attempt {entry.attempt} "
                f"without reaching `{review}` ({result.error})."
            )
            await self._escape(entry, reason, result)
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
        # The chain lives on the ledger, not on the label (#112), so a move between the
        # failure and the retry cannot hand the issue a fresh `agent.max_attempts`.
        ledger = self._ledger.failed(entry.identifier)
        if ledger.failures >= agent.max_attempts:
            reason = f"{ledger.failures} consecutive worker sessions failed; last error: {error}."
            await self._escape(entry, reason, result)
            return
        self._schedule(
            entry.issue,
            attempt=ledger.attempt,
            kind="failure",
            delay_ms=backoff_ms(ledger.attempt, agent.max_retry_backoff_ms),
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
        self._record_escape(entry.identifier, outcome)
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

    def _record_escape(self, identifier: str, outcome: actions.EscapeOutcome) -> None:
        """An escape that landed ends the chain; one that could not be written has not.

        ``skipped`` ends it too. For ``blocked_escape`` that means the issue had already moved
        or closed, so there is no chain left to bound; ``budget_escape`` also answers it for an
        escalation it made earlier and has now only *returned* to ``review``, which did write
        the label -- so this is not a "nothing happened" outcome, only a "no new escalation"
        one, and the chain is over either way. The README's recovery -- fix the cause, then
        relabel -- works because of this line, and it is the *only* thing besides a run that
        succeeded which clears the chain, so a label move on its own still cannot (#112).
        """
        if outcome in ("applied", "skipped"):
            self._ledger.cleared(identifier)

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
            self._record_escape(entry.identifier, outcome)
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
        # The gate, asked before the refresh (#112). A worker that may not claim anything
        # should not spend a request finding out which issue it may not claim -- and claiming
        # is a write to a board a GitHub hold means it has just failed to read. The escape
        # above still goes first: it is the one retry whose whole job is to leave a note.
        verdict = self._admit(entry.identifier, entry.issue_id, None)
        if isinstance(verdict, Refused):
            await self._wait_or_release(entry, verdict)
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
        # Asked again with the issue in hand: the fetch was awaited, so a slot can have gone
        # while it was in flight, and the issue's own state and budget are only answerable
        # now. The attempt number comes back from the ledger, never from the label just read.
        verdict = self._admit(issue.identifier, issue.id, issue)
        if isinstance(verdict, Refused):
            await self._wait_or_release(entry, verdict, issue)
            return
        if not self._bind_account(issue)[1]:
            # After the gate, not inside it (#121): binding *allocates* an account, so it is an
            # action the admitted path takes, not a predicate a pure decision could ask. A slot
            # is free but this workspace's account is not, or the record that would name it will
            # not read. Requeued rather than released: `_dispatch` would only refuse it, and the
            # attempt count would go with it.
            blocked = self._accounts_hold()
            self._requeue(
                entry,
                kind="accounts" if blocked else "slots",
                delay_ms=settings.polling.interval_ms,
                error=blocked or "the workspace's session account is busy",
            )
            return
        await self._dispatch(issue, attempt=verdict.attempt, resume_session_id=None)

    async def _wait_or_release(
        self, entry: RetryEntry, verdict: Refused, issue: Issue | None = None
    ) -> None:
        """Do what the gate said: wait with the hold, wait for a slot, or let the entry go.

        A refusal with no ``wait`` names something waiting will not change. Shutdown is the
        exception: the entry goes back where ``fire_due_retries`` found it, since a worker on
        its way out is not a verdict about the issue. A budget refusal takes the escape, the
        same one the tick's sweep takes, so the issue is handed over rather than dropped.

        A refusal before the refresh costs the entry its poll: while the worker is at capacity
        a retry for an issue that has since closed waits for a slot rather than being finished
        here. The terminal sweep is what finishes it, on the first tick and every tenth.
        """
        if verdict.kind == "stopping":
            self._retries[entry.issue_id] = entry
            return
        if verdict.wait is None:
            self._release(entry, _RELEASE_REASONS.get(verdict.kind, verdict.kind))
            if issue is not None:
                await self._handle_refusal(issue, verdict)
            return
        self._requeue(
            entry,
            kind=verdict.wait,
            delay_ms=self._workflow.config.polling.interval_ms,
            error=verdict.reason,
        )

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
        started = self._clock()
        deadline = started + self._workflow.config.polling.interval_ms / 1000
        # The earliest a refresh may start the next tick (#110): one asked for before then
        # brings the deadline forward to it rather than returning at once.
        admissible = started + MIN_REFRESH_INTERVAL_S
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
                now = self._clock()
                if now >= admissible:
                    return
                if admissible < deadline:
                    deadline = admissible
                    self._log.debug("refresh_deferred", wait_s=round(admissible - now, 3))
                continue
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


def _seeded(entries: Mapping[str, IssueLedger] | None, max_attempts: int) -> dict[str, IssueLedger]:
    """The stored ledger as this process will trust it: every chain capped one short (#112).

    ``seeded_chain`` says why. The cumulative figures are carried whole; only the chain, which
    is the one figure that can refuse an issue without escalating it, is held back.
    """
    return {
        identifier: replace(entry, failures=seeded_chain(entry.failures, max_attempts))
        for identifier, entry in (entries or {}).items()
    }


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
