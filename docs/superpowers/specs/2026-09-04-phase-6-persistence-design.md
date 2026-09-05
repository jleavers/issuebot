# Phase 6: Persistence

Status: Draft for review (2026-09-04)

Parent: [Issuebot phased design](2026-09-02-issuebot-phased-design.md), Phase 6 (§2.7 for
the four tables, decision 8 for raw SQL with psycopg 3 and numbered migrations).
Builds on: [Phase 1: Foundations](2026-09-02-phase-1-foundations-design.md) (the event bus
and sink protocol), [Phase 4: Orchestrator](2026-09-03-phase-4-orchestrator-design.md)
(`request_refresh()`, `RuntimeSnapshot.to_dict()`, the `review` grace) and
[Phase 5: Slack notifications](2026-09-03-phase-5-slack-notifications-design.md) (the sink
pattern this phase copies). This spec owns the detail of Phase 6 only; the architecture
(§2.1, §2.2), the label state machine (§2.3), the security posture (§2.9) and the
configuration schema (§2.11) live in the parent.

## 1. Goal

History survives restarts and the numbers the dashboard needs can be queried. A PostgreSQL
database, when configured, receives every event the bus carries, one row per worker
session, the latest snapshot of every tracked issue and the worker's runtime snapshot, and
the worker never waits on it: a lost connection is logged and retried in the background
while the orchestrator keeps polling. `issuebot stats` answers "how many issues closed and
how many agents ran in the last day and week, and per day"; `issuebot status` shows what
the worker was doing at its last tick; a `NOTIFY issuebot_refresh` from any client makes
the worker tick at once.

In scope: the `issuebot.db` package (connection helpers, numbered `.sql` migrations and
their runner, the write store, the `PostgresSink`, the refresh listener, the query module
whose return types are Phase 7's view models, and a `Database` facade the CLI goes
through); four CLI commands (`migrate`, `status`, `stats`, `refresh`); a real
`database.url` check in `validate`; the sink and listener wired into `worker`, the sink
into `run-once`; one dependency (`psycopg[binary]`); three bounded amendments to the Phase
4 orchestrator (an `on_issues` observer, the `review` grace measured in time, a final
snapshot at shutdown) and one to the Phase 1 events (`RunEnded.log_dir`).

Out of scope (roadmap): HTTP, the dashboard and `/api/v1/*` (Phase 7). Decided here: no
connection pool (one connection per sink, listener and CLI command; Phase 7 adds
`psycopg_pool` for the web process if it needs one); no per-issue log viewer; no retention
or pruning of `events`; no reload hook for `database.url` (a change needs a restart, as
for the webhook). None of the Phase 4 or Phase 5 parked follow-ups is adopted; the three
Phase 4 amendments below are this phase's own needs.

Amended 2026-09-04 (Phase 7): the per-issue log viewer is Phase 7's, backed by a `run_turns`
table (migration `0002_run_turns`) that the sink fills from a run's turn files when it drains
`run_ended`; the web process uses this facade one connection per request and no pool was
added.

Frozen inputs, used as they are: `issuebot.agent` except the one-line `RunEnded.log_dir`
population in `run_session`, `issuebot.github`, `EventBus`/`EventSink`, `EVENT_KINDS` (no
kind is added; §4 explains why), `DatabaseSettings` (no field is added; every knob that is
not in §2.11 is a module constant), `issuebot.notifications`.

## 2. Layout after this phase

```
pyproject.toml, uv.lock             + psycopg[binary]>=3.3 (the phase's one new dependency)
compose.yaml                        comment: worker applies migrations at start
src/issuebot/
├── cli.py                          + migrate, status, stats, refresh; _database_factory seam;
│                                     _database_check; _open_database, _build_sinks; sink and listener lifetime
├── events/types.py                 + RunEnded.log_dir: str | None = None
├── agent/session.py                run_session fills RunEnded.log_dir
├── orchestrator/
│   ├── orchestrator.py             + on_issues, OBSERVED_STATES, review grace in time,
│   │                                 snapshot published at the end of shutdown()
│   └── state.py                    RunningEntry.review_seen_mono replaces review_seen_tick;
│                                     REVIEW_GRACE_TICKS removed
└── db/
    ├── __init__.py                 re-exports
    ├── connection.py               connect, describe, redact, error_text, classify, is_postgres_url,
    │                               reconnect_delay, constants
    ├── errors.py                   DatabaseError, StoreUnavailableError, StoreError, MigrationError
    ├── migrations/0001_initial.sql the four tables and their indexes
    ├── migrate.py                  Migration, discover_migrations, schema_version, apply_migrations,
    │                               migrate, MigrationResult
    ├── store.py                    Store protocol, IssueSnapshot, PostgresStore (the SQL writes)
    ├── sink.py                     PostgresSink (queue, one drain task, reconnect, close)
    ├── listen.py                   RefreshListener, REFRESH_CHANNEL
    ├── queries.py                  view models and Queries (the reads)
    └── database.py                 Database facade: migrate, probe, queries, store, listener,
                                    notify_refresh
tests/
├── conftest.py                     + db_url fixture (skips without DATABASE_URL; one schema per test)
├── test_db_connection.py           hermetic: describe, redact, error_text, classify, URL check, backoff
├── test_db_migrate.py              hermetic: discovery and ordering; DB: apply, idempotent, ahead
├── test_db_store.py                DB: every write rule of §5
├── test_db_sink.py                 hermetic: the sink with a FakeStore
├── test_db_listen.py               hermetic: fake connection; DB: a real NOTIFY round trip
├── test_db_queries.py              DB: every query of §6 against seeded rows
├── test_db_database.py             hermetic: description, seams, redacted failures; DB: probe
├── test_events.py                  + RunEnded.log_dir
├── test_agent_session.py           + run_ended carries the log directory
├── test_orchestrator.py            + on_issues, observed states, grace in time, shutdown snapshot
└── test_cli.py                     + the four commands, the validate check, worker and run-once wiring
```

