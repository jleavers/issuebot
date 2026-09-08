# Phase 4: Orchestrator

Status: Draft for review (2026-09-03)

Parent: [Issuebot phased design](2026-09-02-issuebot-phased-design.md), Phase 4.
Builds on: [Phase 1: Foundations](2026-09-02-phase-1-foundations-design.md),
[Phase 2: GitHub adapter](2026-09-02-phase-2-github-adapter-design.md) and
[Phase 3: Agent runner](2026-09-03-phase-3-agent-runner-design.md).
This spec owns the detail of Phase 4 only; the architecture (§2.1, §2.2), the
label state machine (§2.3), the run lifecycle (§2.4), the security posture
(§2.9) and the configuration schema (§2.11) live in the parent. Symphony
section references (§7, §8, §14, §16, §17.4) point at the Symphony spec.

## 1. Goal

The long-running worker: poll GitHub, claim `todo` and `rework` issues, run
worker sessions concurrently up to a slot limit, retry failures with backoff,
escalate exhausted runs, follow human label moves, turn merged pull requests
into `complete`, survive restarts, and stop cleanly. After this phase
`docker compose up worker` (or `issuebot worker` in the foreground) takes an
issue from `todo` to a pull request and `review` without a human typing
anything, which is the first dogfooding milestone.

In scope: the `issuebot.orchestrator` package (runtime state and pure rules,
GitHub-writing actions, the orchestrator loop); `issuebot worker
[--workflow PATH]`; the compose `worker` service becoming real; a bounded
hardening task on Phase 3 internals that the orchestrator's correctness
depends on; two edits to the dogfood `WORKFLOW.md`.

Out of scope: PostgreSQL (Phase 6), the dashboard (Phase 7), Slack (Phase 5),
and the Claude Code GitHub Action for automated pull-request review, which
the roadmap listed as a Phase 4 repository chore and which is now deferred
(§12, decision 12).

Frozen inputs, used as they are: `run_session`, `RunResult`,
`WorkspaceManager`, `ClaudeRunner`, `TurnObserver`, `TurnEvent`,
`SessionRecord`/`session.json`, `issuebot.events` (`EVENT_KINDS` is
unchanged) and the Phase 2 `GitHubAdapter` protocol. The hardening task (§10)
changes internals of `workspace.py` and `runner.py` without touching a
signature.

## 2. Layout after this phase

```
compose.yaml                    worker runs ["worker"], init, restart, stop_grace_period 120s
WORKFLOW.md                     + hooks.after_create: guarded git fetch --unshallow; attempt wording
src/issuebot/
├── cli.py                      + worker
├── agent/runner.py             _terminate kills the group after the leader exited; pre-set cancel
├── agent/workspace.py          reuse needs .git and .issuebot; OSError -> workspace_error
└── orchestrator/
    ├── __init__.py             re-exports
    ├── state.py                RunningEntry, RetryEntry, BlockedContext, ClaudeTotals, Counters,
    │                           RuntimeSnapshot (+ rows), backoff_ms, sort_candidates,
    │                           claimed_snapshot, observe_transition, constants
    ├── actions.py              claim, blocked_block, blocked_escape, finish_terminal,
    │                           remove_workspace
    └── orchestrator.py         Orchestrator, RunObserver, preflight, OrchestratorStartupError
tests/
├── fakes/claude                + `orphan` scenario (grandchild keeps stdout open)
├── test_orchestrator_state.py
├── test_orchestrator_actions.py
├── test_orchestrator.py        the Symphony §17.4 matrix and one end-to-end run
├── test_cli.py                 + worker
├── test_agent_workspace.py     + reuse marker, OSError conversion
├── test_agent_runner.py        + orphaned grandchild, pre-set cancel
└── test_workflow_default.py    + after_create hook, attempt wording
```

`issuebot.orchestrator` depends on `agent`, `github`, `config`, `events` and
`log` only (roadmap §2.1). Nothing else imports `orchestrator` except
`cli`. No new dependency; no settings change (the orchestrator consumes
`polling.interval_ms`, `agent.max_concurrent_agents`, `agent.max_attempts`,
`agent.max_retry_backoff_ms`, `claude.stall_timeout_ms` and `hooks.timeout_ms`,
all of which exist).

## 3. Runtime model

One asyncio task owns every piece of scheduling state (Symphony §7.4). Workers
are child tasks that call the frozen `run_session` and report back through a
queue; they never touch the orchestrator's dictionaries. All timing goes
through an injectable monotonic clock, so backoff, stall and grace rules are
tested without sleeping.

```
running:  dict[issue_id, RunningEntry]   a worker task exists
retries:  dict[issue_id, RetryEntry]     a retry is due at a known time
claimed = running.keys() | retries.keys()   derived, never stored
```

Symphony's `completed` set is bookkeeping only and becomes a counter. The
loop:

```
startup()                       preflight, probes, terminal sweep
repeat until stopped:
  tick()                        reconcile, reload, preflight, fetch, dispatch, snapshot
  wait until the next poll deadline, firing retries as they come due and
  handling worker exits and refresh requests as they arrive
shutdown()                      cancel workers, wait, drain
```

## 4. State (`state.py`)

Pure data and pure functions. No I/O, no adapter.

```python
RetryKind = Literal["continuation", "failure", "escape", "slots"]
StopCause = Literal["stalled", "moved", "closed", "missing", "shutdown"]

CONTINUATION_DELAY_MS = 1_000
BACKOFF_BASE_MS = 10_000
TERMINAL_SWEEP_EVERY_TICKS = 10
REVIEW_GRACE_TICKS = 1


def backoff_ms(attempt: int, max_backoff_ms: int) -> int:
    """min(10000 * 2 ** (attempt - 1), max_backoff_ms); attempt is the one about to run."""


def sort_candidates(issues: Iterable[Issue]) -> list[Issue]:
    """in_progress first, then rework, then todo; within a rank oldest created_at, then number."""


def claimed_snapshot(issue: Issue, labels: GitHubLabels) -> Issue:
    """The issue as it looks after set_state(IN_PROGRESS): state labels replaced, rest kept."""


def observe_transition(previous: Issue, current: Issue) -> list[Event]:
    """Events for what changed between two snapshots of one open issue (§4.2)."""
```

