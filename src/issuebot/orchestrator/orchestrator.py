"""The orchestrator: one task owning the schedule, workers as child tasks, a queue between."""

import asyncio
import math
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn, Protocol

from issuebot.agent import (
    AgentError,
    ClaudeAuth,
    ClaudeRunner,
    ParentReviewRun,
    ParentReviewRunner,
    RunResult,
    RunTriageFn,
    TriageRunner,
    TurnEvent,
    TurnRunner,
    WorkspaceManager,
    claude_auth_status,
    describe_claude_auth,
    new_run_id,
    run_session,
    run_triage,
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
from issuebot.agent.verdict import VerdictError, parse_parent_review, parse_verdict
from issuebot.config import (
    ConfigError,
    GitHubSettings,
    Settings,
    Workflow,
    load_workflow,
    overlay_path_for,
)
from issuebot.config.workflow import parent_review_prompt_path_for, triage_prompt_path_for
from issuebot.events import AutoTriageState, EventBus, RunKind, StateChanged
from issuebot.github import (
    GhCliAdapter,
    GitHubAdapter,
    GitHubError,
    Issue,
    PageCeilingError,
    ParentSteps,
    StateLabel,
    SubIssue,
    fetch_status_summary,
    parse_status_summary,
)
from issuebot.github.ghcli import ISSUE_PAGE_SIZE, MAX_ISSUE_PAGES
from issuebot.invocation import run_hint
from issuebot.log import get_logger
from issuebot.orchestrator import actions
from issuebot.orchestrator.admission import (
    Admission,
    AdmissionRequest,
    Hold,
    IssueLedger,
    Ledger,
    OrderHold,
    RefusalKind,
    Refused,
    admit,
    seeded_chain,
)
from issuebot.orchestrator.approval import Approved, Unapproved
from issuebot.orchestrator.auto_triage import (
    CEILING_RETRY_S,
    SCAN_INTERVAL_S,
    SETTLE,
    AutoTriageRecord,
    settle,
    skip_reason,
)
from issuebot.orchestrator.parent_review import (
    ParentHandOver,
    ParentRecord,
    ReviewCounts,
    apply_parent_review,
    parent_review_due,
    steps_as_seen,
)
from issuebot.orchestrator.state import (
    CONTINUATION_DELAY_MS,
    TERMINAL_SWEEP_EVERY_TICKS,
    BlockedContext,
    ClaudeTotals,
    Counters,
    DispatchHold,
    DispatchHoldKind,
    PendingParentReview,
    PendingVerdict,
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
from issuebot.orchestrator.triage import (
    apply_verdict,
    earlier_open_step,
    is_triage_candidate,
    plural,
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
# The two triage label reads a page-ceiling failure backs off (#367), one back-off each: the
# board's triage track, every open issue carrying either triage label, and dispatch's triage
# candidates, those carrying `triage-todo`. The most issues either may return is
# `CEILING_ISSUES`, and the warning names it as the count to come under.
TRACK_READ = "triage_track"
CANDIDATES_READ = "triage_candidates"
CEILING_ISSUES = MAX_ISSUE_PAGES * ISSUE_PAGE_SIZE
# And the same for the account registry (#121): one unreadable record, however the
# complaint is worded from tick to tick.
ACCOUNTS_HOLD_KEY = "accounts"
# And the same for the usage hold: one spent window, however the sessions refused against it
# word themselves, so `since` reports how long the board has really been stopped.
USAGE_HOLD_KEY = "usage"
# The least time between the end of one tick and the start of the next when a refresh asks
# for it (#110). A NOTIFY is one row on the store away from any client with its DSN, and
# every tick polls GitHub with this worker's token, so the NOTIFY rate must not set the tick
# rate: a refresh inside the interval is admitted when the interval is up, one tick for
# however many asked, and never dropped. The web throttles its own POST to the same figure.
MIN_REFRESH_INTERVAL_S = 5.0
# How much of `gh`'s complaint the hold's reason carries. Neither `gh`'s stderr nor GitHub's
# GraphQL messages are bounded, and the reason is stored and drawn on every tick it lasts.
MAX_HOLD_ERROR_CHARS = 300
# How long a triage clone outlives the run that read it. The turn capture reads the run's log
# directory after `RunEnded`, so removing the clone at the exit would race it; ten minutes is
# far past any capture, and the startup sweep is the backstop for a worker that stops first.
TRIAGE_DISCARD_DELAY_S = 600.0
# What each kind of run's task and its dispatch log line are called.
RUN_NAMES: Mapping[RunKind, tuple[str, str]] = {
    "work": ("worker", "dispatched"),
    "triage": ("triage", "triage_dispatched"),
    "parent_review": ("parent-review", "parent_review_dispatched"),
}


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


def _github_hold_reason(error: GitHubError, status_note: str | None) -> str:
    """The GitHub hold's reason: this worker's own evidence, the status page as annotation.

    ``error`` is capped for the same reason the annotation is: it is ``gh``'s stderr or every
    GraphQL message GitHub sent, neither of them bounded, and this string is written to the
    snapshot on every tick the hold lasts and drawn on the dashboard's worker line. A rate
    limit is named as one: GitHub is answering, and the budget it refuses on is the account's,
    shared by every worker and session on its token, which "not answering" hid (2026-09-24).
    """
    detail = _clipped(str(error), MAX_HOLD_ERROR_CHARS)
    if error.category == "rate_limited":
        reason = f"GitHub is rate-limiting this worker's account: {detail}"
    else:
        reason = f"GitHub is not answering this worker: {detail}"
    # The note carries its own stamp, so it joins with a space: "githubstatus.com at 12:01Z: ...".
    return f"{reason} \u2014 githubstatus.com {status_note}" if status_note else reason


def _triage_failure(entry: RunningEntry, result: RunResult | None, error: str | None) -> str:
    """What a triage session that reached no verdict failed with, as its chain records it."""
    if entry.stop_cause == "stalled":
        return f"stalled: {entry.stop_detail}"
    if result is None:
        return error or "worker crashed"
    if result.error_category is not None:
        return f"{result.error_category}: {result.error}"
    return f"the session returned no verdict in {plural(result.turns, 'turn')}"


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
        triage_runner_factory: Callable[[Settings], TurnRunner] = TriageRunner,
        run_triage: RunTriageFn = run_triage,
        parent_review_runner_factory: Callable[[Settings], TurnRunner] = ParentReviewRunner,
        which: Callable[[str], str | None] = shutil.which,
        claude_auth: ClaudeAuthProbe = claude_auth_status,
        github_status: GitHubStatusProbe = fetch_status_summary,
        run_as_probe: RunAsProbe = probe_run_as,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
        environ: Mapping[str, str] | None = None,
        initial_rate_limits: RateLimits | None = None,
        initial_ledger: Mapping[str, IssueLedger] | None = None,
        initial_parents: Mapping[int, ParentRecord] | None = None,
        initial_auto_triage: AutoTriageRecord | None = None,
        on_snapshot: Callable[[RuntimeSnapshot], None] | None = None,
        on_issues: Callable[[Sequence[Issue]], None] | None = None,
        on_triage_track: Callable[[Sequence[Issue]], None] | None = None,
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
        self._triage_runner_factory = triage_runner_factory
        self._run_triage = run_triage
        self._parent_review_runner_factory = parent_review_runner_factory
        self._which = which
        self._claude_auth = claude_auth
        self._github_status = github_status
        self._run_as_probe = run_as_probe
        self._clock = clock
        self._now = now
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._on_snapshot = on_snapshot
        self._on_issues = on_issues
        self._on_triage_track = on_triage_track
        self._adapter = adapter_factory(workflow.config.github)
        # The manager the orchestrator itself reads workspaces through: `path_for` and
        # `read_session` ask nothing about the session's account, and everything that does --
        # the clone, the hooks, a removal -- goes through one narrowed to the workspace's own
        # bound account (#121).
        self._workspaces = workspaces_factory(workflow.config)
        self._pool = _pool_for(workflow.config)
        self._running: dict[str, RunningEntry] = {}
        self._retries: dict[str, RetryEntry] = {}
        # Issue id -> the order-hold reason last logged for it, so a step held behind an earlier
        # one says so once per change rather than on every poll (spec 2026-10-25, section 5).
        self._held: dict[str, str] = {}
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
        # The usage hold: claude refused a turn because the account's window is spent. Unlike
        # every other hold here, it names the moment it lifts, so nothing has to probe for it.
        self._usage_reason: str | None = None
        self._usage_reset_at: datetime | None = None
        # What the snapshot says of it: the reset claude reported -- never the one-interval
        # fallback, which moves every time it is taken -- and the window that came with it.
        self._usage_until: datetime | None = None
        self._usage_window: str | None = None
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
        # Issues whose approval evidence would not read, and the error it failed with
        # (GHSA-jm8h-q3j6-p8xp): the read fails closed and is retried every tick, so the
        # warning is logged when the error changes rather than every thirty seconds.
        self._approval_check_failed: dict[str, str] = {}
        # Whether the account issuebot acts as administers the repository, which is what lets
        # its own `todo` approve (GHSA-jm8h-q3j6-p8xp). Read once per process from
        # `repo_info().admin`, and lazily -- at the first approval check rather than in
        # `startup()` -- so a test that never starts the worker still exercises it.
        self._own_labels_approve: bool | None = None
        # The last verdict per issue, and what it was a verdict *about*: the poll snapshot's
        # `updated_at`, title and body. Any edit, rename or label change bumps `updatedAt`,
        # and the session gets the poll snapshot, so a verdict is good for as long as those
        # stand -- and a candidate held back by a busy pool account no longer re-reads its
        # history every tick. The text is in the key as well as the stamp because GitHub
        # stamps `updatedAt` to the second, and an edit landing in the second of the change
        # the verdict saw would otherwise leave the key where it was and reuse an approval of
        # the text before it -- the same-second race the tie rule in `approval.py` closes.
        # The approving label is in the key too: `todo` and `triage-todo` approve the same
        # text independently (spec 2026-10-25, section 3), so a verdict about one is not one
        # about the other -- and so is whether authorship was asked, since a verdict under
        # rule 1 alone is not one under both. The author's association is there because
        # authorship rests on it and losing write access does not move `updatedAt`: without
        # it a remembered `Approved` would outlive the standing it was given for.
        self._approval_verdicts: dict[
            str,
            tuple[
                tuple[str, bool, datetime, str, str | None, str | None],
                Approved | Unapproved,
            ],
        ] = {}
        self._github_note: str | None = None
        self._reported_github_block: str | None = None
        # The triage track (spec 2026-10-25-triage-design.md, section 4), all keyed by issue id
        # but the discard. Kept in memory in phase 1, so a restart resets each of them; every
        # attempt is still bounded by `agent.max_attempts` within one process.
        # Verdicts GitHub would not let the worker file, retried from memory once a tick.
        self._triage_pending: dict[str, PendingVerdict] = {}
        # The triage failure chain: failed triage runs in a row. Apart from the ledger's, which
        # counts work runs only, so a run of failed triage cannot strand the issue's work.
        self._triage_failures: dict[str, int] = {}
        # The monotonic moment before which an issue is not triaged again: a failure's backoff,
        # or one poll interval after a hold.
        self._triage_not_before: dict[str, float] = {}
        # The monotonic moment before which a triage label read is not made again, by read
        # (`TRACK_READ`, `CANDIDATES_READ`): `CEILING_RETRY_S` after one past its page ceiling,
        # which walked every page first and cannot succeed until the count comes down (#367).
        self._ceiling_not_before: dict[str, float] = {}
        # The reason of a triage hand-back GitHub refused to write. The chain it ends is over
        # for this process, so the issue's next pass writes the note again rather than starting
        # another session for an issue that has spent its attempts.
        self._triage_escapes: dict[str, str] = {}
        # Identifier to the monotonic moment its triage clone is removed (TRIAGE_DISCARD_DELAY_S).
        self._triage_discard: dict[str, float] = {}
        # The parent review (spec section 6). A split parent's record, keyed by issue number: the
        # watch is the records neither handed over nor finished. Seeded from the store by the
        # caller that has one, as the ledger is, so steps that closed while the worker was down
        # are reviewed on its first tick; without a store a restart forgets them, and such a
        # parent waits for a human's `triage-todo`.
        self._parents: dict[int, ParentRecord] = dict(initial_parents or {})
        # Auto-triage's record (spec section 2): whether it was on and the watermark behind which
        # no issue is looked at again. Seeded from the store like the parents; without one a
        # restart counts as never having run, so the watermark moves to the present.
        self._auto_triage_record: AutoTriageRecord | None = initial_auto_triage
        # The monotonic moment before which the scan does not read again: `SCAN_INTERVAL_S` after
        # the last read, failed or not, and `CEILING_RETRY_S` after one past its page ceiling.
        self._auto_triage_not_before: float = -math.inf
        # Every issue this process has auto-labelled, by number, and the tick whose scan did it.
        # The watermark is the record that outlives a restart, but a refused write leaves it
        # behind an issue labelled in the same second, and an escape may take that label off
        # before the retry reads the issue again: this is what keeps it to once (phase 4
        # ruling 2). The tick is what this tick's triage pass leaves for the next one.
        self._auto_labelled: dict[int, int] = {}
        # Parent review verdicts GitHub would not let the worker file, retried from memory as
        # triage's are, by issue id.
        self._parent_pending: dict[str, PendingParentReview] = {}
        # A parent hand-over GitHub refused to write, by issue id. The note is written again on a
        # later pass over the parent, before anything else is decided about it.
        self._parent_handovers: dict[str, ParentHandOver] = {}
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
            # A deployment that upgrades has no triage labels until someone runs `labels
            # ensure`; refusing to start over them would leave the worker (and tools/upgrade's
            # checkout) down for a feature nobody has used yet, so they only warn.
            triage_names = {name.lower() for name in settings.github.labels.warn_only()}
            absent_triage = [name for name in missing if name.lower() in triage_names]
            missing = [name for name in missing if name.lower() not in triage_names]
            if absent_triage:
                self._log.warning(
                    "triage_labels_missing",
                    labels=absent_triage,
                    hint=run_hint("labels ensure"),
                )
            if missing:
                names = ", ".join(missing)
                problems.append(f"labels missing: {names}; {run_hint('labels ensure')}")
        # The boundary the deployment asked for has to exist before an issue is claimed
        # (#75): a delegation that does not work would fail every run instead.
        if settings.agent.run_as:
            unusable = await asyncio.to_thread(
                self._run_as_probe, settings.agent.run_as, self._environ
            )
            problems.extend(f"agent.run_as: {error}" for error in unusable)
        # No account a session runs as is one anybody logs into, whether the deployment runs a
        # pool or a single account (#121, #142): a pool shares no login between its members by
        # construction, and the lone `agent` holds none either now that the container's
        # interactive login is gone. So the credential has to be one `claude` needs no file
        # for, and the complaint below asks that of any non-empty `agent.run_as`. Refusing
        # here rather than per run: every session would fail to authenticate, which is #17's
        # rule for a definite logged-out.
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
        # And every triage clone on the disk is a previous process's: this one has run nothing,
        # and a clone is only ever read by the run that made it (spec 2026-10-25, section 4).
        for key in self._workspaces.triage_keys():
            try:
                await self._workspaces.remove_triage_key(key)
            except AgentError as exc:
                self._log.warning(
                    "triage_workspace_sweep_failed", workspace_key=key, error=exc.message
                )
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

    async def _note_fetch_failure(self, error: GitHubError) -> None:
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
            "error": str(error),
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

    def _hold_usage(self, error: str, reset_at: datetime | None, window: str | None = None) -> None:
        """Stop claiming issues until the account's refused window reopens (#173's incident).

        The only hold here that needs no probe to lift: claude says when the window reopens,
        so the worker waits exactly that long. A refusal that did not say falls back to one
        poll interval, which is a wait rather than the 20 s and 40 s a failure chain would
        spend before escalating.

        The reason is re-worded whenever a later run hits the same wall, and the reset is
        moved *out* only, never in: two sessions can be refused against the same window and
        report it seconds apart, and the later reading is the one to wait for.

        A refusal whose ``rate_limit_event`` names no ``resetsAt`` is dated by the runner at the
        moment the line was read (``parse_usage_limit``), so its ``until`` is already past and the
        hub's watcher skips it; only a text-only refusal arrives here with no reset at all.

        ``until`` and ``window`` in the snapshot take claude's own reset only, and move out with
        it, so the two always describe the same refusal.
        """
        fallback = self._now() + timedelta(milliseconds=self._workflow.config.polling.interval_ms)
        due = reset_at or fallback
        if self._usage_reset_at is None or due > self._usage_reset_at:
            self._usage_reset_at = due
        if reset_at is not None and (self._usage_until is None or reset_at > self._usage_until):
            self._usage_until = reset_at
            self._usage_window = window
        self._usage_reason = f"claude usage limit reached: {error}"

    def _release_usage_hold(self) -> None:
        self._usage_reason = None
        self._usage_reset_at = None
        self._usage_until = None
        self._usage_window = None

    def _settle_usage_hold(self) -> None:
        """Lift the usage hold once the window it named has reopened.

        Called from ``tick`` before the hold is composed, so the tick that reaches the reset
        claims again rather than waiting a further interval to notice.
        """
        if self._usage_reset_at is None:
            return
        if self._now() < self._usage_reset_at:
            return
        self._log.info(
            "dispatch_usage_recovered",
            reset_at=self._usage_reset_at.isoformat(),
            error=self._usage_reason,
        )
        self._release_usage_hold()

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
        if self._usage_reason is not None:
            # Below `auth` and above `accounts` deliberately: a credential that will not
            # authenticate is a fault an operator fixes, where this one lifts on its own and
            # says when. It is keyed on the constant so that a second session reporting the
            # same wall in different words keeps the hold's `since`.
            return Hold(
                "usage",
                self._usage_reason,
                key=USAGE_HOLD_KEY,
                until=self._usage_until,
                window=self._usage_window,
            )
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
        self,
        kind: DispatchHoldKind,
        reason: str,
        *,
        key: str | None = None,
        until: datetime | None = None,
        window: str | None = None,
    ) -> None:
        """Carry why dispatch is held into the snapshot; a hold that lasts keeps its ``since``.

        ``key`` is what makes two holds the same one when the wording is not: an unreadable
        ``claude`` can garble its output differently on every probe, and the operator should
        still see how long the hold has really lasted. ``until`` and ``window`` follow the
        newest reason, as the reason itself does.
        """
        identity = (kind, reason if key is None else key)
        if self._dispatch_hold is not None and identity == self._hold_identity:
            self._dispatch_hold = replace(
                self._dispatch_hold, reason=reason, until=until, window=window
            )
            return
        self._hold_identity = identity
        self._dispatch_hold = DispatchHold(
            kind=kind, reason=reason, since=self._now(), until=until, window=window
        )

    def _release_snapshot_hold(self) -> None:
        self._dispatch_hold = None
        self._hold_identity = None

    # --- tick -------------------------------------------------------------------------

    async def tick(self) -> None:
        """Symphony §8.1: reconcile, reload, preflight, fetch, dispatch, snapshot."""
        await self.reconcile()
        self._reload_workflow()
        # Every tick, holds or not: a reload can switch auto-triage on or off (phase 4 ruling 3).
        self._settle_auto_triage()
        await self._settle_run_as()
        # Before any hold is decided, and not inside `_dispatch_candidates`, which a preflight
        # or auth hold skips: a reason `fire_due_retries` quotes has to be about this tick.
        self._read_accounts()
        # Before the hold is composed, so the tick that reaches the reset claims on that tick
        # rather than waiting a further interval to notice the window reopened.
        self._settle_usage_hold()
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
            if await self._refresh_auth_hold() or self._usage_reason is not None:
                # The fetch still works, so the board stays fresh while nothing is claimed
                # (#29). The usage hold skips dispatch for the auth hold's reason rather than
                # the GitHub hold's: the board can be read perfectly well, and it is the
                # claiming that has nowhere to go until the window reopens. A verdict owed is
                # still filed (#308): that needs only GitHub, the session is already paid for,
                # and a usage window can last hours.
                await self._retry_pending_verdicts()
                await self._poll_issues()
            else:
                # Triage first (spec 2026-10-25-triage-design.md, section 4): a triage run is
                # short and what it unblocks is a human, so it takes a free slot before work.
                # A parent review next (section 6): it is short too, and what it unblocks is the
                # parent's next batch. Auto-triage's scan before both, though an issue it labels
                # is left by this tick's triage pass and started on the next.
                await self._scan_for_auto_triage()
                dispatched = await self._dispatch_triage()
                dispatched += await self._dispatch_parent_reviews()
                dispatched += await self._dispatch_candidates()
        # Whatever the holds: removing a clone asks nothing of GitHub or of claude.
        await self._discard_triage_workspaces()
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
            self._hold_snapshot(
                hold.kind, hold.reason, key=hold.key, until=hold.until, window=hold.window
            )

    def _slots(self) -> int:
        return max(self._workflow.config.agent.max_concurrent_agents - len(self._running), 0)

    def _cannot_dispatch(self) -> bool:
        """Whether this tick can admit nothing: no slot free, the worker stopping, or a hold."""
        return self._slots() == 0 or self._stopping or self._current_hold() is not None

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
        triage_path = triage_prompt_path_for(path)
        try:
            triage: os.stat_result | None = triage_path.stat()
        except FileNotFoundError:
            # The normal case: the shipped triage prompt is in force.
            triage = None
        except OSError as exc:
            self._report_reload_failure(f"{triage_path.name} unreadable: {exc}")
            return
        review_path = parent_review_prompt_path_for(path)
        try:
            review: os.stat_result | None = review_path.stat()
        except FileNotFoundError:
            # The normal case: the shipped parent-review prompt is in force.
            review = None
        except OSError as exc:
            self._report_reload_failure(f"{review_path.name} unreadable: {exc}")
            return
        loaded = (
            workflow.source_identity,
            workflow.overlay_identity if workflow.overlay_path is not None else None,
            workflow.triage_identity,
            workflow.parent_review_identity,
        )
        found = (
            _identity(source),
            _identity(overlay) if overlay is not None else None,
            _identity(triage) if triage is not None else None,
            _identity(review) if review is not None else None,
        )
        if loaded == found:
            # Nothing to load: no identity has moved and no file has come or gone. The one
            # thing left worth saying is that this deployment may not be in a position to
            # tell (#46), about any of the files. On this branch the files on
            # disk are exactly what was loaded, so the error state is exactly the complaint:
            # an error an overlay caused must clear once the overlay is gone.
            complaint = _pinned_mount_complaint(path, source)
            if complaint is None and overlay is not None:
                complaint = _pinned_mount_complaint(overlay_path, overlay)
            if complaint is None and triage is not None:
                complaint = _pinned_mount_complaint(triage_path, triage)
            if complaint is None and review is not None:
                complaint = _pinned_mount_complaint(review_path, review)
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
            await self._note_fetch_failure(exc)
            return None
        self._note_fetch_success()
        self._report_issues(issues)
        await self._observe_triage_track()
        await self._bounce_conflicts(issues)
        return issues

    async def _observe_triage_track(self) -> None:
        """Hand the store every open issue on the triage track; a failed read is a warning.

        The board poll reads by state label, so it never sees an issue waiting in Triaged; this
        is the one extra read that does (triage spec section 7, rulings 4 and 5), and its answer
        is the complete set, so the store reconciles against it. Only a worker with a store makes
        it. A failure is deliberately outside the GitHub hold (#88): it neither counts towards
        the hold nor resets the count, as the triage candidates' read does not, because the
        board is not what dispatch depends on and a failed read reconciles nothing. Past its page
        ceiling it waits ``CEILING_RETRY_S`` (#367), and the columns keep the last good read
        meanwhile; the warning, once per back-off, is what reports it.
        """
        if self._on_triage_track is None:
            return
        if self._ceiling_backed_off(TRACK_READ):
            return
        labels = self._workflow.config.github.labels
        try:
            issues = await self._adapter.fetch_issues_with_any_label(
                (labels.triage_todo, labels.triage_complete)
            )
        except PageCeilingError as exc:
            self._back_off_ceiling(
                TRACK_READ,
                "triage_track_fetch_failed",
                exc,
                remedy=(
                    f"bring the open issues carrying {labels.triage_todo} or "
                    f"{labels.triage_complete} to {CEILING_ISSUES} or fewer -- close what is "
                    f"finished, or take {labels.triage_complete} off issues long past triage "
                    "(not a split parent with steps still open); "
                    "the Triage and Triaged columns show the last good read until then"
                ),
            )
            return
        except GitHubError as exc:
            self._log.warning("triage_track_fetch_failed", error=str(exc))
            return
        try:
            self._on_triage_track(issues)
        except Exception:
            self._log.exception("triage_track_consumer_failed", count=len(issues))

    def _ceiling_backed_off(self, read: str) -> bool:
        """Whether ``read`` is still waiting out a page-ceiling failure's ``CEILING_RETRY_S``."""
        return self._clock() < self._ceiling_not_before.get(read, -math.inf)

    def _back_off_ceiling(
        self, read: str, event: str, exc: PageCeilingError, *, remedy: str
    ) -> None:
        """Hold ``read`` off for ``CEILING_RETRY_S`` after it stopped at its page ceiling (#367).

        Every page up to the ceiling was fetched before the error, some thirty GraphQL points of
        the hourly budget every deployment on the account shares, and the next attempt would
        fetch them all again until the count comes down: once a poll, that is most of the
        budget. The warning is logged here and so once per back-off, since nothing reads in
        between. Only the ceiling waits: any other failure is usually a moment's, and the next
        poll tries again.
        """
        self._ceiling_not_before[read] = self._clock() + CEILING_RETRY_S
        self._log.warning(
            event,
            error=str(exc),
            retry_in_s=CEILING_RETRY_S,
            retry_at=(self._now() + timedelta(seconds=CEILING_RETRY_S)).isoformat(),
            remedy=remedy,
        )

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
            if self._ledger.get(issue.identifier).escaped:
                # An escape put this issue in `review` and nothing has run since, so a bounce
                # would undo the one decision the escape made -- and spend a bounce that is
                # meant for "issuebot resolved the conflict and the branch moved again". On
                # 2026-09-22 this fired eight seconds after a blocked escape and took #173's
                # last bounce for a run that never started. Cleared when the issue is next
                # dispatched, which is what a human relabelling it produces.
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
        bounce is on, or the triage track is read for the store: a conflict is about the board,
        not about dispatch.
        """
        if (
            self._on_issues is None
            and self._on_triage_track is None
            and self._conflict_limit() <= 0
        ):
            self._note_fetch_skipped()
            return
        await self._fetch_issues()

    def _admit(
        self,
        identifier: str,
        issue_id: str,
        issue: Issue | None,
        *,
        kind: RunKind = "work",
        order: OrderHold | None = None,
    ) -> Admission:
        """Ask the one gate (#112). Every precondition is its, and it reads the live holds.

        Live rather than ``self._dispatch_hold``, which only settles at the end of a tick: a
        GitHub hold that this tick's successful fetch has just lifted must not refuse the
        claim that fetch produced. A triage run asks the same gate, so it shares the slots,
        the holds and the issue's spend with work (spec 2026-10-25, section 4).
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
                kind=kind,
                order=order,
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
        if verdict.kind == "order":
            # A step waiting its turn in a split (spec 2026-10-25, section 5): nothing is
            # written on the issue and no state changes, so it stays a `todo` candidate and the
            # next poll asks again. The log says so once per change of reason, not per poll.
            if self._held.get(issue.id) != verdict.reason:
                self._held[issue.id] = verdict.reason
                self._log.info(
                    "dispatch_held",
                    issue_number=issue.number,
                    issue_identifier=issue.identifier,
                    reason=verdict.reason,
                )
            return
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
        # A hold is remembered only while its issue is still a `todo` one this poll.
        todo_ids = {issue.id for issue in issues if issue.state is StateLabel.TODO}
        self._held = {id_: reason for id_, reason in self._held.items() if id_ in todo_ids}
        steps = await self._read_parent_steps(issues)
        for issue in sort_candidates(issues):
            verdict = self._admit(
                issue.identifier, issue.id, issue, order=self._order_hold(issue, steps)
            )
            if isinstance(verdict, Refused):
                if verdict.kind in ("slots", "hold", "stopping"):
                    # Nothing about this issue; the next candidate would be told the same.
                    break
                await self._handle_refusal(issue, verdict)
                continue
            self._held.pop(issue.id, None)
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

    def _order_applies(self, issue: Issue) -> bool:
        """Whether the order hold applies to ``issue`` at all: a `todo` sub-issue that is not an
        operator step. An operator step is never held; its `todo` comes off at once instead."""
        return (
            issue.state is StateLabel.TODO
            and issue.parent is not None
            and self._workflow.config.github.labels.operator_step.lower() not in issue.labels
        )

    async def _read_parent_steps(self, issues: Sequence[Issue]) -> dict[int, ParentSteps] | None:
        """One read of every parent the poll's `todo` sub-issues point at, or ``None`` if it failed.

        Nothing is read when no candidate is a sub-issue. A failed read holds those candidates
        for this poll only (they are given an unreadable ``OrderHold``): running a step early is
        the failure the hold exists to stop, and waiting one poll costs one interval. It is
        logged, and does not feed the GitHub hold (spec 2026-10-25, section 5).
        """
        if self._cannot_dispatch():
            # The account's GraphQL budget is shared, and `admit` ranks stopping, hold and slots
            # above `order`: the sweep stops at the first such refusal, so nothing is admitted
            # this tick and an empty mapping cannot let a held step through (spec 2026-10-25,
            # section 9). The hold is read here, after the fetch that may have lifted it.
            return {}
        parents = sorted(
            {
                issue.parent
                for issue in issues
                if issue.parent is not None and self._order_applies(issue)
            }
        )
        if not parents:
            return {}
        try:
            return await self._adapter.parent_steps(parents)
        except GitHubError as exc:
            self._log.warning("parent_steps_failed", error=str(exc), parents=parents)
            return None

    def _order_hold(
        self, issue: Issue, steps: Mapping[int, ParentSteps] | None
    ) -> OrderHold | None:
        """The hold ``issue`` is under, if any. ``steps`` is ``None`` when the read failed."""
        parent = issue.parent
        if parent is None or not self._order_applies(issue):
            return None
        if steps is None:
            return OrderHold(parent, None)
        found = steps.get(parent)
        if found is None:
            return None
        earlier = earlier_open_step(issue, found, self._workflow.config.github.labels)
        return None if earlier is None else OrderHold(parent, earlier)

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

    async def _assess_approval(
        self, issue: Issue, *, label: str | None = None, authorship: bool = False
    ) -> Approved | Unapproved | None:
        """Is the issue's text the text a human approved? ``None`` when GitHub would not say.

        ``label`` is the one that approves: ``todo`` unless the caller names another, which
        the triage dispatch does with ``triage-todo`` (spec 2026-10-25, section 3).
        ``authorship`` adds that section's second rule, for a ``triage-todo`` issuebot applied
        itself: the triage and parent-review dispatch ask for it, and the work dispatch never
        does, so it cannot approve a session that holds a shell or a GitHub credential.

        Read at dispatch rather than at poll: one issue is dispatched at a time, where every
        poll reads the whole board. A verdict is remembered for as long as the issue is
        unchanged (`_approval_verdicts`). A failed read fails closed -- the issue waits for the
        next tick -- and is logged once per error rather than per tick.

        A history past the page ceiling is not a failed read but an answer: nobody can say
        what was approved, and the next tick would read the same ten pages to say so again.
        It is a verdict, so the escape takes the label off and the spin stops.
        """
        label = label or self._adapter.labels.todo
        key = (
            label,
            authorship,
            issue.updated_at,
            issue.title,
            issue.body,
            issue.author_association,
        )
        remembered = self._approval_verdicts.get(issue.id)
        if remembered is not None and remembered[0] == key:
            return remembered[1]
        try:
            if self._own_labels_approve is None:
                self._own_labels_approve = (await self._adapter.repo_info()).admin
            if authorship:
                verdict = await actions.assess_triage_issue(
                    self._adapter,
                    issue,
                    triage_label=label,
                    own_labels_approve=self._own_labels_approve,
                )
            else:
                verdict = await actions.assess_issue(
                    self._adapter,
                    issue.number,
                    todo_label=label,
                    own_labels_approve=self._own_labels_approve,
                )
        except PageCeilingError as exc:
            # The message names the ceiling; the orchestrator does not import the adapter's
            # constants to say it again.
            verdict = Unapproved(
                reason=f"{exc.message}: issuebot cannot tell what was approved",
                approval=None,
                edit=None,
            )
        except GitHubError as exc:
            if self._approval_check_failed.get(issue.id) != str(exc):
                self._approval_check_failed[issue.id] = str(exc)
                self._log.warning(
                    "approval_check_failed",
                    issue_number=issue.number,
                    issue_identifier=issue.identifier,
                    error=str(exc),
                    category=exc.category,
                )
            return None
        self._approval_check_failed.pop(issue.id, None)
        self._approval_verdicts[issue.id] = (key, verdict)
        return verdict

    async def _dispatch(self, issue: Issue, *, attempt: int, resume_session_id: str | None) -> bool:
        # Before anything about the issue is read (spec 2026-10-25, section 1): a step triage
        # filed for the operator is never a work run, so whichever state label brought it here
        # -- `todo`, `rework`, or `in-progress` on a resume or a retry -- comes off again with a
        # note, the way an unapproved issue is handed back (#346). `Issue.labels` is lower-cased.
        if self._workflow.config.github.labels.operator_step.lower() in issue.labels:
            outcome = await actions.operator_step_escape(
                self._adapter, self._bus, issue, now=self._now()
            )
            if outcome == "applied":
                self._counters = self._counters.bump(blocked=1)
            self._record_escape(issue.identifier, outcome)
            return False
        # Before the account and before the claim (GHSA-jm8h-q3j6-p8xp): an issue whose text
        # is no longer the text a human approved is handed back, and never holds a slot.
        verdict = await self._assess_approval(issue)
        if verdict is None:
            return False
        if isinstance(verdict, Unapproved):
            outcome = await actions.unapproved_escape(
                self._adapter, self._bus, issue, verdict, now=self._now()
            )
            if outcome == "applied":
                self._counters = self._counters.bump(blocked=1)
                # Spent: the issue has left the board, and what brings it back is a new label
                # event to be read afresh. A failed escape keeps it, so the retry reads nothing.
                self._approval_verdicts.pop(issue.id, None)
            self._record_escape(issue.identifier, outcome)
            return False
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
        self._start_run(
            issue,
            lambda entry: self._worker(entry, workflow, resume_session_id),
            kind="work",
            attempt=attempt,
            account=account,
            rework=rework,
            resumed=resume_session_id is not None,
        )
        return True

    def _start_run(
        self,
        issue: Issue,
        worker: Callable[[RunningEntry], Coroutine[Any, Any, RunResult]],
        *,
        kind: RunKind,
        attempt: int,
        account: str | None,
        rework: bool = False,
        resumed: bool = False,
        steps_seen: tuple[int, ...] = (),
    ) -> None:
        """Start one run, of any kind, and everything a run's start owes: the one place.

        Every kind shares a slot, an account and the issue's ledger (spec 2026-10-25, sections 4
        and 6), so the bookkeeping is one block rather than one per kind: a step added to a run's
        start reaches them all. ``worker`` builds the run's coroutine from its entry; beyond that
        the kinds differ only in what the task and the log line are called, and in the steps a
        parent review was started on (``steps_seen``).

        A pending retry is dropped for either kind. Admission's ``busy`` refuses a claim for an
        issue with one, so today there is never one to drop; the line is what keeps a retry
        from firing beside a run should that ever stop being true.
        """
        entry = RunningEntry(
            issue=issue,
            attempt=attempt,
            rework=rework,
            resumed=resumed,
            run_id=new_run_id(self._now()),
            started_mono=self._clock(),
            started_at=self._now(),
            cancel=asyncio.Event(),
            account=account,
            kind=kind,
            steps_seen=steps_seen,
        )
        role, event = RUN_NAMES[kind]
        entry.task = asyncio.create_task(worker(entry), name=f"issuebot-{role}-{issue.number}")
        entry.task.add_done_callback(
            lambda _task, issue_id=issue.id: self._queue.put_nowait(_WorkerExited(issue_id))
        )
        self._running[issue.id] = entry
        self._retries.pop(issue.id, None)
        self._counters = self._counters.bump(runs_started=1)
        ledger = self._ledger.dispatched(issue.identifier, at=self._now())
        self._log.info(
            event,
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            attempt=attempt,
            rework=rework,
            resumed=resumed,
            run_id=entry.run_id,
            account=account,
            slots_left=self._slots(),
            issue_runs=ledger.runs,
            issue_cost_usd=ledger.cost_usd,
        )

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

    # --- auto-triage (spec 2026-10-25-triage-design.md, section 2) -------------------------

    def _settle_auto_triage(self) -> None:
        """Read ``triage.auto`` against the record, on every tick rather than only at start.

        ``WORKFLOW.md`` reloads live, so a reload switching it on moves the watermark to the
        present as a start with it on does, and one switching it off is recorded: the issues
        opened during the spell off are then behind the watermark the next switch on sets, and
        never auto-triaged (spec 2026-10-25, section 2; phase 4 ruling 3).
        """
        record = settle(
            self._auto_triage_record, auto=self._workflow.config.triage.auto, now=self._now()
        )
        if record is None:
            return
        self._record_auto_triage(record)
        if record.auto:
            # A new watermark, so any wait the old one earned is over: a scan past its page
            # ceiling names switching off and on as the way out, and it takes effect at once.
            self._auto_triage_not_before = -math.inf
            self._log.info("auto_triage_on", watermark=record.watermark.isoformat())
        else:
            self._log.info("auto_triage_off")

    def _record_auto_triage(self, record: AutoTriageRecord) -> None:
        """Keep ``record`` and publish it whole. The one place the record changes, so the latest
        ``auto_triage_state`` the store holds is the one this worker acts on, and a restart
        reads back exactly that (phase 4 ruling 1)."""
        self._auto_triage_record = record
        self._bus.publish(AutoTriageState(auto=record.auto, watermark=record.watermark))

    async def _scan_for_auto_triage(self) -> int:
        """Label each new maintainer issue ``triage-todo``, oldest first; the number labelled.

        Every open issue opened after the watermark is decided once -- labelled, or passed over
        for its ``skip_reason`` -- and the watermark moves past it, so an issue behind it is
        never looked at again: a label a human or an escape takes off is not put back (spec
        2026-10-25, section 2; phase 4 ruling 2). An issue opened within ``SETTLE`` of the scan
        waits for the next one, the watermark behind it, because GitHub may not list it yet and
        a newer issue it did list must not carry the watermark past both (ruling 6). A refused
        label write ends the scan with the watermark strictly before the refused issue, which
        the next scan decides again; an issue labelled before the refusal is remembered, so it
        is not labelled twice whatever becomes of its label (ruling 15).

        The scan fills no slot of its own, yet it is made only on a tick that could dispatch --
        a slot free, not stopping, no hold -- and at most every ``SCAN_INTERVAL_S`` (ruling 5).
        A label it adds would wait for the slot its triage run needs anyway, so labelling
        sooner gains nothing, and the read spends the account's hourly GraphQL budget, which
        every deployment shares (section 9). A failed read is logged and waits out the
        interval -- an hour, ``CEILING_RETRY_S``, for one past its page ceiling, which would walk
        every page again; like the triage read, it does not feed the GitHub hold, which is the
        board's.
        """
        record = self._auto_triage_record
        if record is None or not record.auto:
            return 0
        if self._cannot_dispatch():
            return 0
        if self._clock() < self._auto_triage_not_before:
            return 0
        self._auto_triage_not_before = self._clock() + SCAN_INTERVAL_S
        try:
            issues = await self._adapter.fetch_issues_opened_since(record.watermark)
        except PageCeilingError as exc:
            self._auto_triage_not_before = self._clock() + CEILING_RETRY_S
            self._log.warning(
                "auto_triage_scan_failed",
                error=str(exc),
                retry_in_s=CEILING_RETRY_S,
                # At least one poll: the setting is read once a tick, so an off and on that no
                # tick sees in between moves nothing.
                remedy=(
                    "set triage.auto off for at least one poll, then on again: that moves the "
                    "watermark to the present, and leaves the issues opened before it to a person"
                ),
            )
            return 0
        except GitHubError as exc:
            self._log.warning("auto_triage_scan_failed", error=str(exc))
            return 0
        labels = self._workflow.config.github.labels
        settled = self._now() - SETTLE
        # `watermark` is past every issue decided so far; `behind` is past every one opened
        # strictly before the issue in hand, which is where a refused write leaves it.
        watermark = behind = record.watermark
        labelled = 0
        for issue in issues:
            if issue.created_at > settled:
                # The read is oldest first, so every issue after this one is newer still.
                break
            if issue.created_at > watermark:
                behind = watermark
            if issue.number in self._auto_labelled:
                reason: str | None = "already auto-labelled"
            else:
                reason = skip_reason(issue, labels)
            if reason is not None:
                self._log.info(
                    "auto_triage_passed_over",
                    issue_number=issue.number,
                    issue_identifier=issue.identifier,
                    reason=reason,
                )
            else:
                try:
                    await self._adapter.edit_labels(issue.number, add=[labels.triage_todo])
                except GitHubError as exc:
                    self._log.warning(
                        "auto_triage_label_failed",
                        issue_number=issue.number,
                        issue_identifier=issue.identifier,
                        error=str(exc),
                    )
                    # The next read is strictly after the watermark, so it is left strictly
                    # before this issue's second, even past one decided in that same second:
                    # one labelled there is in `_auto_labelled`, so the next scan passes it over
                    # rather than labelling it twice, whatever has become of its label.
                    watermark = behind
                    break
                self._auto_labelled[issue.number] = self._tick_count
                # A move on the triage track (phase 4 ruling 13): kept as history, and Slack's
                # `state_changed` says it in one line.
                self._bus.publish(
                    StateChanged(
                        issue_number=issue.number,
                        issue_identifier=issue.identifier,
                        from_label=None,
                        to_label=labels.triage_todo,
                        actor="issuebot",
                        note="auto-triage",
                    )
                )
                self._log.info(
                    "auto_triage_labelled",
                    issue_number=issue.number,
                    issue_identifier=issue.identifier,
                )
                labelled += 1
            watermark = issue.created_at
        if watermark != record.watermark:
            self._record_auto_triage(replace(record, watermark=watermark))
        return labelled

    # --- triage (spec 2026-10-25-triage-design.md) ---------------------------------------

    async def _dispatch_triage(self) -> int:
        """File what is owed, then start a triage run for each candidate the gate admits, or a
        parent review for one that has sub-issues (section 1).

        The candidates are a read of their own -- one label, open issues only -- rather than a
        field of the board's poll, which fetches by state label and so never sees an issue on
        the triage track. A failure of that read is not counted towards the GitHub hold (#88):
        the hold is the board's, and the board's own poll follows in this tick to answer it. One
        past its page ceiling waits ``CEILING_RETRY_S`` before the next (#367).
        """
        await self._retry_pending_verdicts()
        if self._slots() == 0 and not self._triage_escapes and not self._parent_handovers:
            # Section 9: every deployment shares the account's hourly GraphQL budget, and with
            # no slot to fill the gate would refuse the first candidate on `slots` anyway. An
            # escape or hand-over note still owed needs no slot, so it keeps the read.
            return 0
        if self._ceiling_backed_off(CANDIDATES_READ):
            return 0
        labels = self._workflow.config.github.labels
        try:
            issues = await self._adapter.fetch_issues_with_label(labels.triage_todo)
        except PageCeilingError as exc:
            self._back_off_ceiling(
                CANDIDATES_READ,
                "triage_fetch_failed",
                exc,
                remedy=(
                    f"bring the open issues carrying {labels.triage_todo} to {CEILING_ISSUES} "
                    f"or fewer by taking {labels.triage_todo} off those that need not be "
                    "triaged; no triage run starts until then"
                ),
            )
            return 0
        except GitHubError as exc:
            self._log.warning("triage_fetch_failed", error=str(exc))
            return 0
        dispatched = 0
        for issue in issues:
            if not is_triage_candidate(issue, labels):
                continue
            if self._auto_labelled.get(issue.number) == self._tick_count:
                # This tick's scan labelled it moments ago. The approval reads the label's event
                # back, and GitHub may not list it yet: refused for that, the issue would be
                # handed back, and behind the watermark it is never auto-triaged again. So it
                # waits for the next tick.
                continue
            # A parent review's verdict owed is as busy as a triage one: a review a human's
            # `triage-todo` started leaves that label on until its filing lands.
            owed = issue.id in self._triage_pending or issue.id in self._parent_pending
            if issue.id in self._running or owed:
                continue
            note = self._parent_handovers.get(issue.id)
            if note is not None:
                # A parent hand-over GitHub refused, written before anything else is decided
                # about the candidate, as the watch writes one on a parent it is watching.
                await self._hand_over_parent(
                    issue, title=note.title, reason=note.reason, way_back=note.way_back
                )
                continue
            if self._clock() < self._triage_not_before.get(issue.id, -math.inf):
                continue
            reason = self._triage_escapes.get(issue.id)
            if reason is not None:
                await self._triage_give_up(issue, reason)
                continue
            verdict = self._admit(issue.identifier, issue.id, issue, kind="triage")
            if isinstance(verdict, Refused):
                if verdict.kind in ("slots", "hold", "stopping"):
                    # Nothing about this issue; the next candidate would be told the same.
                    break
                if verdict.kind == "spend" and issue.sub_issue_count > 0:
                    # A split parent's review, refused as the watch's would be.
                    await self._hand_over_parent_over_budget(issue, verdict.reason)
                elif verdict.kind == "spend":
                    # Handed back rather than refused on every tick, as a work issue is; the
                    # escape takes `triage-todo` off, which is what stops the refusal repeating.
                    await self._triage_hand_back(
                        issue,
                        title="triage budget limit",
                        reason=verdict.reason,
                        way_back=(
                            "Raise `agent.max_issue_cost_usd`, *then* add "
                            f"`{labels.triage_todo}` again: what that ceiling counts is what "
                            "the issue has already cost, and that never resets."
                        ),
                    )
                continue
            attempt = self._triage_failures.get(issue.id, 0) + 1
            if await self._dispatch_triage_one(issue, attempt=attempt):
                dispatched += 1
        return dispatched

    async def _retry_pending_verdicts(self) -> None:
        """Try once more to file each verdict GitHub refused, once per entry per tick.

        ``apply_verdict`` is idempotent step by step, so a retry files only what is missing and
        posts the comment once. Each retry reads the issue first, as the first filing does, and
        drops a verdict that is owed no longer without a write (``_verdict_owed``).
        ``agent.max_attempts`` failed retries hand the issue back; an entry is dropped only once
        that note has landed, so a GitHub that refuses the note as well as the filing leaves the
        verdict to be tried again rather than forgotten. A parent review's verdict is retried the
        same way, through ``apply_parent_review`` and ``_parent_review_owed``, and its hand-back
        is a parent hand-over.
        """
        agent = self._workflow.config.agent
        labels = self._workflow.config.github.labels
        for issue_id in list(self._triage_pending):
            failure = await self._file_verdict(self._triage_pending[issue_id])
            if failure is None:
                continue
            # The entry the failed try stored, with its swap if that landed; counted here.
            pending = self._triage_pending[issue_id]
            pending = replace(pending, tries=pending.tries + 1)
            self._triage_pending[issue_id] = pending
            if pending.tries < agent.max_attempts:
                continue
            error = _clipped(str(failure), MAX_HOLD_ERROR_CHARS)
            outcome = await self._triage_hand_back(
                pending.issue,
                title="triage not filed",
                reason=f"issuebot could not file the triage verdict on GitHub: {error}",
                way_back=(
                    "Unlink any sub-issues it filed from this issue first, then add "
                    f"`{labels.triage_todo}` again to triage it afresh."
                ),
            )
            if outcome != "failed":
                # The note ends the chain (`_triage_hand_back`), and the verdict with it.
                del self._triage_pending[issue_id]
                self._schedule_triage_discard(pending.issue.identifier)
        for issue_id in list(self._parent_pending):
            failure = await self._file_parent_review(self._parent_pending[issue_id])
            if failure is None:
                continue
            review = self._parent_pending[issue_id]
            review = replace(review, tries=review.tries + 1)
            self._parent_pending[issue_id] = review
            if review.tries < agent.max_attempts:
                continue
            error = _clipped(str(failure), MAX_HOLD_ERROR_CHARS)
            outcome = await self._hand_over_parent(
                review.issue,
                title="parent review not filed",
                reason=f"issuebot could not file the parent review's verdict on GitHub: {error}",
                way_back=(
                    "Unlink any sub-issues this review filed from this issue, then add "
                    f"`{labels.triage_todo}` again."
                ),
            )
            if outcome != "failed":
                del self._parent_pending[issue_id]
                self._schedule_triage_discard(review.issue.identifier)

    async def _file_verdict(self, pending: PendingVerdict) -> GitHubError | None:
        """File one verdict; ``None`` once it landed or is owed no longer, else GitHub's error.

        Filed on a fresh read of the issue (section 1, ``_verdict_owed``), so a change the
        reconcile has not seen yet still discards it. Success ends the issue's triage chain and
        schedules its clone's removal; a verdict owed no longer schedules the removal too, and
        no failure is counted. A failure, the read's included, keeps the verdict for a retry --
        stored here, with whether its label swap landed -- and the caller counts it.
        """
        issue = pending.issue
        swapped = pending.swapped

        def note_swap() -> None:
            nonlocal swapped
            swapped = True

        try:
            current = await self._verdict_owed(pending)
            if current is not None:
                await apply_verdict(
                    self._adapter,
                    self._bus,
                    current,
                    pending.verdict,
                    run_id=pending.run_id,
                    attempt=pending.attempt,
                    turns=pending.turns,
                    cost_usd=pending.cost_usd,
                    now=self._now(),
                    on_swapped=note_swap,
                    swapped=pending.swapped,
                )
        except GitHubError as exc:
            self._log.warning(
                "triage_file_failed",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                run_id=pending.run_id,
                tries=pending.tries,
                error=str(exc),
                category=exc.category,
            )
            # The session's work is kept: the next tick files it from memory, never by running
            # the session again.
            self._triage_pending[issue.id] = replace(pending, swapped=swapped)
            return exc
        self._triage_pending.pop(issue.id, None)
        self._schedule_triage_discard(issue.identifier)
        if current is None:
            return None
        self._forget_triage_chain(issue.id)
        if pending.verdict.verdict == "split":
            # Section 6: a parent whose triage filed its steps is watched from here on.
            self._record_parent(issue.number, ParentRecord(identifier=issue.identifier))
        elif (record := self._parents.get(issue.number)) is not None and not record.finished:
            # A `fits` on a former split, its steps unlinked: no longer a parent triage split,
            # so sub-issues linked by hand later wait for a human's `triage-todo` (section 6).
            self._record_parent(issue.number, replace(record, finished=True))
        self._log.info(
            "triage_filed",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            run_id=pending.run_id,
            verdict=pending.verdict.verdict,
            sub_issues=len(pending.verdict.sub_issues),
        )
        return None

    async def _verdict_owed(self, pending: PendingVerdict) -> Issue | None:
        """The issue as it stands now while its verdict is owed; ``None``, logged, once not.

        Section 1 discards a verdict once a state label wins, ``triage-todo`` goes or the issue
        closes, and the reconcile sees any of those only at the start of a tick: one made in
        the run's last interval, or while a filing waits for a retry, would otherwise get
        sub-issues, a comment and ``triage-complete`` beside the human's ``todo``. So the issue
        is read again, in one query, right before the first write. It is owed while it is
        still a triage candidate, and once its swap has landed, while it is open at all: it
        already shows the verdict as filed, and only the comment saying what it was is missing,
        whatever label a human has added since.
        """
        labels = self._workflow.config.github.labels
        found = await self._adapter.fetch_issues_by_ids([pending.issue.id])
        current = next((issue for issue in found if issue.id == pending.issue.id), None)
        if current is None:
            reason = "missing"
        elif is_triage_candidate(current, labels):
            return current
        elif current.github_state != "open":
            reason = "closed"
        elif pending.swapped:
            return current
        elif current.state_labels:
            reason = "state label"
        else:
            reason = "triage-todo removed"
        if reason in ("closed", "missing"):
            # As for a reconcile's stop: a closed or vanished issue has no chain left.
            self._forget_triage_chain(pending.issue.id)
        self._log.info(
            "triage_verdict_discarded",
            issue_number=pending.issue.number,
            issue_identifier=pending.issue.identifier,
            run_id=pending.run_id,
            reason=reason,
        )
        return None

    async def _dispatch_triage_one(self, issue: Issue, *, attempt: int) -> bool:
        """Start one triage run, or a split parent's review. Neither claims anything: the
        session moves no label (sections 4 and 6)."""
        labels = self._workflow.config.github.labels
        # Before the account (spec section 3, GHSA-jm8h-q3j6-p8xp): the latest `triage-todo` a
        # person applied approves the text as it stood, and an edit after it by anyone else
        # hands the issue back without a session ever reading it. One issuebot applied itself
        # -- auto-triage's -- is approved by authorship instead: a maintainer opened the issue
        # and nobody else has edited it. A refusal under either is handed back the same way.
        verdict = await self._assess_approval(issue, label=labels.triage_todo, authorship=True)
        if verdict is None:
            return False
        if isinstance(verdict, Unapproved):
            if issue.sub_issue_count > 0:
                # A split parent's review, refused as the watch's would be.
                outcome = await self._hand_over_unapproved_parent(issue, verdict.reason)
            else:
                outcome = await self._triage_hand_back(
                    issue,
                    title="unapproved edit",
                    reason=verdict.reason,
                    way_back=(
                        f"Apply `{labels.triage_todo}` again to approve the text as it stands now."
                    ),
                )
            if outcome == "applied":
                self._approval_verdicts.pop(issue.id, None)
            return False
        # Section 1: re-triaging an issue that already has sub-issues is the parent review, "so
        # nothing is filed twice": a fresh triage would write new titles, and its filing would
        # add a second batch beside the first. The candidate read says whether it has any, so
        # only such an issue costs a read of its steps; a read that fails skips the issue this
        # tick, as an unreadable approval history does.
        if issue.sub_issue_count > 0:
            try:
                steps = await self._adapter.sub_issues(issue.number)
            except GitHubError as exc:
                self._log.warning(
                    "triage_sub_issues_unreadable",
                    issue_number=issue.number,
                    issue_identifier=issue.identifier,
                    error=str(exc),
                    category=exc.category,
                )
                return False
            if steps:
                return await self._dispatch_requested_review(issue, steps, attempt=attempt)
        # The issue's own binding key (#121): a later work run on the issue lands on the same
        # account, and a sibling session never shares this one's uid.
        account, ready = self._bind_account(issue)
        if not ready:
            return False
        workflow = self._workflow
        self._start_run(
            issue,
            lambda entry: self._triage_worker(entry, workflow),
            kind="triage",
            attempt=attempt,
            account=account,
        )
        return True

    async def _triage_worker(self, entry: RunningEntry, workflow: Workflow) -> RunResult:
        # Narrowed to the one account bound to the issue (#121) and nothing else: a model label
        # chooses a work run's model, and triage has `triage.model` of its own.
        settings = settings_with_run_as(workflow.config, entry.account)
        return await self._run_triage(
            entry.issue,
            workflow,
            self._adapter,
            self._bus,
            workspaces=self._workspaces_factory(settings),
            runner=self._triage_runner_factory(settings),
            attempt=entry.attempt,
            cancel=entry.cancel,
            observer=RunObserver(
                entry, clock=self._clock, now=self._now, on_rate_limits=self._record_rate_limits
            ),
            run_id=entry.run_id,
        )

    async def _triage_exit(
        self, entry: RunningEntry, result: RunResult | None, error: str | None
    ) -> None:
        """What a triage run's end means: a verdict filed, a failure counted, or nothing.

        The slot, the account and the spend were settled by ``handle_worker_exit`` before it
        came here, exactly as for a work run.
        """
        if self._triage_run_settled(entry, result):
            return
        settings = self._workflow.config
        if result is not None and result.stop_reason == "verdict":
            try:
                verdict = parse_verdict(
                    result.structured_output,
                    max_sub_issues=settings.triage.max_sub_issues,
                    scrubber=self._scrubber,
                )
            except VerdictError as exc:
                # Section 4: a refused verdict counts as a failed run.
                await self._triage_failed(entry, f"verdict refused: {exc}")
                return
            pending = PendingVerdict(
                issue=entry.issue,
                verdict=verdict,
                run_id=entry.run_id,
                attempt=entry.attempt,
                turns=result.turns,
                cost_usd=result.cost_usd,
            )
            # A failure is kept for the next tick by `_file_verdict` itself.
            await self._file_verdict(pending)
            return
        await self._triage_failed(entry, _triage_failure(entry, result, error))

    def _triage_run_settled(self, entry: RunningEntry, result: RunResult | None) -> bool:
        """Whether a triage session's end is settled before any verdict is read; if so, settle it.

        A triage run and a parent review end alike here (spec 2026-10-25, sections 4 and 6): a
        stop the reconcile made, and an account-wide hold, discard whatever the run reached.
        """
        issue_id = entry.issue_id
        stopped = ("moved", "closed", "missing", "shutdown")
        if entry.terminal_issue is not None or entry.stop_cause in stopped:
            # Section 1: a state label won, the triage label went, or the issue did. Any verdict
            # the run reached is discarded, and the reconcile's own cancel is no failure of the
            # issue's -- `closed` included, for an issue reopened before the run ended, which
            # clears `terminal_issue`. A closed or vanished issue has no chain left.
            if entry.terminal_issue is not None or entry.stop_cause in ("closed", "missing"):
                self._forget_triage_chain(issue_id)
            self._schedule_triage_discard(entry.identifier)
            return True
        if result is not None and result.error_category in ("usage_limited", "auth_failed"):
            # Account-wide, and nothing about this issue: the same hold a work run takes, with
            # no failure counted, no escape and no requeue -- the label is what brings it back.
            if result.error_category == "usage_limited":
                self._hold_for_usage_limit(entry, result)
            else:
                self._hold_for_auth_failure(entry, result)
            interval_s = self._workflow.config.polling.interval_ms / 1000
            self._triage_not_before[issue_id] = self._clock() + interval_s
            self._schedule_triage_discard(entry.identifier)
            return True
        return False

    async def _triage_failed(self, entry: RunningEntry, error: str) -> None:
        """One more failure on the issue's triage chain: back off, or hand it back (section 4).

        A parent review's failures count on the same chain (section 6), and its spent chain is a
        parent hand-over. Triage's give-up would leave the record watched, and with every step
        still closed the watch would start a fresh chain on the next tick: a paid session and a
        public note per cycle, for ever.
        """
        # Scrubbed once, here, as `_after_failure` does (#91): it becomes a public note.
        error = self._scrubber.scrub(error)
        agent = self._workflow.config.agent
        issue_id = entry.issue_id
        failures = self._triage_failures.get(issue_id, 0) + 1
        self._schedule_triage_discard(entry.identifier)
        self._log.warning(
            "triage_failed",
            issue_number=entry.issue.number,
            issue_identifier=entry.identifier,
            run_id=entry.run_id,
            failures=failures,
            max_attempts=agent.max_attempts,
            error=error,
        )
        if failures < agent.max_attempts:
            self._triage_failures[issue_id] = failures
            delay_s = backoff_ms(failures, agent.max_retry_backoff_ms) / 1000
            self._triage_not_before[issue_id] = self._clock() + delay_s
            return
        self._forget_triage_chain(issue_id)
        if entry.kind == "parent_review":
            # Recorded once it lands; one GitHub refuses is owed in `_parent_handovers`.
            await self._hand_over_failed_review(
                entry.issue, f"Parent review failed {plural(failures, 'time')}; the last: {error}"
            )
            return
        await self._triage_give_up(
            entry.issue, f"Triage failed {plural(failures, 'time')}; the last: {error}"
        )

    async def _triage_give_up(self, issue: Issue, reason: str) -> None:
        """Hand back an issue whose triage chain is spent; remembered until the note lands.

        An issue that has gained sub-issues meanwhile is a parent, whose `triage-todo` now runs
        its review, so it is handed over as a review that failed: the note GitHub refuses is
        then owed as a parent's (``_parent_handovers``), never here.
        """
        if issue.sub_issue_count > 0:
            await self._hand_over_failed_review(issue, reason)
            return
        labels = self._workflow.config.github.labels
        outcome = await self._triage_hand_back(
            issue,
            title="triage failed",
            reason=reason,
            way_back=f"Add `{labels.triage_todo}` again to retry once the cause is fixed.",
        )
        if outcome == "failed":
            self._triage_escapes[issue.id] = reason
        else:
            self._triage_escapes.pop(issue.id, None)

    async def _triage_hand_back(
        self, issue: Issue, *, title: str, reason: str, way_back: str
    ) -> actions.EscapeOutcome:
        """`triage_escape`: `triage-todo` off, then a note. It never touches a state label.

        One that landed ends the issue's triage chain, as a work escape that landed ends the
        work chain (``_record_escape``): the issue is a human's now, and the `triage-todo` that
        brings it back starts again at attempt 1, with no backoff.

        An issue with sub-issues is handed over as a parent instead (section 6, "one shape"):
        taking only `triage-todo` off a parent a human swapped to it would leave it with
        neither triage label, and that releases every step its order holds. A cause that has a
        parent hand-over of its own -- an unapproved edit, the spend ceiling, a spent chain --
        is sent to that one by the caller, so it reads the same whichever pass met it; this
        catches the rest under the caller's own title.
        """
        if issue.sub_issue_count > 0:
            return await self._hand_over_parent(
                issue, title=title, reason=reason, way_back=way_back
            )
        outcome = await actions.triage_escape(
            self._adapter,
            self._bus,
            issue,
            title=title,
            reason=self._scrubber.scrub(reason),
            way_back=way_back,
            now=self._now(),
        )
        if outcome == "applied":
            self._counters = self._counters.bump(blocked=1)
            self._forget_triage_chain(issue.id)
        return outcome

    def _forget_triage_chain(self, issue_id: str) -> None:
        # An owed parent hand-over is the parent review's `_triage_escapes`: whatever ends the
        # chain -- a verdict filed, a note that landed, a parent closed -- makes it moot too.
        self._triage_failures.pop(issue_id, None)
        self._triage_not_before.pop(issue_id, None)
        self._triage_escapes.pop(issue_id, None)
        self._parent_handovers.pop(issue_id, None)

    def _schedule_triage_discard(self, identifier: str) -> None:
        self._triage_discard[identifier] = self._clock() + TRIAGE_DISCARD_DELAY_S

    async def _discard_triage_workspaces(self) -> None:
        """Remove each triage clone whose delay is up, unless a run for its issue is reading it.

        As the account bound to the issue (#121), which is the one the run read the clone as;
        a failure is logged and left to the startup sweep rather than retried every tick.
        """
        if not self._triage_discard:
            return
        now = self._clock()
        running = {entry.identifier for entry in self._running.values()}
        for identifier, due in list(self._triage_discard.items()):
            if due > now or identifier in running:
                continue
            del self._triage_discard[identifier]
            try:
                await self._workspaces_for_key(workspace_key(identifier)).remove_triage(identifier)
            except AgentError as exc:
                self._log.warning(
                    "triage_workspace_remove_failed",
                    issue_identifier=identifier,
                    error=exc.message,
                )

    # --- the parent review (spec 2026-10-25-triage-design.md, section 6) -------------------

    def _record_parent(self, number: int, record: ParentRecord) -> None:
        """Keep ``record`` and publish it whole. The one place a parent's record changes, so the
        latest ``parent_review_state`` the store holds for it is always the one this worker
        acts on, and a restart reads back exactly that."""
        self._parents[number] = record
        self._bus.publish(record.state_event(number))

    async def _dispatch_parent_reviews(self) -> int:
        """Review each watched parent whose every step has closed, with no label from anyone.

        The watch is one ``parent_steps`` read of every watched parent, made only when a review
        could start or a hand-over note is owed: every deployment shares the account's hourly
        GraphQL budget (section 9). A failed read is logged and skips the watch for this tick;
        like the triage read, it does not feed the GitHub hold, which is the board's. A parent
        GitHub no longer returns, or that has closed, is finished, whoever closed it.

        The parents' issues are read only for what the watch can act on this tick: an owed
        note, written while the parent is open whether or not it is still due, so no note that
        keeps the read going is left with nothing to write it; and a due parent that is not
        running, not owed a verdict and not backing off. Notes go before any review, so a parent
        the gate refuses on ``slots`` holds up no note owed on a later one.
        """
        # A parent with a run going or a verdict owed is that run's or that verdict's this tick:
        # `_retry_pending_verdicts` writes the note a verdict owes. Issue ids are the number
        # (`Issue.id`); the steps' `id` is GraphQL's node id, which none of this takes.
        busy = self._running.keys() | self._parent_pending.keys()
        watched = sorted(number for number, record in self._parents.items() if record.watched)
        owed = any(str(n) in self._parent_handovers and str(n) not in busy for n in watched)
        if not watched or (self._slots() == 0 and not owed):
            return 0
        labels = self._workflow.config.github.labels
        try:
            found = await self._adapter.parent_steps(watched)
        except GitHubError as exc:
            self._log.warning("parent_watch_failed", error=str(exc), parents=watched)
            return 0
        notes: list[int] = []
        due: dict[int, tuple[int, ...]] = {}
        for number in watched:
            steps, issue_id = found.get(number), str(number)
            if steps is None or steps.state == "closed":
                self._record_parent(number, replace(self._parents[number], finished=True))
                # As for a reconcile's stop: a closed or vanished parent has no chain left, and
                # no note is owed on it.
                self._forget_triage_chain(issue_id)
            elif issue_id in busy:
                continue
            elif issue_id in self._parent_handovers:
                notes.append(number)
            elif parent_review_due(steps, labels) and self._clock() >= (
                self._triage_not_before.get(issue_id, -math.inf)
            ):
                due[number] = tuple(step.number for step in steps.sub_issues)
        if not notes and not due:
            return 0
        try:
            issues = await self._adapter.fetch_issues_by_ids([str(n) for n in (*notes, *due)])
        except GitHubError as exc:
            self._log.warning("parent_watch_failed", error=str(exc), parents=[*notes, *due])
            return 0
        by_number = {issue.number: issue for issue in issues}
        for number in notes:
            if (issue := by_number.get(number)) is not None:
                note = self._parent_handovers[issue.id]
                await self._hand_over_parent(
                    issue, title=note.title, reason=note.reason, way_back=note.way_back
                )
        triage = self._workflow.config.triage
        dispatched = 0
        for number, seen in due.items():
            issue = by_number.get(number)
            if issue is None:
                continue
            record = self._parents[number]
            if record.reviews >= triage.max_parent_reviews:
                # Checked before the gate, and needing no slot: the cap is about the parent,
                # and a chain of `next` verdicts must not run on for ever (section 6).
                await self._hand_over_parent(
                    issue,
                    title="parent review limit",
                    reason=(
                        f"#{issue.number} has reached `triage.max_parent_reviews`, the most "
                        "reviews issuebot runs on a parent by itself"
                    ),
                    way_back=(
                        f"It has had {triage.max_parent_reviews} reviews. Add "
                        f"`{labels.triage_todo}` to review it again with its count reset."
                    ),
                )
                continue
            verdict = self._admit(issue.identifier, issue.id, issue, kind="parent_review")
            if isinstance(verdict, Refused):
                if verdict.kind in ("slots", "hold", "stopping"):
                    # Nothing about this parent; the next one would be told the same.
                    break
                if verdict.kind == "spend":
                    # Handed over rather than refused on every tick, as a triage candidate is.
                    await self._hand_over_parent_over_budget(issue, verdict.reason)
                continue
            attempt = self._triage_failures.get(issue.id, 0) + 1
            if await self._dispatch_parent_review_one(issue, record, seen, attempt=attempt):
                dispatched += 1
        return dispatched

    async def _dispatch_parent_review_one(
        self, issue: Issue, record: ParentRecord, steps: tuple[int, ...], *, attempt: int
    ) -> bool:
        """Start one parent review on ``steps``. Like triage, it claims nothing (section 6)."""
        labels = self._workflow.config.github.labels
        # Section 3, as for triage: the latest `triage-todo` a person applied approves the text
        # as it stood, and an edit after it by anyone else hands the parent over before a
        # session reads it. A parent whose latest `triage-todo` was issuebot's own -- an
        # auto-triaged one -- is re-checked by authorship, the rule that approved its triage.
        verdict = await self._assess_approval(issue, label=labels.triage_todo, authorship=True)
        if verdict is None:
            return False
        if isinstance(verdict, Unapproved):
            outcome = await self._hand_over_unapproved_parent(issue, verdict.reason)
            if outcome == "applied":
                self._approval_verdicts.pop(issue.id, None)
            return False
        account, ready = self._bind_account(issue)
        if not ready:
            return False
        workflow = self._workflow
        self._start_run(
            issue,
            lambda entry: self._parent_review_worker(entry, workflow, record),
            kind="parent_review",
            attempt=attempt,
            account=account,
            steps_seen=steps,
        )
        return True

    async def _dispatch_requested_review(
        self, issue: Issue, steps: Sequence[SubIssue], *, attempt: int
    ) -> bool:
        """`triage-todo` on a split parent (section 1): its review now, or a note to wait.

        The triage pass has already admitted it and checked its approval. A human has
        re-authorised the parent, so its record starts afresh either way (section 6): the
        review count and gap rounds reset, and a hand-over is lifted. With a step still open
        there is nothing to review yet, so `triage-complete` goes back -- the steps keep
        running in order -- and the watch reviews the parent once they close. That note is no
        hand-over: no `Blocked`, nothing recorded as handed over, and one GitHub refuses leaves
        `triage-todo` on, so the next triage pass simply decides again.
        """
        labels = self._workflow.config.github.labels
        record = ParentRecord(identifier=issue.identifier)
        still_open = [f"#{step.number}" for step in steps if step.state == "open"]
        if still_open:
            self._record_parent(issue.number, record)
            # Numbers and fixed prose only, so there is nothing in it to scrub.
            await actions.parent_hand_over(
                self._adapter,
                self._bus,
                issue,
                title="parent review waiting",
                reason=(
                    f"#{issue.number} still has open steps: {', '.join(still_open)}. issuebot "
                    "reviews it by itself once every step has closed."
                ),
                way_back=(
                    "Its review count and gap rounds are reset. issuebot has put "
                    f"`{labels.triage_complete}` back, so its steps still run in order."
                ),
                now=self._now(),
                blocked=False,
            )
            return False
        # Bound before the record is reset, so a parent waiting for an account publishes no
        # record on each tick it waits.
        account, ready = self._bind_account(issue)
        if not ready:
            return False
        self._record_parent(issue.number, record)
        workflow = self._workflow
        self._start_run(
            issue,
            lambda entry: self._parent_review_worker(entry, workflow, record),
            kind="parent_review",
            attempt=attempt,
            account=account,
            steps_seen=tuple(step.number for step in steps),
        )
        return True

    async def _parent_review_worker(
        self, entry: RunningEntry, workflow: Workflow, record: ParentRecord
    ) -> RunResult:
        # Triage's session but for its prompt, its schema and the steps it reads (section 6),
        # narrowed to the one account bound to the issue as a triage run is.
        settings = settings_with_run_as(workflow.config, entry.account)
        return await self._run_triage(
            entry.issue,
            workflow,
            self._adapter,
            self._bus,
            workspaces=self._workspaces_factory(settings),
            runner=self._parent_review_runner_factory(settings),
            attempt=entry.attempt,
            cancel=entry.cancel,
            observer=RunObserver(
                entry, clock=self._clock, now=self._now, on_rate_limits=self._record_rate_limits
            ),
            run_id=entry.run_id,
            parent_review=ParentReviewRun(reviews=record.reviews, gap_rounds=record.gap_rounds),
        )

    async def _parent_review_exit(
        self, entry: RunningEntry, result: RunResult | None, error: str | None
    ) -> None:
        """What a parent review's end means: ``_triage_exit``'s, but for the verdict it files.

        Its failures count on the issue's triage chain (section 6: the session is triage's), so
        a stop, a hold and a failed run end it exactly as they end a triage run.
        """
        if self._triage_run_settled(entry, result):
            return
        settings = self._workflow.config
        if result is not None and result.stop_reason == "verdict":
            try:
                verdict = parse_parent_review(
                    result.structured_output,
                    max_sub_issues=settings.triage.max_sub_issues,
                    scrubber=self._scrubber,
                )
            except VerdictError as exc:
                await self._triage_failed(entry, f"verdict refused: {exc}")
                return
            record = self._parents.get(entry.issue.number) or ParentRecord(
                identifier=entry.identifier
            )
            # The one place a verdict is judged before it is filed: the counts it would bring the
            # parent to, which also say whether a `gap` is past the gap limit and is handed over
            # in place of filed (`PendingParentReview.past_gap_limit`).
            gap_round = record.gap_rounds + 1 if verdict.verdict == "gap" else record.gap_rounds
            pending = PendingParentReview(
                issue=entry.issue,
                verdict=verdict,
                seen=entry.steps_seen,
                counts=ReviewCounts(
                    review=record.reviews + 1,
                    max_reviews=settings.triage.max_parent_reviews,
                    gap_round=gap_round,
                    max_gap_rounds=settings.triage.max_gap_rounds,
                ),
                run_id=entry.run_id,
                attempt=entry.attempt,
                turns=result.turns,
                cost_usd=result.cost_usd,
            )
            # A failure is kept for the next tick by `_file_parent_review` itself.
            await self._file_parent_review(pending)
            return
        await self._triage_failed(entry, _triage_failure(entry, result, error))

    async def _hand_over_at_gap_limit(self, issue: Issue, pending: PendingParentReview) -> None:
        """Section 6: a `gap` once the gap rounds are spent posts what it found, files nothing
        and hands the parent over, its would-be steps listed for the human to file or not.

        Reached from the filing, on its fresh read of the parent (``issue``), so a verdict whose
        steps changed while the review ran is discarded there and hands nothing over. The
        review counts, because the worker acted on its verdict (ruling 7), whether or not GitHub
        took the note: one it refused is owed like any parent hand-over.

        The reason is a fixed sentence, since it is the `Blocked` line Slack shows and every
        later record event carries; the review's own summary goes in the note's body.
        """
        settings = self._workflow.config
        verdict = pending.verdict
        rounds = pending.counts.max_gap_rounds
        found = (
            f"The review found gaps again after {plural(rounds, 'gap round')}"
            if rounds
            else "The review found gaps, and `triage.max_gap_rounds` is 0"
        )
        drafts = "\n".join(f"- {draft.title}" for draft in verdict.sub_issues)
        await self._hand_over_parent(
            issue,
            title="parent review gap limit",
            reason=f"{found}, so it filed nothing.",
            way_back=(
                f"What it found: {verdict.summary}\n\nWhat it would have filed:\n{drafts}\n\n"
                "File what is still needed yourself. issuebot will not review this issue again "
                f"until `{settings.github.labels.triage_todo}` is added, which reviews it afresh "
                "with its rounds reset."
            ),
        )
        # After the hand-over, so the count keeps the `handed_over` it has just recorded.
        record = self._parents.get(issue.number) or ParentRecord(identifier=issue.identifier)
        self._record_parent(issue.number, replace(record, reviews=record.reviews + 1))

    async def _file_parent_review(self, pending: PendingParentReview) -> GitHubError | None:
        """File one review's verdict; ``None`` once it landed or is owed no longer, else the error.

        ``_file_verdict``'s shape, on a fresh read of the parent (``_parent_review_owed``). That
        read is handed to the store before the first write: a split parent wears no state
        label, so the board's poll never reports it, and the store writes a ``met`` close only
        onto a row it already has. Success writes the record the review brings the parent to
        and ends its triage chain; a failure keeps the verdict, with whether its move landed,
        and the caller counts it.

        A `gap` past the gap limit is acted on by a hand-over in place of the filing, after the
        same read (ruling 15): one whose steps changed is discarded as any stale verdict is,
        and one whose read failed is kept and tried again like any other.
        """
        issue = pending.issue
        moved = pending.moved

        def note_moved() -> None:
            nonlocal moved
            moved = True

        try:
            current = await self._parent_review_owed(pending)
            if current is not None and not pending.past_gap_limit:
                self._report_issues([current])
                await apply_parent_review(
                    self._adapter,
                    self._bus,
                    current,
                    pending.verdict,
                    seen=pending.seen,
                    counts=pending.counts,
                    run_id=pending.run_id,
                    attempt=pending.attempt,
                    turns=pending.turns,
                    cost_usd=pending.cost_usd,
                    now=self._now(),
                    on_moved=note_moved,
                    moved=pending.moved,
                )
        except GitHubError as exc:
            self._log.warning(
                "parent_review_file_failed",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                run_id=pending.run_id,
                tries=pending.tries,
                error=str(exc),
                category=exc.category,
            )
            # The session's work is kept: the next tick files it from memory, never by running
            # the session again.
            self._parent_pending[issue.id] = replace(pending, moved=moved)
            return exc
        self._parent_pending.pop(issue.id, None)
        self._schedule_triage_discard(issue.identifier)
        if current is None:
            return None
        if pending.past_gap_limit:
            await self._hand_over_at_gap_limit(current, pending)
            return None
        counts = pending.counts
        self._record_parent(
            issue.number,
            ParentRecord(
                identifier=issue.identifier,
                reviews=counts.review,
                gap_rounds=counts.gap_round,
                finished=pending.verdict.verdict == "met",
            ),
        )
        self._forget_triage_chain(issue.id)
        self._log.info(
            "parent_review_filed",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            run_id=pending.run_id,
            verdict=pending.verdict.verdict,
            sub_issues=len(pending.verdict.sub_issues),
            reviews=counts.review,
            gap_rounds=counts.gap_round,
        )
        return None

    async def _parent_review_owed(self, pending: PendingParentReview) -> Issue | None:
        """The parent as it stands now while its verdict is owed; ``None``, logged, once not.

        ``_verdict_owed``'s reasoning: the reconcile sees a change only at the start of a tick,
        so the parent and its steps are read again right before the first write. Before the
        move it is owed only while the parent is open, carries a triage label and no state
        label, and has the steps the review saw, every one closed: a step reopened, added or
        unlinked since makes the verdict stale. Once the move has landed it is owed while the
        parent exists, since only the comment saying what was filed is missing.

        A failed try's own writes are not changes. The steps it created before failing are the
        verdict's (``steps_as_seen``), and so is ``complete`` on a ``met`` parent: that label is
        the close's first write, and its retry must still close the parent and say so. A
        ``met`` parent closed and wearing it is the close itself landing under an error (a 502
        that still applied), so its events and comment are owed too, and the retry's label
        write and close repeat what is already there.
        """
        labels = self._workflow.config.github.labels
        triage_labels = {labels.triage_todo.lower(), labels.triage_complete.lower()}
        own = {labels.complete.lower()} if pending.verdict.verdict == "met" else set()
        found = await self._adapter.fetch_issues_by_ids([pending.issue.id])
        current = next((issue for issue in found if issue.id == pending.issue.id), None)
        if current is None:
            reason = "missing"
        elif pending.moved:
            return current
        elif current.github_state != "open":
            if own and own.issubset(current.state_labels):
                return current
            reason = "closed"
        elif set(current.state_labels) - own:
            reason = "state label"
        elif not triage_labels.intersection(current.labels):
            reason = "triage labels removed"
        else:
            steps = (await self._adapter.parent_steps([current.number])).get(current.number)
            # A gap past the gap limit is handed over and files no step, so it owns none.
            filing = () if pending.past_gap_limit else pending.verdict.sub_issues
            if steps is not None and steps_as_seen(steps, pending.seen, filing):
                return current
            reason = "steps changed"
        if reason in ("closed", "missing"):
            # As for a reconcile's stop: a closed or vanished parent has no chain left.
            self._forget_triage_chain(pending.issue.id)
        self._log.info(
            "parent_review_discarded",
            issue_number=pending.issue.number,
            issue_identifier=pending.issue.identifier,
            run_id=pending.run_id,
            reason=reason,
        )
        return None

    async def _hand_over_parent(
        self, issue: Issue, *, title: str, reason: str, way_back: str
    ) -> actions.EscapeOutcome:
        """`parent_hand_over`: the one shape every hand-over of a parent takes (section 6).

        One that landed is recorded, so the watch leaves a parent that is a human's now, and it
        ends the triage chain, as a triage hand-back does: the `triage-todo` that resumes the
        parent starts again at attempt 1. One GitHub refused is kept to write again.
        """
        # Scrubbed once, here (#91): it becomes a public note, a Slack message and the record.
        reason = self._scrubber.scrub(reason)
        outcome = await actions.parent_hand_over(
            self._adapter,
            self._bus,
            issue,
            title=title,
            reason=reason,
            way_back=way_back,
            now=self._now(),
        )
        if outcome == "failed":
            self._parent_handovers[issue.id] = ParentHandOver(
                title=title, reason=reason, way_back=way_back
            )
            return outcome
        self._counters = self._counters.bump(blocked=1)
        record = self._parents.get(issue.number) or ParentRecord(identifier=issue.identifier)
        self._record_parent(issue.number, replace(record, handed_over=reason))
        # The owed note goes with the chain (`_forget_triage_chain`).
        self._forget_triage_chain(issue.id)
        return outcome

    # The causes both passes can meet: the watch, and the triage pass over a parent carrying a
    # human's `triage-todo`. Each has one title and one way back wherever it is met, so the
    # issue's history, Slack and the docs name the cause once.

    async def _hand_over_unapproved_parent(
        self, issue: Issue, reason: str
    ) -> actions.EscapeOutcome:
        """Section 6: a parent edited by anyone but its approver is not reviewed."""
        labels = self._workflow.config.github.labels
        return await self._hand_over_parent(
            issue,
            title="unapproved edit",
            reason=reason,
            way_back=(
                f"Apply `{labels.triage_todo}` again to approve the text as it stands now; "
                "issuebot then reviews it afresh."
            ),
        )

    async def _hand_over_parent_over_budget(
        self, issue: Issue, reason: str
    ) -> actions.EscapeOutcome:
        """The parent's spend has reached `agent.max_issue_cost_usd`, which a review counts."""
        labels = self._workflow.config.github.labels
        return await self._hand_over_parent(
            issue,
            title="parent review budget limit",
            reason=reason,
            way_back=(
                "What `agent.max_issue_cost_usd` counts is what the issue has already cost, "
                f"and that never resets: raise the ceiling, then add `{labels.triage_todo}` "
                "again."
            ),
        )

    async def _hand_over_failed_review(self, issue: Issue, reason: str) -> actions.EscapeOutcome:
        """The parent's triage chain is spent: `agent.max_attempts` failed runs in a row."""
        labels = self._workflow.config.github.labels
        return await self._hand_over_parent(
            issue,
            title="parent review failed",
            reason=reason,
            way_back=f"Add `{labels.triage_todo}` again to review it once the cause is fixed.",
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
        labels = self._workflow.config.github.labels
        triage_todo = labels.triage_todo.lower()
        triage_labels = {triage_todo, labels.triage_complete.lower()}
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
            if entry.kind == "triage":
                # Spec 2026-10-25, section 1: a state label wins over the triage track, and a
                # human taking `triage-todo` off means "leave this alone". Either way the run is
                # stopped and its verdict, if it reaches one, is discarded at the exit.
                if current.state_labels:
                    self._stop_entry(entry, "moved", "state label")
                elif triage_todo not in current.labels:
                    self._stop_entry(entry, "moved", "triage-todo removed")
                continue
            if entry.kind == "parent_review":
                # Section 6, as for triage: a state label wins, and a parent carrying neither
                # triage label is one a human has taken off the track. A `triage-todo` added
                # meanwhile stops nothing: the review running is the one it asks for.
                if current.state_labels:
                    self._stop_entry(entry, "moved", "state label")
                elif not triage_labels.intersection(current.labels):
                    self._stop_entry(entry, "moved", "triage labels removed")
                continue
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
        """Symphony §8.6, repeated: closed issues still carrying a state label.

        Not every state label: ``complete`` is where a closed issue rests, so the adapter
        stopped asking for that role in #149 and the sweep's cost stopped growing with
        everything the deployment had ever finished. Two things the re-read was quietly doing
        are done here instead. The store keeps its refreshed snapshot of a closed issue,
        because the issues this sweep *moved* are read back once by number and reported --
        bounded by the work of one sweep rather than by the history of all of them. And the
        retry of a workspace removal that failed at the time is now the workspace's own
        business: `_retry_finished_workspaces` asks the disk, which knows about a workspace
        whose issue nobody is asking about any more.
        """
        try:
            issues = await self._adapter.fetch_terminal_issues()
        except GitHubError as exc:
            self._log.warning("terminal_sweep_failed", error=str(exc))
            return
        self._report_issues(issues)
        moved: dict[str, StateLabel | None] = {}
        for issue in issues:
            if issue.id in self._running:
                continue
            self._retries.pop(issue.id, None)
            outcome = await self._finish(issue)
            if outcome in ("complete", "no_change"):
                moved[issue.id] = StateLabel.COMPLETE
            elif outcome == "cancelled":
                moved[issue.id] = None
        await self._report_moved(moved)
        await self._retry_finished_workspaces()
        # After the removals above, so a workspace this sweep deleted gives its account back
        # now rather than on the next one (#121).
        self._prune_accounts()

    async def _report_moved(self, moved: Mapping[str, StateLabel | None]) -> None:
        """Re-read the issues this sweep relabelled, so the store holds them as they now are.

        What `_report_issues` hands the store above is each issue as the sweep *found* it,
        carrying the label `finish_terminal` is about to take off it. Until #149 the next
        sweep put that right, by re-reading every completed issue there had ever been; this
        reads back only what this sweep changed, which in a steady state is nothing and costs
        no request at all. A read that fails is a warning and leaves the row as the sweep
        found it -- the label move itself reaches the store through `state_changed` either
        way, so what a failure costs is the issue's stored label list and title, not its
        state.

        Every answer is checked against the state this sweep moved the issue *to*, and one
        that still shows the old role is dropped with a warning. The write went through `gh
        issue edit` and this read comes back over GraphQL milliseconds later, so an answer
        from a replica that has not caught up is a real possibility -- and it would carry the
        newest `seen_at`, which is what `UPSERT_ISSUE` orders by, so it would overwrite the
        state `state_changed` had just recorded. Under the old scheme the next sweep repaired
        that; nothing reads a `complete` issue again now, so a stale row would be permanent.
        The refresh is the cheap half of this and the state is the load-bearing half: when
        they disagree the refresh is what gives way.

        The read exists for the store alone, so a deployment without one does not make it:
        `_report_issues` is a no-op without an observer, and in a change whose subject is
        avoidable GitHub reads, spending a request per sweep on an answer nobody reads would
        be the same defect in miniature. The same gate `fetch_states` uses for `review`.
        """
        if not moved or self._on_issues is None:
            return
        try:
            refreshed = await self._adapter.fetch_issues_by_ids(list(moved))
        except GitHubError as exc:
            self._log.warning("terminal_refresh_failed", error=str(exc), count=len(moved))
            return
        fresh = []
        for issue in refreshed:
            if issue.id in moved and issue.state is moved[issue.id]:
                fresh.append(issue)
            else:
                self._log.warning(
                    "terminal_refresh_stale",
                    issue_number=issue.number,
                    issue_identifier=issue.identifier,
                    state=issue.state.value if issue.state is not None else None,
                )
        self._report_issues(fresh)

    async def _retry_finished_workspaces(self) -> None:
        """Remove again every workspace a previous removal marked and did not take (#149).

        `WorkspaceManager.remove` writes `.issuebot/finished` before it unlinks anything, so a
        workspace still carrying the mark is one whose removal failed -- the case the sweep's
        re-read of the `complete` role used to cover, at the price of asking GitHub for every
        issue issuebot had ever finished. The candidates come off the disk instead, which is
        bounded by the failures that put them there and survives a restart, and each is
        removed as the account bound to it (#121), since the unlink is the session's files'.

        A key the worker is still using is skipped: a running session's, and a pending retry's
        -- the same set `_prune_accounts` keeps a binding for. A reused workspace has had its
        mark cleared by `create_or_reuse` before its session started, so an issue that reopened
        is not swept out from under the run working it. That clear is best effort, and this
        skip is about *this* worker's keys, so a `run-once` beside a live worker (#121) rests
        on it having succeeded: the two together would have to fail for a live clone to be
        removed, and what that costs is a re-clone, since the work is pushed.
        """
        keys = self._workspaces.finished_keys()
        if not keys:
            return
        busy = self._pool_keys()
        for key in keys:
            if key in busy:
                continue
            try:
                await self._workspaces_for_key(key).remove_key(key)
            except AgentError as exc:
                # Its own event name: `actions.remove_workspace` logs `workspace_remove_failed`
                # with the issue's identifier, and this retry has a directory and no issue.
                self._log.warning("workspace_retry_failed", workspace_key=key, error=exc.message)

    async def _finish(self, issue: Issue) -> actions.FinishOutcome:
        """Close the issue out, and drop what this worker remembered about it.

        Both conflict memos go whatever GitHub answered: a closed issue is never a bounce
        candidate again. The ledger entry goes only when the close actually landed, since a
        ``failed`` one leaves the issue open and its budget still in force. A reopened issue
        starts from zero either way. Dropping ``_conflict_gave_up`` here is what keeps it a
        memo rather than a leak: an issue that hit a capped read once would otherwise hold an
        entry for the life of the process, which is the growth this issue is about (#110).

        The two approval memos go for the same reason (GHSA-jm8h-q3j6-p8xp):
        ``_approval_check_failed``, the error an unreadable history was last logged with, and
        ``_approval_verdicts``, the last verdict and the snapshot it was about. A closed issue
        is never a candidate, so neither can be read again for it, and a reopened one is a
        fresh snapshot that would miss the verdict anyway.

        The triage chain goes too (spec 2026-10-25, section 4): a triage run a human's ``todo``
        stopped leaves its failures behind, and the issue this closes is where they would sit.
        """
        # Removing a workspace means unlinking what the session wrote, which only the session's
        # own account can do (#75) -- and under a pool that is the account bound to *this*
        # workspace, not the pool's first member (#121).
        outcome = await actions.finish_terminal(
            self._adapter, self._bus, self._workspaces_for(issue), issue
        )
        self._conflict_limit_noted.pop(issue.id, None)
        self._conflict_gave_up.pop(issue.id, None)
        self._approval_check_failed.pop(issue.id, None)
        self._approval_verdicts.pop(issue.id, None)
        self._forget_triage_chain(issue.id)
        if outcome != "failed":
            self._ledger.forget(issue.identifier)
        if outcome in ("complete", "no_change"):
            # A no-fault close is a completion here too: the investigation is the delivered work.
            self._counters = self._counters.bump(issues_completed=1)
        elif outcome == "cancelled":
            self._counters = self._counters.bump(issues_cancelled=1)
        return outcome

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
        return self._workspaces_for_key(workspace_key(issue.identifier), issue_number=issue.number)

    def _workspaces_for_key(self, key: str, *, issue_number: int | None = None) -> WorkspaceManager:
        """The same, for a workspace named by its key alone: the sweep's removal retry has a
        directory on disk and no issue to look the binding up from (#149)."""
        settings = self._workflow.config
        account = session_account(settings)
        if self._pool is not None:
            try:
                account = self._pool.bound(key) or account
            except AgentError as exc:
                # The record is unreadable, so the binding is unknown; the first member still
                # delegates, and the manager finds the files' real owner from the tree.
                self._log.warning(
                    "account_lookup_failed",
                    issue_number=issue_number,
                    workspace_key=key,
                    error=exc.message,
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
            kind=entry.kind,
            attempt=entry.attempt,
            outcome=result.outcome if result is not None else None,
            stop_reason=result.stop_reason if result is not None else None,
            cause=entry.stop_cause,
            turns=result.turns if result is not None else entry.turns,
            cost_usd=result.cost_usd if result is not None else 0.0,
            error=error or (result.error if result is not None else None),
        )
        # After the slot, the account and the spend are settled above, which every run shares;
        # what the end of a triage run or a parent review means is its own (spec 2026-10-25,
        # sections 4 and 6).
        if entry.kind == "triage":
            await self._triage_exit(entry, result, error)
            return
        if entry.kind == "parent_review":
            await self._parent_review_exit(entry, result, error)
            return
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
                    f"Turn budget exhausted: {plural(result.turns, 'turn')} in attempt "
                    f"{entry.attempt} without reaching `{review}`."
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
        if result.error_category == "usage_limited":
            self._usage_limited(entry, result)
            return
        if result.error_category == "run_timeout" and result.final_state is StateLabel.IN_PROGRESS:
            # The run's wall clock is spent (#110). A retry never resumes the session, so it
            # would re-read the repository from cold and spend the same clock again, up to
            # max_attempts times over: the case max_turns escapes for, and it escapes the same
            # way. The issue's ceiling is therefore agent.run_timeout_ms, not a multiple of it.
            review = self._workflow.config.github.labels.review
            reason = (
                f"Wall clock exhausted: {plural(result.turns, 'turn')} in attempt {entry.attempt} "
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
        error = self._hold_for_auth_failure(entry, result)
        reason = (
            f"Claude could not authenticate in attempt {entry.attempt}, so the run failed: "
            f"{error}. The worker has stopped claiming issues and re-checks the credential "
            "every poll; it resumes on its own once `claude auth status` reports a login."
        )
        await self._escape(entry, reason, result)

    def _usage_limited(self, entry: RunningEntry, result: RunResult) -> None:
        """The account's window is spent: hold dispatch until it reopens and requeue the issue.

        Deliberately none of the three things a failure does. It does not escalate, because
        nothing about this issue is wrong and a human has nothing to fix. It does not count
        against ``agent.max_attempts``, because the chain is meant to bound an issue that
        keeps failing, and a limit is an account-wide condition every issue on the board meets
        at the same moment. And it does not back off on the failure curve, because that curve
        is 20 s and then 40 s -- so on 2026-09-22 three issues each burned their whole chain
        inside ninety seconds against a window that reopened nineteen minutes later, and each
        was escalated to a human for it.

        The hold is what keeps that from being per-issue at all: no issue is claimed while it
        lasts, so the board stops instead of grinding every candidate through the same wall.
        """
        error = self._hold_for_usage_limit(entry, result)
        # Requeued on the entry's own attempt, not the next one: the chain is untouched, so
        # the run that follows the window is this attempt over again.
        self._schedule(
            entry.issue,
            attempt=entry.attempt,
            kind="usage",
            delay_ms=self._usage_delay_ms(),
            error=error,
        )

    def _hold_for_auth_failure(self, entry: RunningEntry, result: RunResult) -> str:
        """Hold dispatch for a run claude could not authenticate (#20); the error, for the caller.

        Any run's, work or triage: the credential is the account's, so whichever kind of run
        found it broken, nothing more is claimed until a probe reports a login. What the issue
        is owed is the caller's -- a work run escalates it, a triage run waits.
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
        return error

    def _hold_for_usage_limit(self, entry: RunningEntry, result: RunResult) -> str:
        """Hold dispatch until the refused window reopens; the error, for the caller's requeue.

        Any run's, work or triage: the window is the account's, so a refusal met by either kind
        stops the whole board (`_usage_limited` says why that matters). Neither kind counts it
        as a failure; what the issue does next is the caller's.
        """
        error = result.error or "claude refused the turn: usage limit reached"
        self._hold_usage(error, result.usage_reset_at, result.usage_window)
        self._log.warning(
            "dispatch_usage_held",
            issue_number=entry.issue.number,
            issue_identifier=entry.identifier,
            run_id=entry.run_id,
            attempt=entry.attempt,
            reset_at=self._usage_reset_at.isoformat() if self._usage_reset_at else None,
            error=error,
        )
        return error

    def _usage_delay_ms(self) -> int:
        """How long until the refused window reopens, floored at one poll interval.

        Floored because a reset already in the past -- a clock that disagrees, or a reading
        that arrived late -- would otherwise requeue the issue into the same wall at once,
        which is the behaviour this whole function exists to stop.
        """
        floor_ms = self._workflow.config.polling.interval_ms
        if self._usage_reset_at is None:
            return floor_ms
        remaining = (self._usage_reset_at - self._now()).total_seconds()
        return max(int(remaining * 1000), floor_ms)

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

        Three escapes call it: ``blocked_escape``, ``budget_escape`` and ``unapproved_escape``
        (GHSA-jm8h-q3j6-p8xp). The last only ever answers ``applied`` or ``failed``; an issue
        it handed back is a human's in the same sense, and the one relabel that brings it
        back -- a maintainer's ``todo`` -- starts from a cleared chain like the others.
        """
        if outcome in ("applied", "skipped"):
            self._ledger.cleared(identifier)
            # And the issue is now a human's. The conflict bounce reads this so it cannot
            # move it straight back to `rework`, which would re-dispatch what the escape has
            # just stopped and spend one of `agent.max_conflict_reworks` on it.
            self._ledger.mark_escaped(identifier)

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
    if old.triage_template != new.triage_template:
        changed.append("triage prompt")
    if old.parent_review_template != new.parent_review_template:
        changed.append("parent review prompt")
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