Amended (Phase 7): `migrations/0002_run_turns.sql` adds the `run_turns` table (the captured
turn files, capped, with a parsed summary); the package ships two migrations and the schema
version is 2.

`issuebot.db` imports `config`, `events`, `github` (models and `role_for`) and `log` only.
`orchestrator` and `agent` never import it; `cli` wires it. The sink accepts the runtime
snapshot through a structural type (`at` plus `to_dict()`), so `db` does not import
`orchestrator` either.

## 3. Data model and migrations

### 3.1 Tables

One migration, `0001_initial.sql`, creates the four tables of roadmap §2.7. Timestamps are
`timestamptz`; every connection sets its session time zone to UTC (§7), so `date_trunc`
and `::date` mean UTC days.

Amended (Phase 7): a second migration, `0002_run_turns.sql`, creates `run_turns` (Phase 7
spec §3.1), so the schema now has five tables at version 2.

```sql
CREATE TABLE issues (
    number        integer PRIMARY KEY,
    identifier    text NOT NULL,
    title         text NOT NULL,
    state         text,                 -- StateLabel value (todo, in_progress, ...) or NULL
    state_label   text,                 -- the raw label name, or NULL
    github_state  text NOT NULL,        -- open | closed
    url           text NOT NULL,
    labels        text[] NOT NULL DEFAULT '{}',
    pr_number     integer,
    pr_url        text,
    pr_state      text,                 -- open | closed | merged
    pr_merged_at  timestamptz,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL,
    closed_at     timestamptz,
    seen_at       timestamptz NOT NULL  -- when this snapshot was observed (§5)
);
CREATE INDEX issues_state_idx ON issues (state, updated_at DESC);
CREATE INDEX issues_closed_at_idx ON issues (closed_at) WHERE closed_at IS NOT NULL;

CREATE TABLE runs (
    run_id           text PRIMARY KEY,
    issue_number     integer NOT NULL,
    issue_identifier text NOT NULL,
    attempt          integer NOT NULL DEFAULT 0,
    session_id       text,
    started_at       timestamptz NOT NULL,
    ended_at         timestamptz,
    outcome          text,              -- NULL while running
    error            text,
    turns            integer NOT NULL DEFAULT 0,
    input_tokens     bigint NOT NULL DEFAULT 0,
    output_tokens    bigint NOT NULL DEFAULT 0,
    cost_usd         double precision NOT NULL DEFAULT 0,
    duration_s       double precision,
    workspace_path   text,
    log_dir          text
);
CREATE INDEX runs_started_at_idx ON runs (started_at DESC);
CREATE INDEX runs_issue_idx ON runs (issue_number, started_at DESC);

CREATE TABLE events (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at           timestamptz NOT NULL,
    kind         text NOT NULL,
    issue_number integer,
    run_id       text,
    payload      jsonb NOT NULL         -- Event.to_dict(), kind and at included
);
CREATE INDEX events_at_idx ON events (at DESC);
CREATE INDEX events_issue_idx ON events (issue_number, at DESC);

CREATE TABLE runtime_snapshot (
    id         boolean PRIMARY KEY DEFAULT true CHECK (id),  -- exactly one row
    at         timestamptz NOT NULL,    -- RuntimeSnapshot.at
    written_at timestamptz NOT NULL,    -- now() at the write; the dashboard's "snapshot age"
    data       jsonb NOT NULL           -- RuntimeSnapshot.to_dict()
);
```

`issues.state` holds the role (`StateLabel` value) so the Kanban groups without knowing
the configured label names; `state_label` keeps the name for display. Events keep the
whole `to_dict()` payload, so a new event field never needs a migration.

### 3.2 Migrations (`migrate.py`)

```python
@dataclass(frozen=True, slots=True)
class Migration:
    version: int  # 1, 2, ... from the file name prefix
    name: str  # "initial"
    sql: str


@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied: tuple[str, ...]  # "0001_initial", ...
    version: int  # schema version after the run


def discover_migrations() -> tuple[Migration, ...]:
    """Every migrations/NNNN_name.sql, sorted; versions must run 1, 2, 3 ... without gaps."""


async def schema_version(conn: AsyncConnection) -> int:
    """max(version) in schema_migrations, 0 when the table does not exist."""


async def apply_migrations(conn: AsyncConnection) -> MigrationResult:
    """Apply every pending migration in one transaction under an advisory lock."""


async def migrate(url: str) -> MigrationResult:
    """connect, apply_migrations, close; errors as MigrationError (message redacted)."""
```

- Files live in the package (`importlib.resources.files("issuebot.db") / "migrations"`);
  hatchling and the editable install both ship them, so the container needs nothing extra.
  The name pattern is `^(\d{4})_([a-z0-9_]+)\.sql$`; anything else in the directory, a gap
  or a duplicate version is a `MigrationError` at discovery time.
- `schema_migrations (version integer PRIMARY KEY, name text NOT NULL, applied_at
  timestamptz NOT NULL DEFAULT now())` is created by the runner with `CREATE TABLE IF NOT
  EXISTS`, never by a migration file.
- `apply_migrations` runs one transaction: `SELECT pg_advisory_xact_lock(<constant>)` so
  the worker and, in Phase 7, the web process can both migrate at start without racing;
  create the bookkeeping table; read the applied versions; a recorded version above the
  highest known file is `MigrationError("schema version N is newer than this issuebot
  knows (M)")`, a recorded name that differs from the file's is `MigrationError` too;
  execute each pending file's SQL and insert its row. PostgreSQL DDL is transactional, so
  a failing migration leaves the schema where it was.
- No down migrations, no ORM, no Alembic (roadmap decision 8).

## 4. Delivery model: how data reaches the sink