### 4.1 Records

```python
@dataclass(kw_only=True)
class RunningEntry:
    issue: Issue  # latest snapshot
    attempt: int
    rework: bool
    resumed: bool
    run_id: str
    started_mono: float
    started_at: datetime
    cancel: asyncio.Event
    task: asyncio.Task[RunResult] | None = None
    session_id: str | None = None  # from the observer's session_started
    last_activity_mono: float | None = None
    last_activity_at: datetime | None = None
    last_event: str | None = None  # "turn_activity:Read", "turn_completed", ...
    turns: int = 0
    stop_cause: StopCause | None = None
    stop_detail: str | None = None
    review_seen_tick: int | None = None
    terminal_issue: Issue | None = None  # the closed snapshot, when stop_cause == "closed"

    @property
    def issue_id(self) -> str: ...
    @property
    def identifier(self) -> str: ...

    def stop(self, cause: StopCause, detail: str) -> None:
        """Record the first cause only, then set the cancel event."""


@dataclass(frozen=True, kw_only=True, slots=True)
class BlockedContext:
    """What the blocked escape writes; also carried by an escape retry."""

    reason: str  # one sentence, also the Blocked event's reason
    run_id: str
    attempt: int
    turns: int
    log_dir: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class RetryEntry:
    issue_id: str
    identifier: str
    issue_number: int
    issue_url: str
    attempt: int
    kind: RetryKind
    due_mono: float
    due_at: datetime
    error: str | None
    escape: BlockedContext | None = None  # kind == "escape": what to write when it fires


@dataclass(frozen=True, kw_only=True, slots=True)
class ClaudeTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    seconds_running: float = 0.0  # completed runs; the snapshot adds active runs

    @property
    def total_tokens(self) -> int: ...
    def add(self, result: RunResult) -> ClaudeTotals: ...


@dataclass(frozen=True, kw_only=True, slots=True)
class Counters:
    runs_started: int = 0
    runs_ended: int = 0
    issues_completed: int = 0
    issues_cancelled: int = 0
    blocked: int = 0

    def bump(self, **deltas: int) -> Counters: ...
```

Snapshot rows and the snapshot (Symphony §13.3 with `claude_totals` in place of
`codex_totals`; the shape of the future `GET /api/v1/state`):

```python
@dataclass(frozen=True, kw_only=True, slots=True)
class RunningRow:
    issue_number: int
    identifier: str
    title: str
    url: str
    state: str | None  # StateLabel value of the latest snapshot
    attempt: int
    rework: bool
    resumed: bool
    run_id: str
    session_id: str | None
    started_at: datetime
    last_activity_at: datetime | None
    last_event: str | None
    turns: int
    stop_cause: StopCause | None


@dataclass(frozen=True, kw_only=True, slots=True)
class RetryRow:
    issue_number: int
    identifier: str
    url: str
    attempt: int
    kind: RetryKind
    due_at: datetime
    error: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class RuntimeSnapshot:
    at: datetime
    workflow_path: str
    workflow_mtime_ns: int
    config_valid: bool
    config_error: str | None
    poll_interval_ms: int
    max_concurrent_agents: int
    tick_count: int
    last_tick_at: datetime | None
    running: tuple[RunningRow, ...]
    retrying: tuple[RetryRow, ...]
    totals: ClaudeTotals  # seconds_running includes the elapsed time of active runs
    counters: Counters

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe: datetimes as ISO 8601, enums as values, tuples as lists."""
```

Tokens and cost per running row are not live: `TurnEvent` carries no usage,
and the frozen interfaces are not extended for it. They reach `totals` when
the run ends. Symphony's `rate_limits` field is omitted.

### 4.2 `observe_transition`

Both snapshots describe the same issue and `current` is open; callers hand a
closed snapshot to `finish_terminal` instead, which publishes the terminal
events itself. Returned in order:

| Change | Event |
|---|---|
| `previous.state != current.state` and `current.state is REVIEW` and `previous.state is IN_PROGRESS` | `StateChanged(from, to, actor="agent", pr_url=current.linked_pr.url or None)` |
| any other state change | `StateChanged(from, to, actor="human")` |
| `previous.linked_pr is None` and `current.linked_pr is not None` | `PrOpened(pr_number, pr_url)` |

`from`/`to` are the raw label names (`state_labels[0]` when exactly one,
else `None`). The orchestrator publishes its own `StateChanged(actor=
"issuebot")` for the transitions it makes and updates the entry's snapshot
first, so a self-made move is never re-observed as somebody else's.

## 5. Actions (`actions.py`)

Async functions that write to GitHub and publish events. They take the
adapter, the bus and plain records, never the orchestrator. All are tested
against `FakeGitHub`.

```python
async def claim(adapter: GitHubAdapter, bus: EventBus, issue: Issue) -> Issue | None:
    """set_state(IN_PROGRESS), publish StateChanged(actor="issuebot"), return claimed_snapshot;
    None (logged: dispatch_claim_failed) on GitHubError."""


def blocked_block(context: BlockedContext, now: datetime, labels: GitHubLabels) -> str: ...


EscapeOutcome = Literal["applied", "skipped", "failed"]
FinishOutcome = Literal["complete", "cancelled", "unchanged", "failed"]


async def blocked_escape(
    adapter: GitHubAdapter,
    bus: EventBus,
    issue_id: str,
    context: BlockedContext,
    *,
    now: datetime,
) -> EscapeOutcome:
    """Roadmap §1's blocked escape: applied, skipped (no longer needed), or failed (GitHubError)."""


async def finish_terminal(
    adapter: GitHubAdapter,
    bus: EventBus,
    workspaces: WorkspaceManager,
    issue: Issue,
) -> FinishOutcome:
    """A closed issue: complete or cancelled (events published), unchanged (already complete),
    or failed (GitHubError); the workspace is removed in every case."""


async def remove_workspace(workspaces: WorkspaceManager, identifier: str) -> bool:
    """workspaces.remove with AgentError contained (logged: workspace_remove_failed)."""
```

