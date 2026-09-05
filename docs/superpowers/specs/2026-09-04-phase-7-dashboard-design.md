# Phase 7: Web dashboard and the per-issue log viewer

Status: Draft for review (2026-09-04)

Parent: [Issuebot phased design](2026-09-02-issuebot-phased-design.md), Phase 7 (§2.8 for the
pages and the API, §2.9 for the security posture, §2.11 for `server.*`, decision 7 for the
server-rendered stack). Builds on: [Phase 6: Persistence](2026-09-04-phase-6-persistence-design.md)
(§6 the query module whose row types are this phase's view models, §9.1 the `Database` facade,
§9.4 `_open_database`, §13 the decisions this phase inherits) and
[Phase 4: Orchestrator](2026-09-03-phase-4-orchestrator-design.md) (`RuntimeSnapshot.to_dict()`,
the shape of `/api/v1/state`). This spec owns the detail of Phase 7 only; the architecture
(§2.1, §2.2), the label state machine (§2.3), the security posture (§2.9) and the configuration
schema (§2.11) live in the parent.

The roadmap listed a per-issue log viewer under "Later (not scheduled)" and Phase 6 §1 said
"no per-issue log viewer". This phase absorbs it: the operator asked for it on 2026-09-04, and the
storage decision it forces (§4) is the first thing this spec settles.

## 1. Goal

The blueprint's dashboard: a Kanban of the five label columns, hero stats (issues closed in 1 d
and 7 d, agents spun up in 1 d and 7 d, running now, cost), two 30-day charts, a running-agents
panel, and a page per issue with its run history, its events and the transcript of every turn the
agent ran, all served by a second process that reads the Phase 6 database and never touches
GitHub or Claude. A JSON API in the Symphony §13.7.2 shapes serves the same data; `POST
/api/v1/refresh` is the one write, a `NOTIFY issuebot_refresh`; `GET /healthz` says whether the
database answers and how old the worker's last report is.

In scope: the `issuebot.web` package (FastAPI app factory, pure view-model builders, the
transcript parser, Jinja2 templates, one stylesheet, one script, vendored htmx and Chart.js); the
turn-log capture (`issuebot.agent.turnlog`, a `run_turns` table, the sink and store additions
that fill it); five new queries; CLI `issuebot web [--port] [--bind]`; a real compose `web`
service; three dependencies (`fastapi`, `uvicorn`, and `httpx2` for tests); two bounded fixes to
Phase 6's parked list that touch this phase (`issues_by_state` on an unknown role, a ceiling on
`--days`).