Three kinds of data reach the database, through two channels:

| Data | Channel | Why |
|---|---|---|
| Events (`state_changed`, `run_started`, ..., `notification_sent`) | the event bus: `PostgresSink.handle(event)` | discrete facts every sink wants |
| Polled issue snapshots | an injected callback: `Orchestrator(on_issues=sink.record_issues)` | bulk and periodic |
| The runtime snapshot | the existing callback: `Orchestrator(on_snapshot=sink.record_snapshot)` | bulk and periodic |

A poll result is not an event. The `LogSink` writes every event's fields at INFO, so an
`IssuesPolled` event carrying fifty issues would put fifty issues into the log every
thirty seconds, and the Slack sink would have to learn to ignore it. Phase 4 already
chose the callback shape for the snapshot (`on_snapshot`); `on_issues` is its twin.
`EVENT_KINDS` does not grow.

```
bus.publish(event) ──▶ sink.handle: enqueue ────────────────────┐
orchestrator tick  ──▶ sink.record_issues: merge into the       │   one FIFO queue, ≤ QUEUE_LIMIT
                       pending issues marker                     ├──▶ one drain task: connect,
orchestrator tick  ──▶ sink.record_snapshot: replace the slot   │   write, reconnect with backoff
                                                                 ┘
```

- **`handle`** returns at once. It appends `_EventItem(event)` to an unbounded
  `asyncio.Queue`. When `qsize() >= QUEUE_LIMIT` (1000) the event is dropped, `dropped` is
  incremented and `db_queue_full` is logged at WARNING with the kind and issue number.
  Items published before `start()` are buffered; after `close()` they count as `dropped`
  (logged at DEBUG).
- **`record_issues(issues)`** stamps every issue with `now()` (injectable) as its
  `seen_at`, merges the batch into the sink's pending `dict[int, IssueSnapshot]` (a later
  snapshot of the same number replaces an earlier one) and, when no `_IssuesMarker` is
  queued, enqueues one. The marker is never dropped by the cap.
- **`record_snapshot(snapshot)`** stores `(snapshot.at, snapshot.to_dict())` in a slot,
  replacing whatever was there, and enqueues a `_SnapshotMarker` when none is queued.

So during an outage the queue holds every event up to the cap plus at most two markers,
and the database receives the latest issue snapshots and the latest runtime snapshot when
the connection is back, never a replay of stale ones. Order between events and the
markers does not matter because every `issues` write is guarded by its observation time
(§5): applying an older event after a newer poll is a no-op.

- **The drain task** (`start()` creates it) takes one item at a time and applies it
  through the `Store` (§5): `apply_event` for an event item; `upsert_issues` with the
  pending dict (taken and cleared at that moment) for the issues marker; `write_snapshot`
  with the slot's value for the snapshot marker. Before the first item, and whenever a
  write raised `StoreUnavailableError`, it (re)connects: `store.connect()` with delays
  `RECONNECT_DELAYS_S = (1, 2, 4, 8, 16, 30)` and 30 s from then on, logging
  `db_connect_failed` (redacted error, delay) per attempt and `db_connected` (the URL
  without its password, the attempt count) on success; `reconnects` counts the successes
  after the first. An item whose write raised `StoreUnavailableError` is retried after the
  reconnect, not dropped (`db_write_retry` at WARNING). An item whose write raised
  `StoreError` (a statement failed for a reason a retry would not fix) is logged
  `db_write_failed` at WARNING and counted as `failed`; any other exception is logged
  `db_write_crashed` with the traceback and counted the same way; the loop moves on and
  the task never dies on its own. Every successful write bumps `written`. Every wait
  goes through an injectable `sleep`.
- **`close()`** puts a sentinel on the queue and waits up to `DRAIN_TIMEOUT_S` (10) for the
  drain task to reach it. On timeout the task is cancelled, `db_drain_timeout` is logged
  with the number of items left, the item in flight counts as `failed` (logged
  `db_write_cancelled`) and the leftovers as `dropped`. The store's connection is closed either way, and `db_sink_closed` is logged
  with `written`, `failed`, `dropped` and `reconnects`. The CLI closes the sink after the
  orchestrator's shutdown has finished, so the final snapshot (§8.3) and the shutdown's
  `run_ended` events are written; ten seconds fits inside the compose `stop_grace_period`
  next to the Slack sink's own ten.

The sink does no I/O in `handle`, `record_issues` or `record_snapshot`, never raises to
the bus, and never blocks the orchestrator. It publishes nothing.

## 5. The store (`store.py`): what each write does

```python
@dataclass(frozen=True, slots=True)
class IssueSnapshot:
    issue: Issue
    seen_at: datetime


class Store(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def apply_event(self, event: Event) -> None: ...
    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None: ...
    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None: ...


class PostgresStore:
    def __init__(self, url: str, *, labels: GitHubLabels, connect: Connector = connect) -> None: ...
```

Amended (Phase 7): `apply_event(event, turns: Sequence[TurnCapture] = ())` also inserts the
captured turns into `run_turns` in the `run_ended` transaction (`ON CONFLICT (run_id,
turn_number) DO UPDATE`, so a retried item is idempotent); the sink reads the files once, in a
thread, before the item's first write attempt.

`PostgresStore` holds one `psycopg.AsyncConnection` (autocommit; §7). Every method maps
`psycopg.OperationalError` and `psycopg.InterfaceError` to `StoreUnavailableError` and any other
`psycopg.Error` to `StoreError` (`classify`, §7), a `TypeError` or `ValueError` from value
adaptation to `StoreError` too, every message passed through `redact` and cut to its first
line. A method called while not connected raises `StoreUnavailableError`; `connect()`
replaces any previous connection, broken or not.

**`apply_event(event)`**, one transaction:

1. `INSERT INTO events (at, kind, issue_number, run_id, payload)` for every event;
   `issue_number` from `IssueEvent`, `run_id` from the payload when present, `payload =
   event.to_dict()` as `jsonb`.