**`claim`.** `from_label` is `issue.state_labels[0]` when exactly one state
label is present, else `None`; `to_label` is the configured `in_progress`
name. The returned snapshot is `claimed_snapshot(issue, labels)`: the worker
sees the label it will find on its first refresh without a second request.

**`blocked_escape`.**

1. `fetch_issues_by_ids([issue_id])`. Missing, closed, or `state is not
   IN_PROGRESS`: return `"skipped"` (the world moved on; nothing to do).
2. `find_workpad_comment(number)`. When found and its body already contains
   the line ``Run `<run_id>` `` the block is already there (a previous
   attempt appended it and then failed at step 3); skip to step 3. Otherwise
   append `"\n\n" + blocked_block(...)` with `update_comment`. When no
   workpad exists, `comment(number, WORKPAD_MARKER + "\n\n" +
   blocked_block(...))`.
3. `set_state(number, REVIEW)`.
4. Publish `StateChanged(from=in_progress name, to=review name,
   actor="issuebot", pr_url=linked PR url or None)` and
   `Blocked(reason=context.reason)`. Return `"applied"`.

A `GitHubError` at any step logs `blocked_escape_failed` and returns
`"failed"`; the caller retries with backoff and counts only `"applied"`
escapes. The block:

```markdown
### Issuebot blocked (2026-09-03T14:02:11Z)

Turn budget exhausted: 5 turns in attempt 2 without reaching `issuebot/review`.
Run `20260903T135501Z-a1b2c3` (attempt 2, 5 turns); logs: `/workspaces/issuebot-42/.issuebot/runs/20260903T135501Z-a1b2c3`.
Moved to `issuebot/review` for a human to look at.
```

Appended to the end of the workpad it lands under the template's
`### Blockers` heading. Reasons the orchestrator produces:

- `Turn budget exhausted: <turns> turns in attempt <n> without reaching `<review>`.`
- `<max_attempts> consecutive worker sessions failed; last error: <category>: <message>.`
  (a stall reads `stalled: no activity for <n> s`).

**`finish_terminal`.** An issue whose `state is COMPLETE` already is
`"unchanged"` (no writes, no events). Otherwise `classify_closed(issue)`:

- `complete`: `set_state(number, COMPLETE)` and publish
  `StateChanged(from, complete name, actor="issuebot", pr_url)` and
  `IssueCompleted(pr_url)`.
- `cancelled`: `clear_state(number)` and publish `StateChanged(from, None,
  actor="issuebot")` and `IssueCancelled(reason="closed without a merged
  pull request")`.

Then `remove_workspace(workspaces, identifier)` in every case, including
after a `GitHubError` (`"failed"`: the issue is closed either way; the label
write is retried by the next sweep). Log `issue_finished` with the outcome.

## 6. Orchestrator (`orchestrator.py`)

```python
class OrchestratorStartupError(Exception):
    problems: list[str]


def preflight(settings: Settings, *, which: Callable[[str], str | None]) -> list[str]:
    """Problems that block dispatch: claude.command or gh not on PATH, github.token unset."""


class RunObserver:  # satisfies TurnObserver
    def __init__(self, entry: RunningEntry, *, clock, now) -> None: ...
    def on_turn_event(self, event: TurnEvent) -> None: ...


class Orchestrator:
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
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_snapshot: Callable[[RuntimeSnapshot], None] | None = None,
    ) -> None: ...

    @property
    def workflow(self) -> Workflow: ...  # the current (last good) workflow
    @property
    def running(self) -> Mapping[str, RunningEntry]: ...
    @property
    def retries(self) -> Mapping[str, RetryEntry]: ...

    async def run(self) -> None: ...  # startup, loop, shutdown; raises OrchestratorStartupError
    def request_refresh(self) -> None: ...
    def request_stop(self) -> None: ...
    def snapshot(self) -> RuntimeSnapshot: ...

    # steps, public so tests drive them directly
    async def startup(self) -> None: ...
    async def tick(self) -> None: ...
    async def reconcile(self) -> None: ...
    async def terminal_sweep(self) -> None: ...
    async def fire_due_retries(self) -> None: ...
    async def handle_worker_exit(self, issue_id: str) -> None: ...
    async def shutdown(self) -> None: ...
```

`RunSessionFn` is the signature of `issuebot.agent.run_session`. Every
factory defaults to the real implementation; the CLI passes its own
`_adapter_factory`, `_run_session` and `_which` seams through so the
existing test substitutions keep working. The orchestrator's own log lines
carry `issue_number` and `issue_identifier` as explicit fields; the
contextvars binding that `run_session` does stays inside each worker task.

### 6.1 `RunObserver`

Called by the runner from inside the worker task, on the same event loop.
Every event sets `entry.last_activity_mono = clock()`,
`entry.last_activity_at = now()` and `entry.last_event` (`kind`, plus
`:<tool_name>` or `:<message_type>` for `turn_activity`). `session_started`
records `entry.session_id`; `turn_completed`, `turn_failed` and
`turn_timeout` set `entry.turns = event.turn_number`. It never raises past
the runner's `_Emitter` isolation and holds no other state.

### 6.2 Startup

1. `preflight(settings)`; then through the adapter `auth_status()` and
   `missing_labels()`. Any problem (a failed probe is a problem with the
   `GitHubError` message; missing labels read `labels missing: a, b; run
   issuebot labels ensure`) raises `OrchestratorStartupError(problems)`.
   Symphony §6.3: startup validation fails startup. `issuebot validate`
   remains the full diagnostic; this is the minimum to poll and launch.
   Since #17 the Claude login is probed too (`claude auth status --json`
   through the `claude_auth` seam): a definite "not logged in" is a problem
   (`claude auth: not logged in; run claude auth login or set
   ANTHROPIC_API_KEY`); an unreadable or ambiguous answer only warns.
2. Log `orchestrator_started` (repo, poll interval, slots, max turns and
   attempts, stall timeout, workspace root; never the token).
3. The first tick runs immediately (Symphony §8.1); its reconcile carries
   the startup terminal sweep of Symphony §8.6 (§6.5 Part C), whose failure
   only warns.

### 6.3 Tick