Out of scope (roadmap): authentication (bind loopback or put a reverse proxy in front); write
actions on issues from the UI. Decided here: no connection pool (§10, decision 3); no retention
for `run_turns` (as for `events`); no live tail of a running turn (the running panel shows the
worker's last event and turn count; a turn's transcript appears when its run ends); no Markdown
rendering of agent output (everything is escaped text); no reload of `WORKFLOW.md` in the web
process (a change needs a restart, as for the worker's sinks).

Frozen inputs, used as they are: `issuebot.orchestrator`, `issuebot.github`, `issuebot.agent`
except the new `turnlog` module, `EVENT_KINDS` (no kind is added), `ServerSettings` (port 8080,
bind `0.0.0.0`, already present; no field is added), `issuebot.notifications`, the Phase 6
migrations (a new file is added, nothing is edited).

## 2. Layout after this phase

```
pyproject.toml, uv.lock             + fastapi, uvicorn (runtime); httpx2 (dev); a pytest filterwarnings
                                      entry for Starlette's import-time anyio deprecation
.pre-commit-config.yaml             exclude: tests/fixtures/runs/ and static/vendor/ (kept byte-for-byte)
compose.yaml                        + web service (DATABASE_URL, WORKFLOW.md read-only, loopback port,
                                      /healthz check; no env_file, no other volume)
Dockerfile                          unchanged (uv sync installs the packages; curl is already there)
src/issuebot/
├── cli.py                          + web [--workflow] [--port] [--bind]; _serve seam; stats on
│                                     state_counts; --days bounded by MAX_WINDOW_DAYS
├── agent/turnlog.py                TurnCapture, capture_turns(log_dir): file discovery, the caps,
│                                     the parsed summary; constants
├── db/
│   ├── migrations/0002_run_turns.sql
│   ├── store.py                    Store.apply_event(event, turns=()); INSERT_TURN; turn_row
│   ├── sink.py                     the drain captures the turn files once per run_ended item
│   ├── queries.py                  + TurnSummaryRow, TurnRow, MAX_WINDOW_DAYS; issue, events_for_issue,
│   │                                 turn_summaries_for_issue, turn, state_counts; issues_by_state fix
│   └── __init__.py                 re-exports
└── web/
    ├── __init__.py                 create_app and the constants
    ├── app.py                      the FastAPI app: routes, error handlers, response headers
    ├── views.py                    pure builders: state_document, stats_document, issue_document,
    │                                 describe_event, safe_href, worker_status, window_days, turn_url
    ├── transcript.py               parse_transcript(stream) -> Transcript of Blocks
    ├── templates/
    │   ├── base.html               layout, the htmx config meta tag, the script and style links
    │   ├── index.html              the dashboard page (wraps partials/dashboard.html; the charts)
    │   ├── partials/dashboard.html the live region: status, hero stats, panels, Kanban
    │   ├── issue.html              runs with their turns, events
    │   ├── turn.html               header, transcript, prompt, stderr, raw links
    │   └── error.html              status, message
    └── static/
        ├── app.css, app.js
        └── vendor/htmx.min.js, htmx.LICENSE, chart.umd.js, chart.LICENSE, README.md
tests/
├── fakes/database.py               FakeDatabase, FakeQueries, FakeStore, FakeListener (moved out of
│                                     test_cli.py; FakeQueries grows the five new methods)
├── fakes/web.py                    row builders, a clock, the TestClient harness the web tests share
├── fixtures/runs/20260904T202535Z-0964cd/turn-1.jsonl, turn-1.prompt.md, turn-1.stderr.log
├── test_agent_turnlog.py           hermetic: the sample and synthetic streams
├── test_web_transcript.py          hermetic: blocks from the sample and synthetic lines
├── test_web_app.py                 hermetic: the API and /healthz against a FakeDatabase
├── test_web_pages.py               hermetic: the pages, the partial, raw files, static, the filters
├── test_web_app_db.py              DB: seeded through PostgresStore, the real Queries
├── test_db_store.py                + run_turns
├── test_db_sink.py                 + the capture step
├── test_db_queries.py              + the five queries and the unknown-role skip
├── test_db_migrate.py, test_db_database.py   version 1 -> 2
└── test_cli.py                     + web; stats --days ceiling; stats on state_counts
```

`issuebot.web` imports `config`, `db`, `github` (`StateLabel`) and `log` only; `cli` wires it.
`issuebot.db` gains one import, `agent.turnlog` (pure file reading; the file names live next to
the runner that writes them). `orchestrator` and `agent` still never import `db` or `web`.

## 3. Data model and migration

### 3.1 `run_turns`

One migration, `0002_run_turns.sql`, one table: a row per captured turn.

```sql
CREATE TABLE run_turns (
    run_id                      text NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    turn_number                 integer NOT NULL,
    captured_at                 timestamptz NOT NULL,
    model                       text,               -- system/init .model
    subtype                     text,               -- result .subtype
    is_error                    boolean,            -- result .is_error
    num_turns                   integer,            -- result .num_turns (agent iterations)
    input_tokens                bigint,             -- result .usage.*
    cache_creation_input_tokens bigint,
    cache_read_input_tokens     bigint,
    output_tokens               bigint,
    cost_usd                    double precision,   -- result .total_cost_usd
    duration_ms                 bigint,             -- result .duration_ms
    result_text                 text,               -- result .result, first RESULT_TEXT_LIMIT chars
    prompt                      text NOT NULL,      -- turn-N.prompt.md, first PROMPT_LIMIT bytes
    prompt_bytes                integer NOT NULL,   -- the file's size
    stream                      text NOT NULL,      -- turn-N.jsonl, capped (§4.1)
    stream_bytes                integer NOT NULL,
    stream_lines                integer NOT NULL,   -- lines in the file
    omitted_lines               integer NOT NULL,   -- lines replaced by a stub
    stderr                      text NOT NULL,      -- turn-N.stderr.log, last STDERR_LIMIT bytes
    stderr_bytes                integer NOT NULL,
    truncated                   boolean NOT NULL,   -- the head cap dropped at least one line
    PRIMARY KEY (run_id, turn_number)
);
```

The summary columns are nullable because a turn that was killed or crashed never wrote a
`result` line. The `*_bytes` columns hold the original sizes so a page can say "showing 2 MiB of
7 MiB". The foreign key holds because the store inserts the turns in the same transaction as the
`run_ended` upsert of the run's row (§4.3). The schema version becomes 2; `validate` reads
`schema version 2`. No retention, as for `events` (roadmap Later).

### 3.2 What this changes in Phase 6's posture

Phase 6 §11 says no issue body reaches the database. From this phase the database holds untrusted
text: the rendered prompt embeds the issue body, and tool results embed repository content and
command output. Everything the dashboard renders is escaped (§8); nothing in the database is ever
executed or interpolated into SQL. The Phase 6 spec gets an amendment note under §11.

## 4. Delivery model: how a turn's files reach the database

The runner writes, per turn, `<workspace>/.issuebot/runs/<run_id>/turn-N.jsonl` (claude's
stream-json), `turn-N.prompt.md` (the rendered prompt) and `turn-N.stderr.log`. The worker
removes the workspace when the issue reaches `complete` or is cancelled (`finish_terminal`), so
the files of a finished issue are gone unless something keeps them; and under compose the web
process is a separate container that cannot see the worker's files without a shared volume.

Decision (§10, decision 1): the PostgreSQL sink captures the files into `run_turns` when it drains
the run's `run_ended` event. The web process reads the database only, so it works across
containers with no volume and no path coupling, keeps the logs after the issue closes, and covers
`run-once` too. An archive directory on the workspace volume (a worker behaviour change even
without `DATABASE_URL`, a volume mount, an unbounded archive) and live files only (nothing after
`complete`) were the alternatives.

```
run_session publishes RunEnded(log_dir=...) ──▶ PostgresSink.handle: enqueue (no I/O, as before)
                                                        │
drain task takes the item ──▶ capture_turns(Path(log_dir)) in a thread, once ──▶
                              store.apply_event(event, turns=captures): events row, runs upsert,
                              run_turns rows, one transaction
```

### 4.1 Capture (`agent/turnlog.py`)

```python
PROMPT_LIMIT = 256 * 1024
LINE_LIMIT = 64 * 1024
STREAM_LIMIT = 2 * 1024 * 1024
STDERR_LIMIT = 64 * 1024
RESULT_TEXT_LIMIT = 4 * 1024
OMITTED_TYPE = "issuebot_omitted"
TURN_FILE = re.compile(r"^turn-(\d+)\.jsonl$")


@dataclass(frozen=True, kw_only=True, slots=True)
class TurnCapture:
    turn_number: int
    model: str | None
    subtype: str | None
    is_error: bool | None
    num_turns: int | None
    input_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    duration_ms: int | None
    result_text: str | None
    prompt: str
    prompt_bytes: int
    stream: str
    stream_bytes: int
    stream_lines: int
    omitted_lines: int
    stderr: str
    stderr_bytes: int
    truncated: bool


def capture_turns(log_dir: Path) -> list[TurnCapture]:
    """Every turn-N.jsonl under ``log_dir`` with its prompt and stderr, capped; never raises."""
```

- Discovery: every entry matching `TURN_FILE`, ordered by the number (so `turn-10` follows
  `turn-2`). A directory that does not exist or cannot be listed yields `[]`. A turn whose stream
  file cannot be read is skipped; a missing or unreadable prompt or stderr file becomes `""` with
  `0` bytes.
- Prompt: the first `PROMPT_LIMIT` bytes, decoded as UTF-8 with replacement; `prompt_bytes` is
  the file's size.
- Stream: the file is split into lines (empty lines dropped); `stream_lines` and `stream_bytes`
  describe the file. A line longer than `LINE_LIMIT` is replaced by the stub
  `{"type": "issuebot_omitted", "original_type": <its type or null>, "bytes": <its length>}` and
  counted in `omitted_lines`. Lines are then kept from the head while the running total
  (including newlines) stays within `STREAM_LIMIT`; the first line that does not fit stops the
  head and sets `truncated`. When truncated, the last line whose type is `result` is appended if
  it was not kept (after the stub rule), so the turn's outcome is always in the stored stream.
  Unparseable lines are kept as they are (or stubbed, if oversized). The stored text is the kept
  lines joined with newlines, decoded with replacement.
- Stderr: the last `STDERR_LIMIT` bytes; `stderr_bytes` is the file's size.
- Summary: `model` from the `system`/`init` line; `subtype`, `is_error`, `num_turns`, the four
  `usage` counts, `total_cost_usd` and `duration_ms` from the last `result` line of the file (read
  before the caps); `result_text` is its `result` string cut to `RESULT_TEXT_LIMIT` characters.
  A missing line or a value of the wrong type leaves the column `None`.

The module does no logging and raises nothing; the sink logs what it got.

### 4.2 The sink (`sink.py`)

`PostgresSink(store, *, sleep=, now=, description=, capture=capture_turns)`. When the drain task
resolves an `_EventItem` whose event is a `RunEnded` with a non-empty `log_dir`, `_write` runs
`await asyncio.to_thread(capture, Path(log_dir))` once, before the first write attempt, so a
reconnect retry never re-reads files that may have been removed meanwhile. Success logs
`db_turns_captured` (run id, `turns`, `stream_bytes` summed) at INFO; an exception logs
`db_turns_capture_failed` (run id, the error) at WARNING and the event is written with no turns.
The captures travel with the work item to `store.apply_event(event, turns=captures)`. `handle`,
`record_issues` and `record_snapshot` still do no I/O; the queue, the markers, the backoff and
`close()` are unchanged. A `run_ended` whose `log_dir` is `None` (a run that never created its
workspace) is written as before.

### 4.3 The store (`store.py`)

```python
class Store(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def apply_event(self, event: Event, turns: Sequence[TurnCapture] = ()) -> None: ...

    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None: ...

    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None: ...
```

`apply_event(run_ended, turns=...)` runs, in the same transaction as the `events` insert and the
`RUN_ENDED` upsert, one `executemany` of

```sql
INSERT INTO run_turns (run_id, turn_number, captured_at, model, ..., truncated)
VALUES (%(run_id)s, %(turn_number)s, now(), %(model)s, ..., %(truncated)s)
ON CONFLICT (run_id, turn_number) DO UPDATE SET <every column but the key>
```

so a retried item is idempotent. `turns` is ignored for every other kind. The `TypeError` and
`ValueError` mapping of `_guard` covers a capture that cannot be adapted.

## 5. Queries and view models (`queries.py`)

Additions to Phase 6 §6; nothing existing changes shape.

```python
MAX_WINDOW_DAYS = 365


@dataclass(frozen=True, kw_only=True, slots=True)
class TurnSummaryRow:  # every run_turns column except prompt, stream and stderr
    run_id: str
    turn_number: int
    captured_at: datetime
    model: str | None
    subtype: str | None
    is_error: bool | None
    num_turns: int | None
    input_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    duration_ms: int | None
    result_text: str | None
    prompt_bytes: int
    stream_bytes: int
    stream_lines: int
    omitted_lines: int
    stderr_bytes: int
    truncated: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class TurnRow(TurnSummaryRow):  # the whole row
    prompt: str
    stream: str
    stderr: str


class Queries:
    async def issue(self, number: int) -> IssueRow | None: ...

    async def events_for_issue(self, number: int, limit: int) -> list[EventRow]:
        """Newest first."""

    async def turn_summaries_for_issue(self, number: int) -> list[TurnSummaryRow]:
        """Captured turns of the issue's runs: newest run first (runs.started_at), then turn."""

    async def turn(self, run_id: str, turn_number: int) -> TurnRow | None: ...

    async def state_counts(self) -> dict[str, int]:
        """Issues per StateLabel value over the Kanban's predicate; every role key present."""
```

- `turn_summaries_for_issue` is one query joining `run_turns` to `runs` on `run_id`, so the issue
  page and `/api/v1/issues/<n>` list the turns of every run in one round trip; the view groups
  them by run id.
- `state_counts` is one `GROUP BY state` over `github_state = 'open' OR state = 'complete'` with
  `state IS NOT NULL`, the predicate `issues_by_state` uses, so the two agree. `issuebot stats` and
  `/api/v1/stats` both report `by_state` from it; the CLI's counts therefore no longer stop at the
  Kanban's `COMPLETE_LIMIT`.
- `issues_by_state` (Phase 6 parked item): a row whose `state` is not a `StateLabel` value is
  skipped instead of creating a sixth key (`setdefault` goes). Open issues that carry the
  `complete` label keep their place at the head of the `complete` column ahead of the
  `COMPLETE_LIMIT` most recent closed ones: they are mislabels a human should see.
  `COMPLETE_LIMIT` stays 50.
- `MAX_WINDOW_DAYS` bounds both `stats --days` (`[FAIL] stats: --days must be between 1 and
  365`) and the API's `window` (§6.3); `daily_series` itself is unchanged.
- `seen_at` (Phase 6 parked item): the web process writes nothing to any table (its one write is
  `NOTIFY`), so the worker remains the single writer and callback-time stamps stay consistent. A
  second writer would need the server's `now()` for `seen_at`; recorded, not built.

## 6. The web application (`issuebot.web`)

### 6.1 The app factory

```python
LIVE_POLL_S = 10
CHART_POLL_S = 60
STALE_FACTOR = 3
REFRESH_MIN_INTERVAL_S = 5.0
RECENT_EVENTS_LIMIT = 50
RUN_ID_PATTERN = r"^\d{8}T\d{6}Z-[0-9a-f]{6}$"


def create_app(
    database: Database,
    settings: Settings,
    *,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = _utcnow,
) -> FastAPI: ...
```

Every request that reads opens one connection through `database.queries()` and closes it when the
response is built (Phase 6's facade; §10, decision 3). The app keeps `settings.github.repo` and
`settings.github.labels` for display and the `Database` for reads and the refresh; it never reads
`WORKFLOW.md` again. `clock` drives the refresh throttle and `now` the ages, both injectable for
tests. Templates come from a `jinja2.Environment` with `autoescape=True`, `StrictUndefined` and
the package's `templates/` directory; static files from `StaticFiles` over `static/`.

### 6.2 Pages

| Route | Renders |
|---|---|
| `GET /` | `index.html`: the live region (below) wrapped in `<div id="live" hx-get="/partials/dashboard" hx-trigger="every 10s" hx-swap="outerHTML">`, plus two `<canvas>` charts that `app.js` fills from `GET /api/v1/stats?window=30d` on load and every `CHART_POLL_S` (issues closed per day; runs per day; bars) |
| `GET /partials/dashboard` | `partials/dashboard.html`: the worker's last report (snapshot age, tick, slots, poll interval, config validity, `stale` once the age passes `STALE_FACTOR` poll intervals, "no report yet" without a row), the "Poll now" button (`hx-post="/api/v1/refresh" hx-swap="none"`; `app.js` writes the outcome into a status span from the response status), hero stats (closed 1 d and 7 d, runs 1 d and 7 d, running, retrying, total cost and total tokens from the snapshot totals), the running panel (issue, attempt, turns, last event, last activity age, started, stop cause), the retry panel when non-empty (issue, kind, attempt, due, error), the Kanban: five columns in `StateLabel` order headed by the configured label name and the count, cards with number, title, PR link when present and the updated age, each card linking to `/issues/<n>`; the `complete` column notes "50 most recent" when it holds `COMPLETE_LIMIT` closed rows. A `DatabaseError` renders the fragment as a "database unavailable" banner (status 503) so the page keeps polling |
| `GET /issues/{number}` | `issue.html`: title, state label, GitHub link, PR link and state, created, updated, closed; the snapshot's running or retry entry for this issue when present; the runs table (run id, attempt, started, ended, outcome, turns, tokens in and out, cost, error, log dir), each run followed by its captured turns (turn, model, agent iterations, tokens, cost, duration, subtype, a link to the turn page) or, for a finished run with none, "turn logs were not captured"; the issue's events newest first (`RECENT_EVENTS_LIMIT`) with `describe_event`'s one line each. Unknown issue: `error.html`, 404 |
| `GET /issues/{number}/runs/{run_id}/turns/{turn_number}` | `turn.html`: issue, run, "turn t of N" (N from the run's `turns`), model, subtype, iterations, tokens, cost, duration, `truncated`/`omitted_lines` notes with the byte counts; the transcript (§6.5); the prompt and stderr in `<details>` blocks with their sizes; three raw links. 404 when the run is not this issue's or the turn is absent |
| `GET /issues/{number}/runs/{run_id}/turns/{turn_number}/{part}` | `part` in `prompt`, `stream`, `stderr`: the stored text as `text/plain; charset=utf-8` with `X-Content-Type-Options: nosniff` and `Content-Disposition: inline; filename="<run_id>-turn-<t>.<ext>"` (`md`, `jsonl`, `log`); 404 otherwise |
| `GET /static/{path}` | the package's static files |

Path parameters are typed (`int`) or constrained (`run_id` matches `RUN_ID_PATTERN`); a value that
does not fit is a 404, never a 422.

### 6.3 API

Shapes follow Symphony §13.7.2 with `claude_totals` for `codex_totals`; the issue route is keyed
by number (roadmap §2.8) rather than identifier. Errors are `{"error": {"code": "...",
"message": "..."}}`. Everything datetime is ISO 8601 UTC.

`GET /api/v1/state`, from the `runtime_snapshot` row:

```json
{
  "generated_at": "<snapshot.at>",
  "written_at": "<snapshot.written_at>",
  "snapshot_age_s": 12.3,
  "worker": {
    "tick_count": 41, "last_tick_at": "...", "poll_interval_ms": 30000,
    "max_concurrent_agents": 2, "workflow_path": "...", "config_valid": true,
    "config_error": null, "stale": false
  },
  "counts": {"running": 1, "retrying": 0},
  "running": [
    {
      "issue_id": "7", "issue_identifier": "issuebot-scratch-7", "issue_number": 7,
      "issue_url": "https://github.com/...", "title": "...", "state": "in_progress",
      "run_id": "20260904T202535Z-0964cd", "session_id": "...", "attempt": 1,
      "rework": false, "resumed": false, "turn_count": 1, "last_event": "turn_activity",
      "started_at": "...", "last_event_at": "...", "stop_cause": null
    }
  ],
  "retrying": [
    {
      "issue_id": "9", "issue_identifier": "...", "issue_number": 9, "issue_url": "...",
      "attempt": 2, "kind": "failure", "due_at": "...", "error": "..."
    }
  ],
  "claude_totals": {
    "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0,
    "seconds_running": 0.0
  },
  "counters": {
    "runs_started": 0, "runs_ended": 0, "issues_completed": 0, "issues_cancelled": 0,
    "blocked": 0
  }
}
```

Without a snapshot row: 200 with `generated_at`, `written_at`, `snapshot_age_s` and `worker`
null, empty lists, zero counts, totals and counters. Per-running-session tokens are not in the
snapshot (the runner reports usage per finished turn) and are not invented; Symphony's
`last_message` and `rate_limits` are omitted.

`GET /api/v1/issues/{number}`:

```json
{
  "issue": {"number": 7, "identifier": "...", "title": "...", "state": "review", "...": "every IssueRow column"},
  "running": null,
  "retry": null,
  "runs": [
    {
      "run_id": "...", "attempt": 1, "outcome": "succeeded", "turns": 1,
      "...": "every RunRow column",
      "captured_turns": [
        {
          "turn_number": 1, "model": "claude-opus-5", "subtype": "success", "num_turns": 19,
          "...": "every TurnSummaryRow column",
          "url": "/issues/7/runs/20260904T202535Z-0964cd/turns/1"
        }
      ]
    }
  ],
  "logs": [
    {"run_id": "...", "turn_number": 1, "label": "run 20260904T202535Z-0964cd turn 1", "url": "..."}
  ],
  "recent_events": [
    {"id": 11, "at": "...", "kind": "run_ended", "issue_number": 7, "run_id": "...", "payload": {}}
  ]
}
```

`running` and `retry` are the snapshot entries for this issue, shaped as in `/api/v1/state`, or
null. Each run carries every `RunRow` column (`turns` stays the run's turn count) plus
`captured_turns`, the `TurnSummaryRow`s with their page `url`; `logs` is Symphony's key, a flat
list of the same turns. 404 `unknown_issue` when the issue is not in `issues`.

`GET /api/v1/stats?window=7d`:

```json
{
  "window": "7d", "days": 7, "closed": 2, "runs": 3,
  "by_state": {"todo": 0, "in_progress": 0, "review": 1, "rework": 0, "complete": 3},
  "series": [{"day": "2026-08-29", "closed": 0, "runs": 0}, {"day": "2026-09-04", "closed": 1, "runs": 1}]
}
```

`window` is `<N>d` with `1 <= N <= MAX_WINDOW_DAYS`; the default is `7d`; anything else is 400
`invalid_window`. `closed` and `runs` are `closed_count`/`runs_count` over N days, `series` is
`daily_series(N)`, `by_state` is `state_counts()`.

`POST /api/v1/refresh`: `Database.notify_refresh()`, then 202:

```json
{"queued": true, "coalesced": false, "requested_at": "...", "operations": ["poll", "reconcile"]}
```

A request within `REFRESH_MIN_INTERVAL_S` of the last NOTIFY this process sent answers 202 with
`"queued": false, "coalesced": true` and sends nothing (Symphony: implementations MAY coalesce).
A flood therefore costs one extra poll every five seconds at most, and the worker's
`request_refresh()` coalesces further. A `DatabaseError` is 503 `database_unavailable`.

`GET /healthz`: one query (the snapshot row).

```json
{"status": "ok", "database": "ok", "snapshot_at": "...", "snapshot_age_s": 12.3, "worker": "ok"}
```

`worker` is `ok`, `stale` (age above `STALE_FACTOR` times the snapshot's `poll_interval_ms`) or
`none` (no row); the web process is healthy in all three, so the status stays 200. A
`DatabaseError` is 503 `{"status": "unavailable", "database": "unavailable", "error":
"<redacted>"}`.

### 6.4 Errors, headers and logging

- `DatabaseError` anywhere: 503, a JSON envelope under `/api/` and `/healthz`, `error.html`
  elsewhere (the live partial renders its banner instead). The message is the facade's, already
  redacted.
- `HTTPException` and unknown routes: 404 `not_found` / 405 `method_not_allowed` envelopes under
  `/api/` and `/healthz`, `error.html` elsewhere. Path-parameter validation failures are 404s.
- Every response carries `Content-Security-Policy: default-src 'self'; script-src 'self';
  style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri
  'none'; form-action 'self'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`
  and `X-Frame-Options: DENY`, added by one middleware.
- uvicorn runs with `log_config=None`, so its access and error lines go through the root logger
  that `configure_logging` set up and come out as structlog lines like everything else.

### 6.5 The transcript (`transcript.py`)

```python
TOOL_INPUT_COLLAPSE = 2 * 1024
TOOL_RESULT_LIMIT = 4 * 1024
BlockKind = Literal[
    "init", "text", "thinking", "tool_use", "tool_result", "result", "omitted", "unparseable"
]


@dataclass(frozen=True, kw_only=True, slots=True)
class Block:
    kind: BlockKind
    title: str  # "assistant", "Bash", "tool result", "result: success", ...
    text: str  # the body, already cut
    cut: int  # characters removed from the body; 0 when whole
    collapsed: bool  # rendered inside <details>


@dataclass(frozen=True, kw_only=True, slots=True)
class Transcript:
    blocks: list[Block]
    hidden: int  # status messages not rendered


def parse_transcript(stream: str) -> Transcript: ...
```

Line by line: `system`/`init` becomes an `init` block (model, claude version, cwd, tool count);
each `assistant` content block becomes `text` (whole), `thinking` (collapsed) or `tool_use`
(title the tool name, body the input as `json.dumps(indent=2, sort_keys=True)`, collapsed past
`TOOL_INPUT_COLLAPSE`); each `user` content block of type `tool_result` becomes `tool_result`
(body the text content, or the JSON of a non-text content, cut at `TOOL_RESULT_LIMIT` with `cut`
set, collapsed) and one of type `text` becomes `text` titled `user` (the sample has one: the brief
the agent gave its review subagent); the `result` line becomes `result` (title `result: <subtype>`, body the result
text; the page adds cost and tokens from the row); an `issuebot_omitted` stub becomes `omitted`
("a <original_type> message of N bytes was not stored"); a line that is not JSON becomes
`unparseable` (its first 200 characters). Every other line (`rate_limit_event`, `tool_progress`,
`system` subtypes such as `task_started`, `thinking_tokens`, `vcs_state_changed`) is counted in
`hidden` and the page shows one "n status messages hidden" line. Nothing here touches HTML: the
template escapes every `title` and `text`.

### 6.6 Templates, styles and scripts

Six templates (§2), one stylesheet and one script of the project's own, and two vendored
libraries: htmx 2.0.10 (`htmx.min.js`, 0BSD) and Chart.js 4.5.1 (`chart.umd.js`, MIT), each with
its licence file beside it and a `vendor/README.md` naming version, source URL and licence. No
build step, no Node, no CDN (roadmap decision 7). The `<meta name="htmx-config">` tag sets
`allowEval: false`, `selfRequestsOnly: true` and `includeIndicatorStyles: false`, so htmx needs
neither `unsafe-eval` nor an inline stylesheet. `app.js` owns the chart fetch loop and the
"Poll now" status line (an `htmx:afterRequest` listener); there is no inline script or style
anywhere, which is what lets the policy in §6.4 hold. Every `href` that comes from data passes
`safe_href`, which returns the URL only when it starts with `https://`. Text from the database is
rendered as text; the transcript and the prompt sit in `<pre>` elements with wrapping.

## 7. CLI, compose, configuration, image and CI

### 7.1 `issuebot web`

```
issuebot web [--workflow PATH] [--port N] [--bind HOST]
```

1. Load the workflow (`[FAIL] workflow: ...`, exit 2).
2. `database.url` unset: `[FAIL] database: not configured; export DATABASE_URL or set
   database.url: $VAR`, exit 1 (the Phase 6 line). The web process requires the database.
3. `_open_database(settings)` (Phase 6 §9.4): migrate at start, `db_migrated` logged; a
   `DatabaseError` is `[FAIL] database: <redacted>`, exit 1. The worker and the web migrating at
   the same moment is safe: both take the same advisory lock.
4. `create_app(database, settings)`; `--port` overrides `server.port` and `--bind` overrides
   `server.bind` (Symphony §13.7: the CLI wins; port 0 asks for an ephemeral port, which uvicorn
   reports in its own log line); log `web_started` with `bind`, `port` and the database
   description.
5. `await _serve(app, host=, port=)`, where `_serve` is the seam tests substitute (like
   `_orchestrator_factory`) and the real one is `uvicorn.Server(uvicorn.Config(app, host=host,
   port=port, log_config=None)).serve()` on the command's own event loop. uvicorn installs its
   own SIGTERM and SIGINT handlers and, once its server has shut down, re-raises the signal that
   stopped it with the previous handler restored; `_uvicorn_serve` installs no-op handlers for
   both around the call so that re-raise is harmless, then restores what was there. The command
   exits 0 on either signal. A bind failure is uvicorn's error line and exit 1.

`validate` gains no check (the count stays twelve): `ServerSettings` already rejects a bad port
or an empty bind at load, and a port in use is reported at start.

### 7.2 Compose and configuration

```yaml
  web:
    build: .
    # Reads WORKFLOW.md for database.url, server.* and the repository name; needs no GitHub,
    # Claude or Slack credential, so no env_file. Migrates at start like the worker.
    command: ["web"]
    init: true
    restart: unless-stopped
    environment:
      DATABASE_URL: postgresql://issuebot:issuebot@db:5432/issuebot
    ports:
      - "127.0.0.1:${ISSUEBOT_WEB_PORT:-8080}:8080"
    volumes:
      - ./WORKFLOW.md:/app/WORKFLOW.md:ro
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
    depends_on:
      db:
        condition: service_healthy
```

The host port is `ISSUEBOT_WEB_PORT` (dot-env example; default 8080); the container side is
`server.port`, whose default the health check assumes. No new setting: `server.port` and
`server.bind` are used as they are; the cadences, the caps, the throttle and the limits are
constants (§4.1, §6.1). Outside Docker set `server.bind: 127.0.0.1` (the default `0.0.0.0` is the
container default, roadmap §2.11) or run behind a reverse proxy. A `WORKFLOW.md` that names
another `$VAR` explicitly (for example `github.token: $GH_TOKEN`) needs that variable in the web
container too, since the loader resolves it; the dogfood file does not.

### 7.3 Dependencies, image, CI

- `uv add 'fastapi>=0.141' 'uvicorn>=0.52'` (0.141.1 and 0.52.4 on 2026-09-04; starlette 1.6.0,
  anyio, click, h11 and annotated-doc come with them; all pure Python with 3.14 support) and
  `uv add --dev 'httpx2>=2.12'` (2.12.0, the test client's transport: Starlette 1.6 prefers
  `httpx2` and warns when only `httpx` is installed). Jinja2 is already a dependency.
  Dependabot's `uv` group bumps them; the vendored JavaScript is bumped by hand
  (`vendor/README.md`). `pyproject.toml` also gains one pytest `filterwarnings` entry that
  ignores Starlette's import-time `anyio.abc.BlockingPortal` deprecation, so the summary line
  stays `N passed`. `.pre-commit-config.yaml` excludes `tests/fixtures/runs/` and
  `src/issuebot/web/static/vendor/` from every hook: the recorded prompt and `htmx.min.js` have
  no trailing newline and `end-of-file-fixer` would break their checksums.
- The Dockerfile is unchanged: `uv sync --frozen --no-dev` installs the two packages, `curl` for
  the health check is already installed, hatchling ships `templates/` and `static/` the way it
  ships `migrations/` (everything under the package directory). The image build in CI still
  passes with `docker run --rm issuebot:ci --version`.
- CI is unchanged: the test job's `postgres:18` service runs the new DB tests; the lint job's
  `check-added-large-files` (500 KB) passes (`chart.umd.js` is about 205 KB, `htmx.min.js` about
  50 KB, the sample fixture 115 KB).

## 8. Security

- Everything rendered is text through Jinja2 autoescape; no template uses `|safe` on data and
  nothing renders Markdown. `StrictUndefined` makes a missing variable a template error caught by
  the tests. `safe_href` keeps `javascript:` and relative schemes out of `href` even though the
  URLs come from GitHub's API.
- The headers in §6.4 hold without `unsafe-inline` or `unsafe-eval` because all script and style
  live in files (§6.6). The vendored libraries are pinned files in the repository, reviewed like
  any other change.
- Path parameters are typed or pattern-checked, so the raw-text `Content-Disposition` filename is
  built from validated parts; raw text is `text/plain` with `nosniff`, never rendered as HTML.
- The database holds untrusted text from this phase on (§3.2); it is stored as bound parameters,
  read as text and escaped on render. No SQL is built from data.
- The database URL is never logged or shown: the web logs `describe(url)`, every error message
  the facade raises has passed `redact`, and the 503 bodies carry those messages only.
- No authentication (roadmap): compose publishes the port on the host's loopback; the container's
  `0.0.0.0` bind is reachable only through that mapping. The one write, `POST /api/v1/refresh`,
  is throttled to one NOTIFY per five seconds per process; a cross-site POST can at most bring one
  poll forward.
- uvicorn's access log lines carry the path and status of every request, including the
  ten-second poll; `--log-level WARNING` silences them.

## 9. Testing

All hermetic except the DB-marked tests, which use the Phase 6 `db_url` fixture (one schema per
test; skipped, and reported as skipped, without `DATABASE_URL`). The web tests use
`fastapi.testclient.TestClient` (httpx2) against `create_app(FakeDatabase(), settings)`; the
`FakeDatabase` is the `test_cli.py` one, moved into a shared `tests/fakes/database.py` with its
`FakeQueries` grown by the five new methods and canned rows; `tests/fakes/web.py` holds the row
builders, a fake clock and the `Harness` (a `FakeDatabase` behind a `TestClient`) that
`test_web_app.py` (the API and `/healthz`) and `test_web_pages.py` (the pages) share.
`tests/fixtures/runs/<run_id>/`
holds the real turn of scratch issue #7 (95 lines, 115 429 bytes; `claude-opus-5`; 19 iterations;
38 + 23 100 + 490 200 input, 8 425 output tokens; $0.8976; 201 719 ms) copied from the workspace.

| File | Covers |
|---|---|
| `test_agent_turnlog.py` | the sample: one capture, `stream_lines` 95, `stream_bytes` 115429, `omitted_lines` 0, not truncated, the summary columns above, `prompt_bytes` 10106, `stderr_bytes` 0; synthetic: a line over `LINE_LIMIT` becomes the stub with its type and byte count and counts as omitted; a stream over `STREAM_LIMIT` is truncated with its `result` line appended; a stream whose result line was kept is not appended twice; the prompt head and stderr tail caps; missing prompt or stderr files; a missing directory; a stream that cannot be read is skipped; an unparseable line is kept; `turn-10` sorts after `turn-2`; a turn with no result line has null summary columns |
| `test_web_transcript.py` | the sample: 1 init, 11 text (10 assistant, 1 user), 4 thinking, 24 tool_use, 24 tool_result, 1 result, hidden 30; a tool result over `TOOL_RESULT_LIMIT` is cut with `cut` set; a tool input over `TOOL_INPUT_COLLAPSE` is collapsed; an `issuebot_omitted` stub and an unparseable line become their placeholders; a non-text tool result is rendered as JSON; the empty stream gives no blocks |
| `test_web_app.py` | `/api/v1/state` with and without a row and every documented key, `stale`; `/api/v1/issues/7` shape (`captured_turns`, `logs`, `running`, `retry`), 404 envelope, 404 for `/issues/abc`; `/api/v1/stats` default `7d`, `30d`, and 400 for `0d`, `366d`, `7`, `x`, `7D`, `-3d`; `/api/v1/refresh` 202 queued, then coalesced within the interval, then queued again after it (fake clock), 503 envelope on failure, 405 on GET; `/healthz` ok, stale, none and 503; a `DatabaseError` in the API is a 503 envelope; `/api/v1/nothing` 404 envelope; `DELETE /api/v1/state` 405 envelope; the four headers on every API response; one connection per request; `safe_href`; `window_days`; `worker_status`; `describe_event` for every kind |
| `test_web_pages.py` | `/` 200 with a title of `<script>alert(1)</script>` appearing only escaped, the htmx attributes, the chart canvases and script tags, every label as a column heading, a `javascript:` PR URL rendered without a link, no inline `<script>` or `style=`; the live partial with and without a snapshot, stale, a config error, the complete cap note, the 503 banner on a `DatabaseError`; a `DatabaseError` on a page is the 503 error page; `/issues/7` 200 with runs, captured turns, "turn logs were not captured" for a run without them, the running entry and the event lines, escaped title, 404 page for an unknown issue and for `/issues/abc`; the turn page 200 with escaped tool input and result text, the hidden-status line, the raw links and the cap notes, 404 for another issue's run, a missing turn, a missing run, a malformed run id, a malformed turn number and an unknown issue; the three raw parts with their content type, `nosniff` and filename, 404 for another part; static files served, 404 for a missing one; `/nothing` 404 page; the four headers on pages and static files; `age_text`, `stamp_text`, `duration_text`, `money`, `thousands`, `dashboard_context` |
| `test_web_app_db.py` | DB: seeded through `PostgresStore` (three issues, a run with `run_started`/`run_ended` and two captures, events, a snapshot): `/api/v1/issues/<n>` lists the run with both turns and their urls; the turn page renders a tool name from the stored stream; `/api/v1/stats?window=7d` equals `render_stats`'s numbers for the same rows; `/` renders every column heading and the seeded titles; `/healthz` is ok |
| `test_db_store.py` | `run_ended` with two captures writes two `run_turns` rows with every column; the same call again leaves two rows; `run_ended` without captures writes none; captures with a `run_started` are ignored |
| `test_db_sink.py` | a `run_ended` whose `log_dir` holds the sample files reaches the store with one capture (real files in `tmp_path`, `capture_turns` itself); a `log_dir` that does not exist gives no captures; a capture callable that raises is logged and the event is still written; the capture runs once when the store is unavailable on the first attempt and the item is retried; a `run_ended` with `log_dir` `None` never calls capture |
| `test_db_queries.py` | `issue` present and absent; `events_for_issue` newest first and limited; `turn_summaries_for_issue` across two runs (newest run first, turns ascending, no text columns); `turn` present and absent; `state_counts` every key, counts, a closed cancelled issue excluded; `issues_by_state` skips an unknown role row (inserted directly) |
| `test_db_migrate.py`, `test_db_database.py` | discovery finds two files; applying reports both and version 2; the probe reads 0 of 2 then 2 |
| `test_cli.py` | `web` without `database.url` prints the not-configured line and exits 1; a migration failure prints `[FAIL] database:` and exits 1 before serving; the seam receives the app, `server.bind`/`server.port` by default and the `--bind`/`--port` overrides; `stats --days 366` is rejected and `--days 365` accepted; `stats` reports `by_state` from `state_counts` |

## 10. Decisions made in this phase

1. **Turn logs live in the database.** `run_turns` is written by the PostgreSQL sink when it
   drains `run_ended`, from the files under `RunEnded.log_dir`, read once in a thread before the
   first write attempt. The web process reads the database only. An archive directory on the
   workspace volume and live files only were the alternatives (§4).
2. **Raw files are stored, capped, with a parsed summary**: prompt 256 KiB head; stream lines over
   64 KiB stubbed, 2 MiB of head lines with the result line always kept; stderr 64 KiB tail;
   result text 4 KiB; model and the result's numbers as columns. The transcript is rendered from
   the stored stream, so a better renderer later needs no recapture.
3. **The `Database` facade, one connection per request; no pool.** The traffic is one operator's
   browser. `psycopg_pool` stays a recorded follow-up.
4. **`issuebot web` migrates at start through `_open_database`**, and requires `database.url`.
   The advisory lock makes the worker and the web migrating together safe.
5. **Cadences are constants**: the live region every 10 s, the charts every 60 s, the worker
   `stale` after three of its own poll intervals measured from `runtime_snapshot.written_at`.
6. **The two 30-day charts read `daily_series(30)` through `/api/v1/stats?window=30d`** from
   `app.js`; no chart data is inlined into HTML.
7. **The Kanban is `issues_by_state` with `COMPLETE_LIMIT = 50` unchanged**; an unknown role is
   skipped; open issues labelled `complete` head that column.
8. **`MAX_WINDOW_DAYS = 365`** bounds `stats --days` and the API window.
9. **`state_counts()` serves `by_state`** for both the CLI and the API, so they agree past the
   Kanban cap.
10. **The worker stays the single writer**; the web's only write is `NOTIFY`. `seen_at` needs no
    change; a future second writer must use the server's clock.
11. **`POST /api/v1/refresh` is throttled** to one NOTIFY per 5 s per process and reports
    Symphony's `coalesced`; the dashboard's "Poll now" button uses it.
12. **`/healthz` reports `database` and `worker`** and is 503 only when the database does not
    answer.
13. **Dependencies: `fastapi`, `uvicorn`, `httpx2` (dev)**; vendored htmx 2.0.10 (0BSD) and
    Chart.js 4.5.1 (MIT) with licence files; the Dockerfile and CI are unchanged.
14. **No `validate` check for `server.*`**; the count stays twelve.
15. **The web container gets `DATABASE_URL` and the workflow file only**, no `env_file`.
16. **Security: autoescape, `safe_href`, typed path parameters, `text/plain` plus `nosniff` for
    raw text, a CSP without `unsafe-inline` or `unsafe-eval`, and four headers on every
    response.** The database now holds untrusted text; Phase 6 §11 is amended.
17. **No new settings**; `server.*` is used as it is; everything else is a constant; a change to
    `WORKFLOW.md` needs a web restart.
18. **Error handling**: JSON envelopes under `/api/` and `/healthz`, an HTML page elsewhere;
    validation failures are 404s; `DatabaseError` is 503.
19. **API shapes**: Symphony §13.7.2 as the baseline with `claude_totals`, `issues/<n>` by number
    (roadmap §2.8), the fields the snapshot actually has; no per-running tokens, no
    `last_message`, no `rate_limits`.
20. **Of the Phase 6 parked list, only the three items that touch this phase are adopted**
    (`issues_by_state`, `--days`, the `seen_at` note); the rest stays parked.

## 11. Open questions for the operator

Each has a default the spec and plan follow; say so if you want the other choice.

1. Should the compose `web` service get the same `env_file` as the worker, for parity, even
   though it uses none of those secrets? (Spec: no; `DATABASE_URL` only.)
2. Are 10 s for the live region, 60 s for the charts and three poll intervals for `stale` right?
   (Spec: yes, as constants.)
3. Should the Kanban's `complete` column show more than 50? (Spec: `COMPLETE_LIMIT = 50`.)
4. Is one NOTIFY per 5 s the right throttle for `POST /api/v1/refresh`? (Spec: 5 s.)
5. Should `/healthz` return 503 when the worker's report is stale, so a compose health check on
   `web` also watches the worker? (Spec: no; the web is healthy, the body says `stale`.)
6. Should `run_turns` be pruned, for example with `events`, once retention exists? (Spec: no
   retention in this phase.)
7. Should uvicorn's access log be off by default, given the ten-second poll? (Spec: on, at INFO.)

## 12. Done when

- `uv run pytest -q` passes with no network and no database (the DB tests report as skipped) and
  passes in CI with the service container (no skips); ruff and pre-commit clean; `docker compose
  build` succeeds; `pyproject.toml` and `uv.lock` carry `fastapi`, `uvicorn` and (dev) `httpx2`
  and nothing else new (plus the pytest `filterwarnings` entry).
- `uv run issuebot validate` on the committed `WORKFLOW.md` still prints twelve checks; with
  `DATABASE_URL` pointing at the compose database the line reads `schema version 2` after
  `issuebot migrate` (`0002_run_turns` applied) and warns `schema version 1 of 2` before it.
- Live check from the developer host against `jleavers/issuebot-scratch` (issues #1, #3 and #5
  closed `complete`; #7 in `review` with PR #8 open and mergeable; `~/issuebot-scratch/WORKFLOW.md`
  as Phase 6 left it) with the compose `db` on `ISSUEBOT_DB_PORT=5440` holding the Phase 6 history
  (4 issues, 1 run, 11 events, a snapshot), `GH_TOKEN`, `SLACK_WEBHOOK_URL` and `DATABASE_URL`
  exported in the same command and never printed:
  1. `migrate` applies `0002_run_turns`; `validate` reads `schema version 2`.
  2. `issuebot web --bind 127.0.0.1 --port 8090` starts detached before any worker runs: `/healthz`
     is 200 with `worker: stale` (the Phase 6 snapshot is hours old), `/api/v1/state` shows that
     snapshot, `/api/v1/stats?window=30d` shows the Phase 6 history, `/` renders the Kanban with #7
     in `review` and #1, #3 and #5 in `complete`, `/issues/7` shows the Phase 6 run with its
     `log_dir` and "turn logs were not captured". The operator opens `http://127.0.0.1:8090/` in a
     browser: the charts render and the console shows no CSP violation.
  3. The worker starts detached (the Phase 6 `worker.log` renamed first); within one tick
     `/healthz` says `worker: ok` and the live region shows tick 1.
  4. A new `todo` issue runs to a pull request and `review`; the Kanban moves it through
     `in_progress` to `review` within a poll interval plus ten seconds; `/issues/<n>` lists the run
     with turn 1 (model, iterations, tokens, cost) and the turn page renders the transcript; the
     three raw links return the stored files; the worker log has `db_turns_captured turns=1`.
  5. `curl -X POST /api/v1/refresh` answers `queued: true`; a second within five seconds answers
     `coalesced: true`; the worker logs one `db_refresh_received`.
  6. The operator merges PR #8; the next sweep sets #7 `complete`; the Kanban shows it in the
     `complete` column and `/api/v1/stats` shows `closed` one higher; `issuebot stats` prints the
     same `by_state` numbers.
  7. SIGTERM to both processes: clean exits, `db_sink_closed failed=0 dropped=0`.
- `CLAUDE.md` describes `issuebot.web`, `agent.turnlog`, the db additions, the `web` command and
  schema version 2; `README.md` shows `issuebot web`, the compose `web` service, `ISSUEBOT_WEB_PORT`
  and the dashboard URL; `compose.yaml` has the service; the dot-env example has
  `ISSUEBOT_WEB_PORT`; the roadmap's Phase 7 section records what was decided and moves the log
  viewer out of Later; the Phase 6 spec carries amendment notes for `run_turns` (§2, §3), the
  `apply_event` signature (§5) and the untrusted-text posture (§11).