2. Then, by kind:

| Kind | Second statement |
|---|---|
| `run_started` | `INSERT INTO runs (run_id, issue_number, issue_identifier, attempt, session_id, started_at, workspace_path) ... ON CONFLICT (run_id) DO UPDATE SET` those columns (`started_at = event.at`; an empty `workspace_path` is stored as NULL) |
| `run_ended` | `INSERT INTO runs (run_id, issue_number, issue_identifier, started_at, ended_at, outcome, error, turns, input_tokens, output_tokens, cost_usd, duration_s, log_dir) ... ON CONFLICT (run_id) DO UPDATE SET` the ended columns only (`ended_at = event.at`; the insert branch computes `started_at = at - duration_s` so a run whose start was dropped still has a row) |
| `state_changed` | `UPDATE issues SET state = $role, state_label = $to_label, seen_at = $at WHERE number = $n AND seen_at <= $at`; `$role` is `role_for(labels, to_label)` (`None` when `to_label` is `None` or unknown) |
| `issue_completed`, `issue_cancelled` | `UPDATE issues SET github_state = 'closed', closed_at = COALESCE(closed_at, $at), seen_at = $at WHERE number = $n AND seen_at <= $at` |
| every other kind | nothing more |

The two run upserts touch disjoint column sets, so `run_ended` before `run_started` gives
the same row as the other order. The `issues` updates change no other column and affect
zero rows when the issue has never been polled; the next poll fills it in.

**`upsert_issues(snapshots)`**, one transaction, one `INSERT ... ON CONFLICT (number) DO
UPDATE SET <every column> WHERE issues.seen_at <= EXCLUDED.seen_at` per snapshot
(`executemany`). `state` is `issue.state.value` or NULL, `state_label` is
`issue.state_labels[0]` when exactly one, `labels` the tuple as `text[]`, the four `pr_*`
columns from `linked_pr` or NULL.

**`write_snapshot(at, data)`**: `INSERT INTO runtime_snapshot (id, at, written_at, data)
VALUES (true, $1, now(), $2) ON CONFLICT (id) DO UPDATE SET at = EXCLUDED.at, written_at =
now(), data = EXCLUDED.data`.

The `seen_at` guard is what makes §4's coalescing safe: a poll snapshot and a
`state_changed` event for the same issue can be applied in either order, and the row ends
up reflecting whichever observation was later. Within one tick the fetch precedes the
claim, so the claim's `StateChanged.at` is later than the poll's `seen_at` and wins.

## 6. Queries and view models (`queries.py`)

Frozen dataclasses, Phase 7's view models; a `Queries` object bound to one connection.

```python
@dataclass(frozen=True, kw_only=True, slots=True)
class IssueRow:  # every column of issues, same names and types


@dataclass(frozen=True, kw_only=True, slots=True)
class RunRow:  # every column of runs


@dataclass(frozen=True, kw_only=True, slots=True)
class EventRow:
    id: int
    at: datetime
    kind: str
    issue_number: int | None
    run_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True, kw_only=True, slots=True)
class DailyPoint:
    day: date
    closed: int
    runs: int


@dataclass(frozen=True, kw_only=True, slots=True)
class SnapshotRow:
    at: datetime
    written_at: datetime
    data: dict[str, Any]


COMPLETE_LIMIT = 50


class Queries:
    def __init__(self, conn: AsyncConnection) -> None: ...

    async def closed_count(self, window: timedelta) -> int:
        """issues with state = 'complete' and closed_at >= now() - window."""

    async def runs_count(self, window: timedelta) -> int:
        """runs with started_at >= now() - window."""

    async def daily_series(self, days: int) -> list[DailyPoint]:
        """One point per UTC day for the last ``days`` days (today last), zero-filled."""

    async def issues_by_state(self) -> dict[str, list[IssueRow]]:
        """Open issues with a state grouped by StateLabel value (updated_at desc), plus the
        COMPLETE_LIMIT most recently closed 'complete' issues; every role key present."""

    async def runs_for_issue(self, number: int) -> list[RunRow]:
        """Newest first."""

    async def recent_events(self, limit: int) -> list[EventRow]:
        """Newest first."""

    async def snapshot(self) -> SnapshotRow | None: ...
```

Closed counts and the closed series come from `issues.closed_at` (GitHub's own timestamp,
kept current by every poll and sweep) restricted to `state = 'complete'`; an issue closed
without a merged pull request is cancelled and does not count. Run counts and the runs
series come from `runs.started_at`. The roadmap sketched the series over `events` with
`date_trunc`; `issues` and `runs` are self-healing after an outage (the next poll rewrites
them) where a dropped event is gone, so the series read those two tables. `events` serves
`recent_events` and the per-issue timeline. `daily_series` uses `generate_series` over
`date_trunc('day', now())`, so it is UTC because the connection is.

Closed issues whose state is not `complete` (cancelled, or closed and awaiting the sweep)
and open issues without a state appear in no Kanban column.

## 7. Connection helpers (`connection.py`) and the listener (`listen.py`)

```python
CONNECT_TIMEOUT_S = 5
RECONNECT_DELAYS_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
REDACTED = "<database url>"
POSTGRES_SCHEMES = ("postgresql", "postgres")


def is_postgres_url(url: str) -> bool: ...


def describe(url: str) -> str:
    """postgresql://user@host:port/db without the password; for log lines."""


def redact(text: str, url: str) -> str:
    """Replace the full URL and, on its own, its password with REDACTED."""


def reconnect_delay(attempt: int) -> float:
    """RECONNECT_DELAYS_S[attempt - 1], the last value from then on."""


def error_text(exc: BaseException) -> str:
    """The first line of the message (libpq appends hints on further lines)."""


def classify(exc: psycopg.Error, url: str) -> DatabaseError:
    """StoreUnavailableError for OperationalError/InterfaceError, StoreError otherwise; redacted."""


async def connect(url: str) -> AsyncConnection:
    """autocommit=True, connect_timeout=CONNECT_TIMEOUT_S, application_name='issuebot',
    then SET TIME ZONE 'UTC'."""
```