In this order (Symphony §8.1 with the roadmap's additions):

1. `reconcile()` (§6.5); its Part C runs the terminal sweep when
   `tick_count % TERMINAL_SWEEP_EVERY_TICKS == 0`, `tick_count` being the
   number of ticks completed so far, so the first tick sweeps.
2. Reload: `stat` the workflow path; when `st_mtime_ns` differs from the
   current workflow's `source_mtime_ns`, `load_workflow(path)`. Success
   replaces the workflow, rebuilds the adapter and the workspace manager
   from the new settings, logs `workflow_reloaded` (INFO, with the changed
   top-level sections), and clears `config_error`. A `ConfigError` (or a
   failed stat) keeps the last good workflow, sets `config_error`, and logs
   `workflow_reload_failed` at ERROR once per distinct failure message. Running
   workers keep the `Workflow` they were dispatched with; the new poll
   interval and slot count apply at once; the prompt and settings apply to
   the next dispatch (Symphony §6.2).
3. `preflight(settings)`; problems log `dispatch_preflight_failed` at ERROR
   (once per distinct message) and skip steps 4 to 6.
   *Amended by #20:* with preflight clean, an authentication hold set by a
   failed run (§6.8) also skips steps 4 to 6. The hold re-probes
   `claude auth status --json` through the `claude_auth` seam once per tick;
   a verdict of `ok` or `ambiguous` clears it and logs
   `dispatch_auth_recovered`, anything else keeps it and logs
   `dispatch_auth_held` (ERROR once per distinct error, DEBUG thereafter).
   An `unreadable` answer holds, unlike at startup: a run has already failed
   to authenticate, so no answer is not an answer to resume on.
4. `fetch_issues_by_states([IN_PROGRESS, REWORK, TODO])`; a `GitHubError`
   logs `candidates_fetch_failed` at WARNING and skips steps 5 and 6.
   *Amended by Phase 6 (spec §8.1):* with an `on_issues` observer attached the
   fetch covers `REVIEW` as well (`OBSERVED_STATES`), and every successful
   fetch the orchestrator makes (this one, reconcile's refresh, the terminal
   sweep, a fired retry's refresh) is handed to `on_issues`, exceptions logged
   as `issues_consumer_failed` and swallowed.
5. Candidates: `dispatchable`, `state in ACTIVE_STATES`, not claimed.
   `sort_candidates`. Every `in_progress` candidate is an orphan (no claim
   in this process): a crash, a restart, or a human who applied the label
   by hand.
6. Dispatch (§6.4) while `max(max_concurrent_agents - len(running), 0) > 0`.
7. `tick_count += 1`, `last_tick_at = now()`, log `tick_finished` at DEBUG
   (running, retrying, dispatched, slots), call `on_snapshot(snapshot())`
   with exceptions logged and swallowed.

### 6.4 Dispatch

```python
async def _dispatch(self, issue: Issue, *, attempt: int, resume_session_id: str | None) -> bool
```

1. `rework = issue.state is REWORK`. When `issue.state is not IN_PROGRESS`,
   `claim(...)`; `None` aborts the dispatch (not claimed; the next tick or
   retry sees the issue again). Roadmap Phase 4: the label is set before the
   worker starts and a failure to set it aborts.
2. The tick decides `attempt` and `resume_session_id` for an `in_progress`
   candidate with `_resume_plan(issue)`: when `workspaces.read_session(path)`
   returns a record for the same `issue_number` whose `last_outcome` is
   `None` (a run was in flight when the previous process died) or
   `"cancelled"` (the previous process stopped it: shutdown or stall), the
   plan is `attempt = record.attempt`, `resume_session_id =
   record.session_id`, `resumed = True`; the Claude transcript is intact in
   both cases. Any other record (`succeeded`, `failed`, `timed_out`) or no
   record means a fresh session at attempt 1; a `path_for` failure skips the
   candidate for this tick (logged `dispatch_skipped`; Phase 4 ruling R2).
   `todo` and `rework` candidates are always fresh; retries (§6.7) never
   resume.
3. Create the `RunningEntry` (`run_id = new_run_id()`, `started_* = clock()
   / now()`, a fresh `cancel` event), spawn the worker task, register its
   done-callback (posts `WorkerExited(issue_id)` to the queue), store the
   entry in `running`, drop any `retries` entry, bump `runs_started`, log
   `dispatched` (attempt, rework, resumed, run id, slots left).

The worker task:

```python
async def _worker(self, entry: RunningEntry, workflow: Workflow) -> RunResult:
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
```

`attempt` follows Symphony §12.3: it is the retry attempt, 1 on a fresh or
continuation dispatch and n on the nth consecutive failure retry; a normal
exit resets it. `agent.max_attempts` compares against it.

### 6.5 Reconcile

**Part A, stall** (Symphony §8.5): for each running entry with
`claude.stall_timeout_ms > 0`, `elapsed = clock() - (last_activity_mono or
started_mono)`; above the timeout, `entry.stop("stalled", f"no activity for
{elapsed:.0f} s")` and log `reconcile_stalled`. The cancel event makes the
runner terminate `claude` (SIGTERM, ten seconds, SIGKILL) and `run_session`
run `after_run`; the exit handler schedules the retry. This clock covers
workspace creation, hooks and the between-turn refresh, which the runner's
silence timeout does not, and it is the reason the setting is
orchestrator-side.

**Part B, label refresh**: nothing running is a no-op (no request).
Otherwise one `fetch_issues_by_ids(running ids)`; a `GitHubError` logs
`reconcile_refresh_failed` at WARNING and keeps every worker. Per entry:

| Refreshed snapshot | Action |
|---|---|
| open, `in_progress`, dispatchable | publish `observe_transition(entry.issue, current)` (that is, `PrOpened` when a PR appeared); `entry.issue = current` |
| open, `review` | first sight: publish `observe_transition` (`StateChanged(actor="agent")`), `entry.issue = current`, `review_seen_tick = tick_count`; seen at an earlier tick and still running: `stop("moved", "review")` |
| open, `todo`, `rework`, unlabelled, more than one state label, or not dispatchable | publish `observe_transition`, `entry.issue = current`, `stop("moved", <state or "unlabelled">)`; no cleanup |
| closed | `entry.terminal_issue = current` (always, even when another cause already stopped the entry), `stop("closed", ...)`; `finish_terminal` runs when the worker has exited (§6.8), never while `claude` may still be writing to the workspace. A later refresh that returns the issue open again clears `terminal_issue`, so the exit releases the issue instead of finishing it (Phase 4 ruling R13) |
| not returned | `stop("missing", ...)`; no cleanup |

The one-tick grace for `review` (decided 2026-09-03) exists because the
agent itself sets `review` as the last step of its completion bar and then
writes its final message; the worker's own between-turn refresh normally
ends the run inside the grace window, and killing the turn there would cut
the transcript and lose the turn's cost. A human move to `review` still
stops the worker within two intervals. Every other move stops it at the
tick that sees it. *Amended by Phase 6 (spec §8.2):* the grace is one poll
interval measured on the monotonic clock (`RunningEntry.review_seen_mono`),
not one tick, so a `NOTIFY`-driven tick cannot cut it short;
`REVIEW_GRACE_TICKS` is gone.

**Part C**: `terminal_sweep()` (§6.6) on the ticks §6.3 step 1 names.

### 6.6 Terminal sweep

`fetch_terminal_issues()`; a `GitHubError` logs `terminal_sweep_failed` at
WARNING. For each closed issue that is not in `running` (those are handled
by Part B): `finish_terminal` when `state is not COMPLETE`, otherwise
`remove_workspace` only, which is a no-op when the directory is gone. This
is what turns a merged pull request into `complete` for an issue nobody is
running (`review` is not an active state, so no worker observes it). The
every-tenth-tick cadence (five minutes at the default interval) bounds the
cost of a query that returns every completed issue the repository has; a
`since` filter on the adapter is noted under Later.

### 6.7 Retries

`_schedule(issue, *, attempt, kind, delay_ms, error, escape=None)` replaces
any existing entry for the issue (Symphony §8.4) and logs `retry_scheduled`
(kind, attempt, due in ms, error). Delays:

| Kind | Delay |
|---|---|
| `continuation` | `CONTINUATION_DELAY_MS` (1 s) |
| `failure` | `backoff_ms(attempt, agent.max_retry_backoff_ms)`: 20 s for attempt 2, 40 s for 3, capped |
| `escape` | `backoff_ms(attempt, ...)` with the entry's attempt counting escape tries from 1: 10 s, 20 s, ..., capped, unbounded in count |
| `slots` | `polling.interval_ms` |
| `auth` (#20) | `polling.interval_ms` |

`fire_due_retries()` handles every entry with `due_mono <= clock()`, oldest
due first, logging `retry_fired`:

1. Pop it. `kind == "escape"`: `blocked_escape(...)` with the stored context
   (it refreshes the issue itself); `"failed"` re-schedules kind `escape`
   with `attempt + 1`. Done either way, before any refresh.
2. *Amended by #20:* an authentication hold re-schedules the same attempt as
   kind `auth` after `polling.interval_ms`, before any refresh, so a due
   retry cannot claim what a tick would not.
3. `fetch_issues_by_ids([issue_id])`; a `GitHubError` re-schedules the same
   kind and attempt after `polling.interval_ms` with error `retry refresh
   failed: <message>` (never counted as an attempt).
4. Missing: release (log `retry_released`, reason `missing`).
5. Closed: `finish_terminal`; release.
6. Not active, or not dispatchable: release (reason `not_active`).
7. Active and no slot free: re-schedule kind `slots`, same attempt, error
   `no available orchestrator slots` (Symphony §8.4 step 4; the attempt is
   not incremented so a busy worker can never push an issue into the
   blocked escape).
8. Active: `_dispatch(issue, attempt=entry.attempt if issue.state is
   IN_PROGRESS else 1, resume_session_id=None)`. A human who moved the issue
   back to `todo` or to `rework` gets a fresh attempt 1.

Retry entries do not survive a restart (Symphony §14.3).

### 6.8 Worker exit

`handle_worker_exit(issue_id)` pops the entry, adds the `RunResult` to the
totals and `seconds_running`, bumps `runs_ended`, logs `worker_exited`
(outcome, stop reason, cause, turns, cost) and then:

| Condition | Action |
|---|---|
| `entry.terminal_issue` is set | `finish_terminal(entry.terminal_issue)`; release |
| `outcome == "succeeded"` and `final_issue` is set and open, whatever the stop cause | publish `observe_transition(entry.issue, final_issue)`, then continue with the rows below (amended by Phase 5, see below) |
| task cancelled by `task.cancel()` (shutdown last resort), or `stop_cause in ("moved", "missing", "shutdown", "closed")` | release (`closed` reaches here only when a later refresh cleared the terminal snapshot: the reopen edge of §6.5) |
| `stop_cause == "stalled"` | as failed, error `stalled: <detail>` |
| task raised (not cancelled) | log `worker_crashed` with the traceback; treat as failed with error `worker crashed: <exc>` |
| `outcome == "succeeded"`, `stop_reason == "max_turns"`, `final_state is IN_PROGRESS` | blocked escape with reason `Turn budget exhausted ...`; on `"failed"`, schedule kind `escape`, attempt 1 |
| `outcome == "succeeded"` otherwise | schedule `continuation`, attempt 1 |
| failed, `entry.attempt < max_attempts` | schedule `failure`, `attempt + 1`, error `<category>: <message>` |
| failed, `entry.attempt >= max_attempts` | blocked escape with reason `<n> consecutive worker sessions failed; last error: ...`; on `"failed"`, schedule kind `escape`, attempt 1 |
| failed with `error_category == "auth_failed"` (#20), whatever the attempt | log `dispatch_auth_failed`, hold dispatch (§6.3), blocked escape with reason `Claude could not authenticate in attempt <n> ...`; on `"failed"`, schedule kind `escape`, attempt 1 |

"Failed" covers `failed`, `timed_out` and a `cancelled` outcome the
orchestrator did not cause. The escape's `BlockedContext` carries the run
id, attempt, turns and `str(result.log_dir)`; a successful escape bumps
`blocked` and logs `blocked_escape_applied`. Releasing means the issue is
neither running nor retrying; the next tick treats it like any other.

*Amended by Phase 5 (spec §7):* a `succeeded` result whose `final_issue` is
set and open publishes `observe_transition(entry.issue, final_issue)` right
after the `terminal_issue` row and before every release row, so an agent's
move to `review` that lands while the worker is being stopped (shutdown, or
a human move seen by reconcile) still reaches the bus; the continuation row
no longer publishes it itself, and the `max_turns` escape may be preceded by
a `PrOpened`.

The continuation retry after a normal exit is Symphony §7.1's re-check: it
re-fetches one second later and releases when the issue is no longer active
(the common case, `review`), or dispatches a fresh attempt 1 when a human
moved it back to `todo`.

### 6.9 The loop, refresh and stop

```
run():
  await startup()                              OrchestratorStartupError propagates
  try:
    while not stopping:
      await tick()
      deadline = clock() + poll_interval_ms / 1000
      while not stopping:
        await fire_due_retries()
        if clock() >= deadline: break
        timeout = min(deadline, earliest retry due or +inf) - clock()
        if timeout <= 0: continue
        message = await wait_for(queue.get(), timeout)   TimeoutError: continue
        WorkerExited -> await handle_worker_exit(id); Refresh -> break; Stop -> break
  finally:
    await shutdown()
```

`request_refresh()` sets a pending flag and posts `Refresh` unless one is
already pending (coalesced); the loop clears the flag when it breaks to a
tick. `request_stop()` sets `stopping` and posts `Stop`. Both are
synchronous and meant for the event-loop thread; another thread uses
`loop.call_soon_threadsafe`. Phase 6 wires `LISTEN issuebot_refresh` to
`request_refresh()`.

`shutdown()` (Symphony has no equivalent; roadmap Phase 4 "graceful
shutdown"): log `shutdown_started`; `entry.stop("shutdown", ...)` for every
running entry (the runner SIGTERMs `claude`, `run_session` runs `after_run`
and publishes `RunEnded`); `asyncio.wait` on the tasks with timeout
`hooks.timeout_ms / 1000 + TERMINATE_GRACE_S + 10`; stragglers get
`task.cancel()` and are awaited, logged `shutdown_timeout`; every finished
task goes through `handle_worker_exit` (which releases on
`stop_cause == "shutdown"`); retries are dropped; log `orchestrator_stopped`
with the counters. Issues stay `in_progress` so the next process resumes
them from `session.json`. *Amended by Phase 6 (spec §8.3):* `shutdown()` ends
with the same `on_snapshot` publish the tick makes, so the stored snapshot of
a stopped worker shows nothing running.

### 6.10 `snapshot()`

Builds `RuntimeSnapshot` from the current state: rows from `running` and
`retries` (retries ordered by due time), `totals` with
`seconds_running + sum(clock() - started_mono)` over running entries,
`config_valid = config_error is None`. Synchronous, no I/O, safe to call
from any coroutine on the loop thread.

## 7. CLI: `issuebot worker [--workflow PATH]`

1. Load the workflow (`[FAIL] workflow: ...`, exit 2).
2. `bus = EventBus([LogSink()])`; `Orchestrator(workflow, bus=bus,
   adapter_factory=_adapter_factory, run_session=_run_session, which=_which)`
   through a module-level `_orchestrator_factory` seam.
3. `asyncio.run(_run_worker(orchestrator))`: install `SIGTERM` and `SIGINT`
   handlers that call `request_stop()` (`NotImplementedError` from
   `add_signal_handler` is ignored: not a Linux host), then `await
   orchestrator.run()`.
4. `OrchestratorStartupError`: one `[FAIL] startup: <problem>` line per
   problem, exit 1. A clean stop exits 0. Everything else goes to the
   structured log; `--log-format console` is the readable choice at a
   terminal.

The default poll interval and the rest come from the workflow; there are no
worker-specific flags. `validate` is unchanged (twelve checks).

## 8. Compose and Dockerfile

`compose.yaml` `worker`: `command: ["worker"]`, `init: true` (a reaper for
grandchildren that outlive a killed `claude`), `restart: unless-stopped`,
`stop_grace_period: 120s` (longer than the shutdown wait so Docker never
SIGKILLs a worker that is still running `after_run`). The comment about
Phase 4 goes. The Dockerfile keeps `CMD ["validate"]` as the safe default for
a bare `docker run`; compose overrides it.

## 9. Dogfood `WORKFLOW.md`

Two edits, mirrored into the scratch copy for the live check:

1. Front matter gains

   ```yaml
   hooks:
     after_create: |
       if [ "$(git rev-parse --is-shallow-repository)" = true ]; then git fetch --unshallow; fi
   ```

   so the self-review's `git diff origin/HEAD...HEAD` and a rework's merge
   of the default branch always have a merge base (carry-over 2; the
   built-in clone stays shallow per Phase 3 decision 9). The guard matters:
   a `--depth 1` clone of a repository whose default branch has a single
   commit is not shallow, and an unconditional `git fetch --unshallow`
   then fails with "--unshallow on a complete repository does not make
   sense", failing every workspace creation.
2. The follow-up context block's first line becomes `This is attempt
   {{ attempt }} for this issue: the previous worker session failed or was
   cut short, and issuebot dispatched a fresh session.` because `attempt`
   now means the retry attempt (§6.4), never a continuation.

`test_workflow_default.py` asserts the hook is configured and the attempt
wording renders for `attempt=2`.

## 10. Hardening of Phase 3 internals

One task at the start of the plan; no signature changes.

| Item | Change | Test |
|---|---|---|
| Reuse marker (carry-over 1) | `create_or_reuse` reuses only when both `path/.git` and `path/.issuebot` are directories; `.issuebot` is created as the **last** creation step, after `after_create`. Anything else at the path is a remnant: removed and recloned | `.git` without `.issuebot` is recreated; `after_create` observes no `.issuebot` yet; the existing reuse test still passes |
| `OSError` (carry-over 6) | `remove()` wraps `shutil.rmtree` (and a plain-file remnant `unlink`) in `AgentError("workspace_error")`; `root.mkdir` and the final `.issuebot` mkdir likewise, with the directory removed on failure as for any other creation failure | a file where the root should be; a hook that creates a file named `.issuebot`; `rmtree` patched to raise |
| Group kill (carry-over 5) | `_terminate` always ends with `os.killpg(pid, SIGKILL)` under `suppress(ProcessLookupError)`, including when `returncode` is already set; the leader is only SIGTERMed and waited for when it is still running | fake `claude` scenario `orphan`: emits init, starts `sleep 30` in the same session with stdout inherited, writes the grandchild pid to `CLAUDE_FAKE_PIDFILE`, exits 0; with a 500 ms silence timeout the turn ends `turn_timeout` and the grandchild is gone |
| Pre-set cancel (carry-over 5) | `run_turn` returns `cancelled` (`exit_code None`, no `process_exit` event, nothing spawned, no log directory created) when `cancel.is_set()` right after the workspace preflight | with `CLAUDE_FAKE_RECORD` set, no record file and no log directory appear |

The Phase 3 spec's §5.3 and §7.5 gain a one-line "amended by Phase 4"
note pointing here.

## 11. Testing

All hermetic: `FakeGitHub`, `tests/fakes/claude`, `tests/fakes/gh`,
`tmp_path`; hooks through `("bash", "-c")`; no network, no real `gh`,
`claude` or login shell. Tests that spawn the fakes are `skipif win32`.

The orchestrator harness: `FakeGitHub` preseeded with labels; a `Workflow`
written to `tmp_path / "WORKFLOW.md"` (so reload tests can rewrite it);
a fake clock (`FakeClock.advance(seconds)`) and a fixed `now`; a scripted
`run_session` whose every call registers a `PendingRun` (the kwargs it
received and a future) that the test completes with a `RunResult` of its
choosing, so worker exits happen exactly when the test says; a recording
sink on the bus; `which` returning a path for everything. Step methods are
called directly; `run()` is used by a few loop tests with
`interval_ms=1000`.

| File | Covers |
|---|---|
| `test_orchestrator_state.py` | `backoff_ms` 1 → 10 s, 2 → 20 s, 3 → 40 s, cap at `max_retry_backoff_ms`; `sort_candidates` rank (in_progress, rework, todo), oldest first, number tie-break; `claimed_snapshot` replaces only the state labels; `observe_transition` agent review with `pr_url`, human moves, no change, `PrOpened`, both at once; `ClaudeTotals.add`; `Counters.bump`; `RuntimeSnapshot.to_dict` survives `json.dumps` |
| `test_orchestrator_actions.py` | `claim` calls `set_state`, publishes `StateChanged` with the right labels and actor, returns the claimed snapshot; `GitHubError` → `None`, no event; `blocked_block` content; `blocked_escape` appends to an existing workpad (one `update_comment`, body ends with the block), creates the workpad when missing (`comment`, body starts with the marker), sets `review`, publishes `StateChanged` and `Blocked`; no-op when the issue is `review`/closed/missing; idempotent when the block for that run id is already present (no second append, label still set); `GitHubError` on the comment write → `"failed"` and no `set_state`; `finish_terminal` complete (label, both events, workspace removed), cancelled (labels cleared, both events, workspace removed), already complete (no `set_state`, removal still attempted), `GitHubError` → `"failed"` and removal still attempted; `remove_workspace` contains `AgentError` |
| `test_orchestrator.py` | **dispatch**: rank order and slot limit across two ticks; `dispatchable=false` and non-active states skipped; claimed issues (running and retrying) skipped; claim failure aborts without a worker and the next tick dispatches; worker kwargs (`rework`, `attempt=1`, `run_id`, `cancel`, observer, no resume); orphan with `last_outcome=None` resumes (`resume_session_id`, `attempt` from the record, `resumed`), other records dispatch fresh; workspace path error skips. **exits and retries**: `issue_moved` to `review` → `StateChanged(agent)` from the final snapshot and a continuation entry due in 1 s, which releases when fired; `issue_moved` to `todo` → continuation fires into a fresh attempt 1; failure → attempt 2 due in 20 s with the error text, then attempt 3 in 40 s, cap honoured; `max_attempts` reached → escape (label, workpad block naming the error, `Blocked`, counter); `max_turns` while `in_progress` → escape immediately, no retry; escape `GitHubError` → kind `escape` due in 10 s, fires and succeeds; slots full when a retry fires → kind `slots`, same attempt, due in one interval; retry refresh failure → requeued unchanged; retry finds the issue closed → `finish_terminal`; missing → released; crashed worker → failure retry and `worker_crashed` logged. **reconcile**: nothing running makes no request; refresh failure keeps workers; `in_progress` refresh updates the snapshot and publishes `PrOpened`; `review` grace (marked on tick n, cancelled on tick n+1, exit releases without cleanup, one `StateChanged(agent)`); `todo` cancels at once with `StateChanged(human)`; closed with a merged PR cancels, and the exit completes the issue and removes the workspace; closed without a PR cancels and strips labels; missing cancels and releases; stall after `stall_timeout_ms` of silence → cancel and failure retry with `stalled` in the error; `stall_timeout_ms: 0` disables. **sweep**: on the first tick and every tenth after it, never in between; `review` issue closed by a merged PR → `complete`, `IssueCompleted`, workspace gone; already complete → no `set_state`; a failing terminal fetch only warns. **reload and preflight**: interval and slot changes apply on the next tick; a template change reaches the next dispatch's workflow; an invalid rewrite keeps the last good config, sets `config_error`, logs once; a missing `claude` skips dispatch while reconcile still runs. **loop**: `run()` with a 1 s interval ticks, `request_refresh()` forces an early tick, `request_stop()` returns from `run()`; shutdown sets every cancel event, waits for the exits, leaves labels alone, releases. **startup**: preflight problem, auth failure and missing labels each raise `OrchestratorStartupError` with a message that names the fix. **snapshot**: rows, totals including active seconds, counters, `to_dict`. **end to end** (`skipif win32`): real `run_session`, `WorkspaceManager` on `tmp_path` with the fake `gh`, `ClaudeRunner` on the fake `claude` (`success`), `max_turns=1`, `after_run` hook writing a marker file, `interval_ms=1000`: a `todo` issue is claimed, the run ends `max_turns`, the escape moves it to `review` with a workpad comment, `run_started`/`run_ended`/`state_changed`/`blocked` are all on the bus, then `request_stop()` returns |
| `test_cli.py` | `worker`: unloadable workflow → 2; `OrchestratorStartupError` → `[FAIL] startup:` lines and 1; a stub orchestrator whose `run()` sends `SIGTERM` to the process sees `request_stop()` called and the command exits 0 (`skipif win32`); the factory receives the CLI's adapter factory and `run_session` seams |
| `test_agent_workspace.py`, `test_agent_runner.py`, `test_workflow_default.py` | the §10 rows; the §9 edits |

## 12. Decisions made in this phase

1. **One orchestrator task, a message queue and a deadline-driven wait**;
   no per-retry timer handles, no separate poller and supervisor tasks. An
   injectable monotonic clock makes every timing rule testable without
   sleeping.
2. **`claimed` is derived** from `running` and `retries`; Symphony's
   separate set is unnecessary when every mutation happens in one task.
3. **Reuse requires `.git` and `.issuebot`**, and `.issuebot` is created
   last (carry-over 1).
4. **A guarded `git fetch --unshallow` lives in `hooks.after_create`** of
   the dogfood workflow; the built-in clone stays shallow (carry-over 2).
5. **The blocked escape appends a dated block to the workpad, then sets
   `review`**, creating the workpad when it is missing, idempotent per run
   id, retried with backoff on failure and never re-dispatching
   (carry-over 3).
6. **A failed resume counts as a failed attempt**; retries never pass
   `resume_session_id`, so resume is tried at most once per orphan
   (carry-over 4). An orphan is resumed when its last recorded outcome is
   `None` (crash) or `cancelled` (stopped by the previous process), never
   after a run that ended on its own.
7. **Stall detection is orchestrator-side through the observer**, measured
   over the whole run; the runner's group kill and pre-set cancel check are
   fixed in the hardening task (carry-over 5).
8. **`remove()` and the mkdirs raise `workspace_error`**, never a raw
   `OSError` (carry-over 6).
9. **`attempt` is the retry attempt** (Symphony §12.3): 1 on fresh and
   continuation dispatches, incremented only by consecutive failures,
   compared against `max_attempts`; the prompt's wording follows.
10. **Backoff uses the attempt about to run**: `min(10000 * 2^(attempt-1),
    max_retry_backoff_ms)`, so the first failure retry waits 20 s; slot
    requeues keep the attempt and wait one poll interval; escape retries
    count from 1 and are unbounded.
11. **Orphaned `in_progress` issues sort first**, then `rework`, then
    `todo`; a restart resumes work before starting new work.
12. **The Claude Code GitHub Action is deferred** (carry-over 7, decided
    2026-09-03): it needs an `ANTHROPIC_API_KEY` repository secret and,
    unguarded, a missing secret fails every PR check, which the agent's
    completion bar would treat as blocking. It becomes a chore issue
    issuebot can take once it is dogfooding.
13. **`review` gets a one-tick grace** before reconciliation cancels the
    worker; every other move out of `in_progress` cancels at the tick that
    sees it. (Phase 6 makes it one poll interval in time; §6.5.)
14. **Workspaces are removed only after the worker has exited**, whichever
    path decided the issue is terminal.
15. **The terminal sweep runs on the first tick and every tenth tick**;
    running issues are reconciled by id every tick.
16. **Startup probes `auth_status` and `missing_labels`** and fails on
    either; the per-tick preflight is local (executables, token).
17. **`observe_transition` infers the actor**: `in_progress` to `review` is
    the agent, every other observed move is a human, and the orchestrator's
    own moves are published by the action that makes them.
18. **`RunOutcome` `stalled` stays unused by events**: `run_session` reports
    the cancellation it saw; the orchestrator's log line and retry error
    say `stalled`.
19. **No new settings**; `claude.stall_timeout_ms <= 0` disables stall
    detection as in Symphony.
20. **The live check runs the worker in the foreground** under the
    operator's subscription login; the container path needs an API key and
    is exercised by `docker compose build` plus `validate` only.

## 13. Done when

- `uv run pytest -q` passes (no network, no real `gh` or `claude`); ruff and
  pre-commit clean; CI green; `docker compose build` succeeds.
- `uv run issuebot validate` still reports twelve checks for the committed
  `WORKFLOW.md`.
- Live check from the developer host with `export GH_TOKEN=$(gh auth token)`
  and no `ANTHROPIC_API_KEY`, against `jleavers/issuebot-scratch` (issue #1
  in `review`, PR #2 open, `~/issuebot-scratch/WORKFLOW.md` updated with the
  §9 edits):
  1. `uv run issuebot --log-format console worker --workflow
     ~/issuebot-scratch/WORKFLOW.md` starts, logs `orchestrator_started`,
     and its first tick dispatches nothing (issue #1 is in `review`).
  2. A second issue labelled `issuebot/todo` (a trivial change with a test)
     is claimed within one poll interval, runs to a pull request whose body
     contains `Closes #<n>`, and reaches `issuebot/review` by the agent's
     own label change; the log shows `dispatched`, `run_started`,
     `state_changed` with `actor=agent`, `run_ended`, `retry_scheduled`
     (continuation) and `retry_released`.
  3. Merging PR #2 on GitHub makes the next terminal sweep (within five
     minutes, or at once after a restart) set `issuebot/complete` on issue
     #1, publish `issue_completed`, and remove
     `~/issuebot-workspaces/issuebot-scratch-1`.
  4. `SIGTERM` (Ctrl-C) while idle stops the worker within a second with
     `orchestrator_stopped`; `SIGTERM` during a run stops it within the
     shutdown wait, `after_run` having run, `session.json` recording
     `cancelled`, and the issue stays `in_progress`; restarting the worker
     resumes that issue from `session.json` (`dispatched` with
     `resumed=true`) and the agent picks up from the workpad.
- `CLAUDE.md` describes `issuebot.orchestrator` and `worker`; `README.md`
  lists `worker` and says `docker compose up` runs it; the roadmap's Phase 4
  section notes the deferred review action; the Phase 3 spec carries the
  §10 amendment notes.