`Connector` is the callable type of `connect`; the store, the listener and the facade
take it as a seam. `connect_timeout` bounds a hung server at connect time; a statement
against a server that stops answering is bounded by task cancellation (psycopg's async
operations honour it), which is what `close()`'s drain timeout does.

**`RefreshListener(url, on_notify, *, connect=connect, sleep=asyncio.sleep)`** owns its own
connection, separate from the sink's, because `notifies()` occupies a connection. `start()`
creates a task that connects, executes `LISTEN issuebot_refresh` (`REFRESH_CHANNEL`), logs
`db_listen_started`, then iterates `conn.notifies()`; every notification calls `on_notify()`
(the orchestrator's `request_refresh`, which coalesces) and bumps `notified`, logging
`db_refresh_received` at INFO. `OperationalError`/`InterfaceError` from the connect, the
`LISTEN` or the iteration logs `db_listen_lost` and reconnects with the same backoff as the
sink (`reconnects` counted). `close()` cancels the task, closes the connection and logs
`db_listen_closed` with the counters; it is idempotent and a no-op before `start()`. The
callback runs on the event loop, so it may call `request_refresh()` directly.

## 8. Phase 4 and Phase 1 amendments

### 8.1 `on_issues` and the observed states (Phase 4 §6.3, §6.5, §6.6, §6.7)

`Orchestrator(..., on_issues: Callable[[Sequence[Issue]], None] | None = None)`. After every
successful fetch the orchestrator makes, it calls `on_issues` with the fetched issues
(nothing for an empty result), exceptions logged as `issues_consumer_failed` and swallowed
(the `on_snapshot` rule):
the tick's candidate fetch, reconcile's refresh of the running issues, the terminal sweep's
closed issues, and a fired retry's refresh of its issue.

When `on_issues` is set, the tick fetches `OBSERVED_STATES = (in_progress, rework, todo,
review)` instead of `CANDIDATE_STATES`; the dispatch loop's `state in ACTIVE_STATES` filter
already ignores `review`, so scheduling is unchanged. `review` is neither active nor
terminal, so nothing else ever lists it, and the Kanban's `review` column would otherwise
only fill when a running worker's issue moved there. The cost is one more `gh api graphql`
call per tick (the adapter runs one query per label), paid only when a database is
configured.

### 8.2 The `review` grace, in time (Phase 4 §6.5, decision 13)

Phase 4 stops a running worker whose issue reached `review` at the tick after the one that
first saw it (`REVIEW_GRACE_TICKS = 1`), which at a regular cadence is one poll interval.
Once `NOTIFY issuebot_refresh` can start a tick at any moment, "the next tick" can be
milliseconds later and would kill `claude` while it writes its final message, the very
thing the grace protects. The rule becomes: `RunningEntry.review_seen_mono = clock()` at
first sight; stop when `clock() - review_seen_mono >= polling.interval_ms / 1000` (the
current setting, so a reload applies). At a regular cadence the next tick's reconcile
runs at least one interval after the mark (the deadline is set after the mark and the loop
waits for it), so behaviour there is identical; a refresh-driven tick inside the interval
leaves the worker alone. `REVIEW_GRACE_TICKS` and `review_seen_tick` go.

### 8.3 A snapshot at the end of `shutdown()` (Phase 4 §6.9)

`shutdown()` ends by calling the same `_publish_snapshot()` the tick calls, so the
`runtime_snapshot` row of a cleanly stopped worker shows no running or retrying entries
rather than the last tick's. `status` then reads truthfully; the dashboard also has
`written_at` for the age.

### 8.4 `RunEnded.log_dir` (Phase 1 §6)

`RunEnded` gains `log_dir: str | None = None` and `run_session` fills it from
`RunResult.log_dir`. The `runs` row wants the log directory (roadmap §2.7) and the event
is the right carrier: the sink should not guess `<workspace>/.issuebot/runs/<run_id>` and
be wrong for the run that never spawned a turn. The `LogSink` line gains the field; Slack's
`format_event` ignores it; `to_dict()` and the payload column carry it.

## 9. CLI

### 9.1 The `Database` facade and the seam

```python
class Database:
    """Everything the CLI does with the database, behind one object tests substitute."""

    def __init__(self, url: str, *, connect: Connector = connect) -> None: ...

    @property
    def description(self) -> str: ...  # describe(url)

    async def migrate(self) -> MigrationResult: ...
    async def probe(
        self,
    ) -> Probe: ...  # server_version, schema_version, latest_version, behind, ahead
    def queries(self) -> AbstractAsyncContextManager[Queries]: ...  # one connection
    def store(self, labels: GitHubLabels) -> PostgresStore: ...
    def listener(self, on_notify: Callable[[], None]) -> RefreshListener: ...
    async def notify_refresh(self) -> None: ...  # NOTIFY issuebot_refresh
```

`cli._database_factory = Database` is the seam, the way `_adapter_factory` and
`_slack_post` are; `tests/test_cli.py` installs a `FakeDatabase` for every test (canned
probe and migration results, a `FakeStore`, a fake listener, a fake `Queries`), so no CLI
test opens a socket.

### 9.2 Commands

All four load the workflow (`[FAIL] workflow: ...`, exit 2) for `database.url`; when it is
unset they print `[FAIL] database: not configured; export DATABASE_URL or set
database.url: $VAR` and exit 1. Any `DatabaseError` prints `[FAIL] database: <redacted
message>` and exits 1.

| Command | Output |
|---|---|
| `migrate [--workflow]` | one `[ OK ] migration NNNN_name: applied` per applied file, then `[ OK ] database: schema version N` (or `... unchanged at version N`) |
| `status [--workflow]` | the snapshot row rendered: `at` and its age, `written_at`, workflow path and config validity, tick count, poll interval, slots; a `running` table (number, identifier, attempt, turns, run id, last event, started); a `retrying` table (number, kind, attempt, due); totals and counters. No row: `no runtime snapshot yet (has the worker run against this database?)`, exit 0 |
| `stats [--workflow] [--days N]` | `closed` and `runs` for `1d` and `7d`; issue counts by state from `issues_by_state`; then the `daily_series(N)` table (`DAY CLOSED RUNS`), default 7 days |
| `refresh [--workflow]` | `NOTIFY issuebot_refresh`; `[ OK ] refresh: notified issuebot_refresh` |

`refresh` is the roadmap's CLI command (decision 12) and the way the live check exercises
the listener before Phase 7 exists. The two rendering functions (`render_status`,
`render_stats`) are pure and tested with fixed rows.

### 9.3 `validate`

The `database.url` line becomes a real check; the count stays twelve:

| State | Line |
|---|---|
| unset | `[ OK ] database.url: not configured (history and dashboard disabled)` |
| not a `postgresql://` or `postgres://` URL | `[FAIL] database.url: not a postgresql:// URL` |
| cannot connect | `[FAIL] database.url: cannot connect: <redacted error>` |
| connected, schema behind | `[WARN] database.url: connected (PostgreSQL 18.1); schema version 0 of 1; run issuebot migrate` |
| connected, schema ahead | `[FAIL] database.url: connected (PostgreSQL 18.1); schema version 2 is newer than this issuebot knows (1)` |
| connected and current | `[ OK ] database.url: connected (PostgreSQL 18.1); schema version 1` |

The probe is `Database.probe()`: `SELECT version()` (the first two words are shown) and
`schema_version`. It is network I/O, like the three `gh` probes, bounded by
`CONNECT_TIMEOUT_S`. The URL never appears in a line.

### 9.4 `worker` and `run-once`

`_open_database(settings) -> Database | None`: `None` when `database.url` is unset;
otherwise `Database(url).migrate()` (a `DatabaseError` prints `[FAIL] database: ...` and the
command exits 1 before anything else starts; a schema problem or an unreachable server is a
configuration fault the operator must see, and under compose `depends_on:
service_healthy` has already waited for the server) and log `db_migrated` (applied names,
version, the URL description). `_build_sinks(settings) -> _Sinks` calls it and returns the
bus (`EventBus([LogSink(), slack?, postgres?])`, the PostgreSQL sink being
`PostgresSink(database.store(labels))`) with the sinks and the `Database`; `_Sinks.start()`
starts every sink on the running loop before the session or the orchestrator, and
`_Sinks.close()` closes each in a `finally` after it has returned, the Slack sink first and
the database sink last.

`worker` additionally passes `on_snapshot=sink.record_snapshot` and
`on_issues=sink.record_issues` to the orchestrator factory and starts
`database.listener(orchestrator.request_refresh)` right after the orchestrator is built,
closing it before the sinks. `run-once` calls `sink.record_issues([issue])` for the issue
it fetched and again for the refreshed one after the claim, so a `run-once` against an
empty database still leaves an `issues` row for its `state_changed` to update.

The roadmap's "the compose entrypoint runs `issuebot migrate`" is met by `worker` doing it
itself: `compose.yaml` keeps `command: ["worker"]` and gains a comment; Phase 7's `web`
uses the same helper. The advisory lock makes the two safe together.

## 10. Configuration, compose, CI and the image

No new setting. `database.url` (`SecretStr | None`, resolved from `$DATABASE_URL`) is used as
it is; queue size, timeouts and backoff are constants (§4, §7). Compose already gives the
worker `DATABASE_URL=postgresql://issuebot:issuebot@db:5432/issuebot` and waits for the
`db` health check; CI's test job already runs `postgres:18` and exports `DATABASE_URL`, so
the DB tests activate there with no workflow change. The Dockerfile is unchanged:
`psycopg[binary]` ships `libpq` in its wheel (3.3.5 has a CPython 3.14 wheel), which is why
it is chosen over `psycopg[c]` or the pure package with an apt `libpq5`. Dependabot's `uv`
group will bump it.

Running the worker on the host against the compose database means exporting
`DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:<ISSUEBOT_DB_PORT>/issuebot`; the
dot-env example documents it and `README.md` shows it. On this development host port 5432
is taken, so the live check and local DB tests use `ISSUEBOT_DB_PORT=5440` (exported in
the same command as `docker compose up -d db`, or set in the dot-env file).

## 11. Security

- The database URL is a credential (it carries the password). It is held in the store,
  the listener and the facade as a private attribute and never logged: log lines carry
  `describe(url)` (scheme, user, host, port, database), and every error string that leaves
  `issuebot.db` has passed through `redact`, which removes the full URL and the password
  on its own. `validate` lines never contain it; `--show-config` masks it as a
  `SecretStr` already.
- No issue body reaches the database. Events carry issue numbers, identifiers, label
  names, GitHub URLs, run ids, paths, and the free-text `reason`/`error` fields that are
  already in the log and the workpad; `issues` carries titles and labels. The dashboard
  (Phase 7) escapes what it renders. Amended (Phase 7): from Phase 7 the database holds
  untrusted text in `run_turns` (the rendered prompt embeds the issue body; tool results embed
  repository content and command output), stored as bound parameters and escaped on render.
- Migrations are the repository's own SQL, applied under an advisory lock; no SQL is
  built from data (every value is a bound parameter).
- `psycopg` logs through the standard library and therefore through structlog's
  `ProcessorFormatter` (Phase 1 §5), so its lines carry no more than issuebot's.

## 12. Testing

All hermetic except the DB-marked files, which read `DATABASE_URL` and are skipped, and
reported as skipped by pytest's summary, when it is unset (roadmap §2.10). `conftest.py`
captures `DATABASE_URL` at import (the `clean_env` fixture clears it from the environment
for every test) and provides `db_url`: it connects, creates a schema `issuebot_test_<hex>`,
yields the URL with `options=-c search_path=<schema>` appended, and drops the schema at
teardown, so DB tests are isolated from each other and from any real data in the same
database. CI has the service container and runs them; locally `ISSUEBOT_DB_PORT=5440
docker compose up -d db` and the matching `DATABASE_URL` export activate them.

| File | Covers |
|---|---|
| `test_db_connection.py` | `is_postgres_url`; `describe` drops the password and keeps user, host, port and database; `redact` removes the URL and the bare password and leaves other text alone (and copes with no password); `reconnect_delay` 1, 2, 4, 8, 16, 30, 30; `error_text` keeps the first line; `classify` maps the two connection-level classes to `StoreUnavailableError` and the rest to `StoreError`, never leaking the URL |
| `test_db_migrate.py` | discovery finds `0001_initial`, versions are contiguous from 1, a gap or a duplicate or a stray file raises (against a temporary directory through an injectable root); DB: applying from scratch creates the four tables and `schema_migrations` with version 1; a second run applies nothing; a recorded version above the known files raises `newer than this issuebot knows`; a recorded name mismatch raises; `schema_version` is 0 on an empty schema |
| `test_db_store.py` | DB, one migrated schema per test: every §5 row: `run_started` then `run_ended` and the reverse order produce the same `runs` row; `run_ended` alone computes `started_at`; `state_changed` updates state, label and `seen_at`, maps an unknown label to NULL state, and is ignored when older than the row; `issue_completed`/`issue_cancelled` close the row and keep an existing `closed_at`; every event lands in `events` with `issue_number`, `run_id` and the full payload; `upsert_issues` inserts, updates, keeps the newer of two snapshots, stores labels and the PR columns; `write_snapshot` keeps one row and rewrites it; a write after `close()` raises `StoreUnavailableError`; a bad statement raises `StoreError` (a payload that is not JSON-serialisable) |
| `test_db_sink.py` | with a `FakeStore` (records calls; can raise `StoreUnavailableError` or `StoreError` on demand; counts connects) and a recording `sleep`: `handle` buffers before `start`, applies events in order, drops at the cap (`dropped`, log) and keeps going; `record_issues` merges by number keeping the later snapshot and stamps `seen_at`; two `record_issues` before the drain runs give one `upsert_issues` call; `record_snapshot` twice gives one `write_snapshot` with the later data; an unavailable store reconnects with the backoff and the item is retried, not lost (`reconnects`); `StoreError` counts `failed` and the next item is written; a store that raises something else is logged and skipped; `close()` drains what is queued and closes the store; `close()` times out on a hanging store (patched `DRAIN_TIMEOUT_S`), cancels the task, counts the in-flight item as failed and the rest as dropped; `close()` twice is a no-op; `start()` twice raises; events after `close()` count as dropped |
| `test_db_listen.py` | with a fake connection whose `notifies()` is fed by the test: a notification calls the callback and bumps `notified`; `LISTEN issuebot_refresh` was executed; a connection error reconnects with the backoff (`reconnects`); `close()` cancels and closes; `close()` before `start()` is a no-op. DB: `RefreshListener` on `db_url`, a `NOTIFY issuebot_refresh` from a second connection reaches the callback within two seconds |
| `test_db_database.py` | hermetic: `description` hides the password; `store()` and `listener()` carry the URL; `probe()`, `queries()` and `notify_refresh()` against a refusing connector raise `StoreUnavailableError` reading `cannot connect` without the password; `Probe.behind`/`ahead`. DB: `probe()` reports version 0 of 1 before `migrate()` and 1 after |
| `test_db_queries.py` | DB, seeded through `PostgresStore`: `closed_count` and `runs_count` for 1 d and 7 d against rows dated inside and outside the window and a cancelled closure that does not count; `daily_series(3)` zero-fills and puts today last; `issues_by_state` groups by role with every key present, orders by `updated_at`, excludes unlabelled open issues and closed non-complete ones, caps `complete`; `runs_for_issue` newest first; `recent_events` newest first and limited; `snapshot()` `None` then the row |
| `test_events.py`, `test_agent_session.py` | `RunEnded.log_dir` defaults to `None`, serialises, and `run_session` fills it with the run's log directory |
| `test_orchestrator.py` | `on_issues` receives the tick's fetch, reconcile's refresh, the sweep's closed issues and a fired retry's refresh; a raising consumer is logged and does not stop the tick; the tick fetches `review` only when `on_issues` is set and never dispatches it; the grace: marked at tick n, a refresh-driven tick 1 s later leaves the worker running, a tick one interval later stops it (`moved`, `review`); `shutdown()` publishes a final snapshot with no running entries |
| `test_cli.py` | with `FakeDatabase`: the six `database.url` lines and the count stays twelve; `migrate` prints the applied names and the version, and the failure line; `status` renders a row and the no-row message; `stats` prints the windows, the state counts and `--days`; `refresh` notifies and reports; each of the four fails with the not-configured line when the URL is unset; `worker` with `DATABASE_URL` migrates first, passes a bus with the postgres sink and `on_snapshot`/`on_issues` to the factory, starts the listener with `request_refresh` and closes everything; a migration failure prints `[FAIL] database:` and exits 1 before the orchestrator is built; `run-once` records the issue and the claim's `state_changed` reaches the fake store before the command returns; without the variable the bus is `["log"]` as today |

The existing `test_validate_configured_database_and_slack` becomes the "connected and
current" case. The Phase 4 grace test advances the fake clock by one interval instead of
counting a tick.

## 13. Decisions made in this phase

1. **Poll results and the runtime snapshot reach the sink through injected callbacks**
   (`on_issues`, the existing `on_snapshot`), not through the bus; `EVENT_KINDS` is
   unchanged. Bulk periodic data on the bus would be logged in full every tick.
2. **One FIFO queue with two coalescing markers**: events are queued in order up to a cap
   of 1000; issue snapshots merge into one pending batch and the runtime snapshot into
   one slot, so an outage never replays stale periodic data and the queue stays bounded.
3. **Every `issues` write is guarded by observation time** (`seen_at`), which makes the
   order of events and polls irrelevant and lets `state_changed`, `issue_completed` and
   `issue_cancelled` keep the row current between polls.
4. **Connection loss is retried in the background with backoff** (1, 2, 4, 8, 16, then
   30 s); the item in flight is retried, statement-level failures are dropped and counted;
   `close()` drains for at most 10 s.
5. **The tick polls `review` too, when an observer is attached**, at one extra `gh` call
   per tick; scheduling is unchanged.
6. **The `review` grace is one poll interval on the monotonic clock**, not one tick, so a
   `NOTIFY`-driven tick cannot cut it short.
7. **`shutdown()` publishes a final snapshot.**
8. **`RunEnded` carries `log_dir`** (additive, default `None`).
9. **`worker` and `run-once` migrate at start and fail fast** when the database is
   configured but unusable; an unconfigured database still means "run without one".
   Migrations run in one transaction under an advisory lock.
10. **`migrate`, `status`, `stats` and `refresh` are the CLI**; `refresh` is added to the
    roadmap's Phase 6 list because the listener needs a sender before Phase 7.
11. **`validate` connects and reports the server and schema versions**; behind warns
    (the worker migrates itself), ahead fails, unreachable fails.
12. **Counts come from `issues.closed_at` (complete only) and `runs.started_at`**, not
    from `events`, because those tables heal after an outage.
13. **`psycopg[binary]`**, one connection each for the sink, the listener and a CLI
    command; no pool until Phase 7 needs one; no ORM, no Alembic (roadmap decision 8).
14. **The `Database` facade is the CLI's single seam** for tests.
15. **No new settings**; the operational knobs are constants. A `database.url` change needs
    a restart, like the webhook.
16. **No parked Phase 4 or Phase 5 follow-up is adopted.**

## 14. Open questions for the operator

Each has a default the spec and plan follow; say so if you want the other choice.

1. Should `worker` warn and run without history, instead of failing, when
   `database.url` is set but the server is unreachable at start? (Spec: fail, exit 1.)
2. Should `closed_count` and the closed series count cancelled closures (closed without a
   merged pull request)? (Spec: `complete` only.)
3. Is a cap of 50 on the Kanban's `complete` column right for Phase 7? (Spec:
   `COMPLETE_LIMIT = 50`, a constant Phase 7 may change.)
4. Should `run-once` migrate too, or refuse to write when the schema is behind? (Spec:
   migrate, so a `run-once` against a fresh database just works.)
5. Should the tick poll `review` even without an observer, for symmetry? (Spec: only with
   one, so a worker without a database makes exactly the requests it makes today.)

## 15. Done when

- `uv run pytest -q` passes with no network and no database (the DB tests report as
  skipped) and passes in CI with the service container (no skips); ruff and pre-commit
  clean; `docker compose build` succeeds; `pyproject.toml` and `uv.lock` carry
  `psycopg[binary]` and nothing else new.
- `uv run issuebot validate` on the committed `WORKFLOW.md` prints twelve checks; with
  `DATABASE_URL` pointing at the compose database the line reads `connected (PostgreSQL
  18.x); schema version 1` after `issuebot migrate` and warns before it.
- Live check from the developer host against `jleavers/issuebot-scratch` (issue #3 in
  `review` with PR #4 rebased and mergeable; `~/issuebot-scratch/WORKFLOW.md` already has
  the repo, root, `stall_timeout_ms` and `run_ended` edits) with the compose `db` on
  `ISSUEBOT_DB_PORT=5440`, `GH_TOKEN`, `SLACK_WEBHOOK_URL` and `DATABASE_URL` exported in
  the same command and never printed:
  1. `migrate` applies `0001_initial`; `validate` reports twelve checks with the database
     line `[ OK ]`; `status` reports no snapshot yet.
  2. The worker starts detached, logs `db_migrated` (nothing applied), `db_connected` and
     `db_listen_started`; within one tick `status` shows tick 1 with nothing running; the
     first sweep's `on_issues` puts issues #1, #3 and #5 into `issues`.
  3. `issuebot refresh` makes the worker log `db_refresh_received` and tick at once.
  4. A new `todo` issue runs to a pull request and `review`; `stats` shows `runs 1d = 1`;
     `runs` has the row with `outcome = succeeded`, tokens, cost and `log_dir`; `events`
     has its `run_started`, `state_changed` (three), `pr_opened`, `run_ended` and
     `notification_sent` rows.
  5. The operator merges PR #4; the next sweep sets `complete` on #3 and `stats` shows
     `closed 1d = 1` (and `7d` includes #1 and #5 while they are within seven days).
  6. `SIGTERM`, then restart: `db_sink_closed` reports `failed=0 dropped=0`; after the
     restart `status` shows a fresh `at` within one tick and `stats` is unchanged (history
     preserved).
- `CLAUDE.md` describes `issuebot.db`, the four commands, the wiring and the restart rule;
  `README.md` mentions `DATABASE_URL`, `migrate`, `status`, `stats`, `refresh` and the host
  port; the roadmap's Phase 6 section records what was decided; the Phase 4 spec carries
  the §8 amendment notes; the Phase 1 spec notes `RunEnded.log_dir`; the dot-env example
  describes `DATABASE_URL`; `compose.yaml` says the worker migrates.
