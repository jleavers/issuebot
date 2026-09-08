# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python 3.14 with `uv`; `src` layout; package `issuebot`.

```bash
uv sync                              # create .venv and install (uses uv.lock)
uv run pytest                        # tests (hermetic; no network, no Docker; DB tests skip)
uv run pytest tests/test_cli.py -k validate   # one file / one pattern
docker compose --profile test up -d --wait test-db   # a throwaway postgres:18 on an ephemeral port
DATABASE_URL=postgresql://issuebot:issuebot@$(docker compose port test-db 5432)/issuebot uv run pytest
docker compose rm -sf test-db        # throw it away (not `compose down`: that is project-wide)
docker compose up -d db              # the long-lived db instead, on ISSUEBOT_DB_PORT (5434 here)
uv run ruff check . && uv run ruff format --check .
uv run pre-commit run --all-files    # whitespace, yaml, ruff (same as CI lint job)
uv run issuebot validate             # load ./WORKFLOW.md and check the environment
uv run issuebot validate --slack-probe   # same, plus one test message to the Slack webhook
uv run issuebot labels ensure        # create/update the state labels and markers in github.repo
uv run issuebot issues list          # table of open issues carrying a state label
uv run issuebot run-once <number>    # one worker session in the foreground (--show-prompt renders only)
uv run issuebot worker               # the long-running orchestrator; SIGTERM or Ctrl-C stops it
uv run issuebot migrate              # apply pending .sql migrations (worker and run-once do it too)
uv run issuebot status               # the worker's last runtime snapshot, read from the database
uv run issuebot stats [--days N]     # issues closed and runs started: 1d, 7d and per day
uv run issuebot refresh              # NOTIFY issuebot_refresh: a running worker polls at once
uv run issuebot web [--port N] [--bind HOST]   # the dashboard and the JSON API (needs DATABASE_URL)
docker compose build                 # image: git, gh, claude, app venv
docker compose up                    # db (postgres:18) + worker (issuebot worker) + web (issuebot web,
                                     #   http://127.0.0.1:${ISSUEBOT_WEB_PORT:-8080})
```

**Run the DB tests against `test-db`, not against the long-lived `db`.** It sits behind a
`test` profile, so a plain `docker compose up` never starts it; its cluster is tmpfs, so
nothing survives the container; and it publishes an *ephemeral* host port, so it cannot
collide with `db` or with the other projects on this host. `docker compose port test-db
5432` reads back the port Docker chose -- and that 5432 is the port *inside* the container,
where postgres listens whatever the host publishes. Throw it away with `docker compose rm
-sf test-db`, **not** `docker compose down`: `down` is project-wide and would stop the live
`db`, `worker` and `web` too.

**Do not pass `ISSUEBOT_DB_PORT=...` inline to `docker compose`.** That is the long-lived
`db`'s port, and it belongs to the project's env file (5432 in `.env.example`, 5434 on this
host) where compose reads it on its own. An inline value that disagrees with the configured
one is a different *published* port, so compose recreates the `db` container -- which may be
live and serving the worker and the web. 5434 is neither arbitrary nor a collision: this
host runs a database per project, and `docker ps` shows 5432, 5433 and 5435 held by three of
the others. Leave it there.

CI (`.github/workflows/ci.yml`) runs lint, tests (with a postgres:18 service) and
a Docker build on every PR. Dependabot covers uv, Docker and Actions weekly.
`claude-code-version.yml` covers what Dependabot cannot see: weekly, it compares the
Dockerfile's `CLAUDE_CODE_VERSION` with npm's `dist-tags.latest`, builds the image with the
new version, and opens a PR. `MIN_CLAUDE_VERSION` (`agent/runner.py`) is a compatibility
floor, not the shipped version, and moves by hand.

## Package layout

- `issuebot.config`: `load_workflow(path)` → `Workflow(config: Settings, prompt_template,
  raw_config, path, source_mtime_ns)`. Front matter → `$VAR`/`~`/relative-path
  resolution (`resolve.py`, designated fields only) → pydantic `Settings`
  (`settings.py`, `extra="forbid"`). Errors are `ConfigError` subclasses with a `code`.
- `issuebot.log`: `configure_logging()` (structlog, JSON to stderr by default),
  `get_logger()`, `bind_issue_context()`, `bind_session_context()`, `clear_context()`.
- `issuebot.events`: frozen dataclass events (`EVENT_KINDS`), `EventBus.publish()`
  (synchronous, sink failures isolated and counted), `LogSink`. `RunEnded.log_dir` (Phase 6)
  carries the run's log directory.
- `issuebot.github`: `StateLabel` roles, the transition table, `model_label_style` and
  `marker_label_styles` (`state.py`; `classify_closed(issue, labels)` returns `complete` for a
  merged linked PR, `no_change` when the issue carries `github.labels.no_fault` — the marker a
  no-fault session adds beside `review` — and `cancelled` otherwise. The marker is deliberately
  outside `GitHubLabels.as_tuple()`, which is what `clear_state` strips, so it survives the move
  to `complete`; `set_state(..., clear_markers=True)` is the one caller that does strip it, which
  is how `claim` makes the marker the *last* session's verdict rather than a label nothing ever
  removes; both adapters ensure it and report it missing alongside the five roles);
  frozen `Issue`/`LinkedPr`/`Comment` records (`models.py`); `GitHubAdapter`
  protocol (async); `GhCliAdapter` (GraphQL reads via `gh api graphql`, writes via
  `gh issue edit`, `gh label create`, `gh api`; `GhRunner` is the only subprocess boundary;
  `ensure_labels` creates, and `missing_labels` reports, the extra labels they are given);
  `FakeGitHub` for tests (same normaliser, GitHub-like semantics, `fail_next`, `calls`).
- `issuebot.agent`: `WorkspaceManager` (sanitised keys, containment, `gh repo clone --depth 1`,
  `bash -lc` hooks with timeout, `.issuebot/session.json`); `PromptRenderer` (Jinja2
  `StrictUndefined`; variables `issue`, `repo`, `labels`, `workpad_marker`, `attempt`,
  `turn_number`, `max_turns`, `rework`, `self_review`); `ClaudeRunner` (`claude -p
  --output-format stream-json --permission-prompts none`, prompt on stdin, minimal
  environment, silence timeout, SIGTERM then SIGKILL, per-turn logs under
  `.issuebot/runs/<run_id>/`); `settings_for_labels` (a `claude.model_labels` entry carried
  by the issue replaces `claude.model`; no match, or two labels naming different models,
  keeps the default) and `settings_with_model`; `claude_auth_status(command, environ)` (the
  `claude auth status --json` probe under `agent_environment`, 10 s, stdout or `None`) and
  `describe_claude_auth(output)` → `ClaudeAuth(verdict, detail, credential)` with verdict `ok`,
  `ambiguous` (a login and an API key both set), `unreadable` (no output, a timeout, or an older
  `claude` without the subcommand) or `logged_out`, shared by `validate` and the worker's
  startup, and carrying `credential` (`subscription`, `api_key` or `unknown` — only a definite
  probe names one, so `ambiguous` stays `unknown`), which is what the dashboard labels cost by;
  `parse_rate_limits` reads a `rate_limit_event` line into `RateLimits(five_hour, seven_day,
  observed_at)` of `RateLimitWindow(utilization, resets_at)`, total like `turnlog` because the
  line's shape is claude's and undocumented, and `StreamParser` reports it as a `rate_limits`
  turn event carrying the reading; `run_session` (turns, refresh between turns, `RunResult`, publishes
  `RunStarted`/`RunEnded`); `classify_result` maps a turn's last result (or its absence) to an
  `AgentErrorCategory`, `auth_failed` among them (see `issuebot.orchestrator`).
  `budget_exceeded` is the one category the turn loop does not fail on: `--max-budget-usd`
  caps one `claude -p` process, so the cap is a turn boundary and the next turn resumes the
  same session with a fresh ledger. Failing there would end the run, and the retry after it
  never resumes, so the replacement session would re-read the repository from cold and spend
  the cap again reaching what the first had already committed and pushed; a run whose every
  turn hits the cap now stops at `max_turns` and takes the blocked escape instead.
  Runtime turn events go to a `TurnObserver`, not the bus.
  Tests use `tests/fakes/claude` (replays `tests/fixtures/claude/*.jsonl`). `turnlog` (Phase 7):
  `capture_turns(log_dir)` reads a run's `turn-N.jsonl`, `.prompt.md` and `.stderr.log` into
  `TurnCapture`s, capped (prompt 256 KiB head; a stream line over 64 KiB becomes an
  `issuebot_omitted` stub; 2 MiB of head lines plus the last `result` line; stderr 64 KiB tail;
  result text 4 KiB), with the summary parsed from the init and result lines; it never raises.
  `tests/fixtures/runs/<run_id>/` holds a real turn (scratch issue #7), kept byte-for-byte
  (pre-commit excludes it).
- `issuebot.orchestrator`: one asyncio task owns the schedule. `state.py` (pure): `RunningEntry`,
  `RetryEntry`, `DispatchHold`, `RuntimeSnapshot`, `backoff_ms` (`min(10000 * 2^(attempt-1), max_retry_backoff_ms)`,
  attempt being the one about to run), `sort_candidates` (orphaned `in_progress`, then `rework`,
  then `todo`, oldest first), `observe_transition` (agent for `in_progress`→`review`, human
  otherwise, plus `PrOpened`). `actions.py`: `claim` (`in_progress`, markers cleared),
  `blocked_escape` (workpad block then
  `review`, idempotent per run id), `finish_terminal` (`complete`, `no_change` or `cancelled`,
  workspace removed; the first two both rest in the `complete` label and publish
  `IssueCompleted` with `resolution` `merged_pr` or `no_change`, so the dashboard's closed
  counts include triage, and only a genuine abandonment still clears the label).
  `orchestrator.py`: `Orchestrator.run()` = `startup()` (preflight, `auth_status`,
  `missing_labels`, then the Claude login through the `claude_auth` seam, a callable like
  `which` defaulting to `claude_auth_status`, run in a thread; every probe reports so one
  restart fixes everything), then `tick()` (reconcile: stalls, running refresh with one poll
  interval of grace for `review` measured on the monotonic clock, terminal sweep on the first and every tenth
  tick; mtime reload; preflight; fetch `in_progress`/`rework`/`todo`, plus `review` when an
  `on_issues` observer is attached; dispatch while slots remain; snapshot) and a queue wait that
  fires retries (continuation 1 s; failure backoff; `escape`; `slots`) and handles worker exits
  (the session's final transition is published before any release; `max_turns` while
  `in_progress` or `max_attempts` failures → the blocked escape).
  A session's runner is built from `settings_for_labels`, so a model label on the issue picks
  that session's model.
  A reading is about the account, not the issue, so `RunObserver` forwards it past the entry
  through `on_rate_limits` to the orchestrator, which keeps the newest (sessions run
  concurrently, so they arrive out of order) and carries it, with the startup probe's
  `credential`, in the snapshot — both new fields on `RuntimeSnapshot`, which `to_dict` walks
  generically into the existing `jsonb`, so neither needed a migration.
  `request_refresh()`, `request_stop()`, `snapshot()`; SIGTERM shutdown waits for `after_run`
  and publishes a final snapshot. `on_snapshot` (every tick and at shutdown) and `on_issues`
  (every successful fetch) are how polled data reaches the database sink without the
  orchestrator importing `db`.
  Orphans resume from `session.json` when its `last_outcome` is `null` or `cancelled`; retries
  never resume. Tests drive `tick()`, `handle_worker_exit()` and `fire_due_retries()` directly
  with a fake clock, a scripted `run_session` and a scripted `claude_auth`.
  Two startup choices made on purpose (#17): a definite `logged_out` is a startup failure, so
  under compose's `restart: unless-stopped` a logged-out worker restart-loops until the
  `claude-home` volume holds a login (visible in `docker compose ps`, costs nothing, heals
  itself), rather than claiming issues it cannot work; and only that definite answer fails,
  while `unreadable` and `ambiguous` log `orchestrator_startup_warning` and the worker starts,
  so a slow or wedged `claude` cannot keep a worker down. The verdict is logged on
  `orchestrator_started` as `claude_auth`. A credential that lapses *after* startup (#20) is
  caught by the run instead: `classify_result` reads an authentication failure out of claude's
  own words (`is_auth_failure`, `AUTH_FAILURE_MARKERS`) and gives it the `auth_failed`
  category, and a run that ends with it escapes the issue at once, with a blocker naming
  authentication rather than after `max_attempts` opaque failures. The same exit holds
  dispatch: no issue is claimed (`_dispatch_candidates` is skipped, a due retry requeues as
  kind `auth` at one poll interval) until a probe through the same `claude_auth` seam reports
  a login, which lifts the hold and resumes dispatch with no restart. `logged_out` holds for
  as long as it lasts, since a run has already failed and that answer shows nothing has
  changed; an `unreadable` one holds for at most `MAX_UNREADABLE_AUTH_PROBES` (10) ticks and
  then gives up (`dispatch_auth_hold_abandoned`), because #17's rule that a `claude` which
  cannot answer must not keep a worker down applies here too — the fallback is the per-run
  escalation, one issue per hold rather than one per attempt. The hold logs
  `dispatch_auth_held` every tick (ERROR on the first and on a changed error, WARNING after:
  an idle worker says nothing else) and `dispatch_auth_recovered` when it lifts.
  Both holds are carried in the snapshot as `dispatch_hold` (#29), a `DispatchHold(kind,
  reason, since)` beside `config_error`: `kind` is `preflight` (the message `preflight`
  builds) or `auth` (`claude authentication unavailable: <the probe's detail>`), and `since`
  is when that reason first held dispatch, so an unchanged hold keeps its start and a changed
  one restarts it. A held worker keeps ticking, so without it `issuebot status`, `/api/v1/state`,
  the dashboard and `/healthz` all read as a healthy worker while the board stops moving.
  An auth hold's `since` is keyed on the probe's verdict, not its wording, so an unreadable
  `claude` that garbles itself differently every tick still reports how long the hold has
  lasted. A hold still polls issues (`_poll_issues`, the fetch dispatch uses without the
  claiming), so the history the dashboard renders stays current for as long as it lasts --
  unless `fetch_preflight` (the `gh` and `github.token` half of `preflight`) is what is
  failing, when the request would only fail too.
- `issuebot.notifications`: the Slack sink, imported by `cli` only. `messages.py` (pure):
  `format_event(event, repo=, labels=)` → one line of mrkdwn per kind (issue link, `from → to`
  by actor, PR link, blocker reason, run cost) or `None`. `slack.py`: `urllib_post` (stdlib
  `urllib` in `asyncio.to_thread`, never raises, errors pass through `redact`), `PostResult`,
  `subscribed_kinds` (the allow-list minus `notification_sent`), `SlackSink` (`handle` formats
  and enqueues, cap 100; one drain task started by `start(bus)` posts with three attempts,
  `Retry-After` on 429 capped at 30 s, backoff 1 s then 4 s on 5xx and network errors, other
  4xx dropped; publishes `NotificationSent` after each delivery; `close()` drains for up to
  10 s). A drain timeout cancels the task, but a post already in the worker thread finishes
  its own socket timeout first, so exit can take up to 20 s. Constants, not settings. A
  webhook or allow-list change needs a worker restart.
- `issuebot.db`: the observability store, imported by `cli` and `web`; imports `config`,
  `events`, `github`, `log` and `agent.turnlog`. `migrations/NNNN_name.sql` (`0001_initial`,
  `0002_run_turns`; schema version 2) applied by `migrate.py` in one transaction under an advisory lock
  (`schema_migrations` bookkeeping; a recorded version newer than the files is an error).
  `connection.py`: `connect` (autocommit, 5 s connect timeout, UTC session), `describe`/`redact`
  (the URL's password never reaches a log or a line), `reconnect_delay` (1, 2, 4, 8, 16, then
  30 s). `store.py`: `PostgresStore` (`apply_event(event, turns=())` appends to `events`, upserts
  `runs` on `run_started`/`run_ended` and inserts the captured turns into `run_turns` in the
  `run_ended` transaction (idempotent per `(run_id, turn_number)`), or updates `issues` on
  `state_changed`, `issue_completed`, `issue_cancelled`; `upsert_issues`; `write_snapshot`);
  every `issues` write is guarded by `seen_at`, so write order never matters. `sink.py`:
  `PostgresSink` (`handle` enqueues events, cap 1000; `record_issues` merges polled snapshots
  into one pending batch; `record_snapshot` keeps the latest; one drain task writes, reconnects
  with backoff and retries the item in flight; a `run_ended` item's turn files are captured
  once, in a thread, before its first write attempt (`db_turns_captured`,
  `db_turns_capture_failed`); statement failures are dropped and counted; `close()` drains for
  up to 10 s). `listen.py`: `RefreshListener` (`LISTEN issuebot_refresh` on its own connection,
  callback per NOTIFY, reconnects). `queries.py`: `Queries` over one connection (`closed_count`,
  `runs_count`, `run_totals` (tokens and cost summed over the runs `runs_count` counts),
  `daily_series`, `issues_by_state` (unknown roles skipped, every column capped at
  `BOARD_LIMIT = 5`), `state_counts` (uncapped, which is what the board's headers count),
  `issues_for_state` (one column in full up to `ISSUE_LIST_LIMIT = 200`, or every column
  when the state is `None`; an unknown role lists nothing, as it sits on no column),
  `issue`, `runs_for_issue`, `events_for_issue`, `turn_summaries_for_issue`, `turn`,
  `recent_events`, `snapshot`) returning the frozen row types the dashboard renders;
  `MAX_WINDOW_DAYS = 365` bounds `--days` and the API window. `database.py`: the `Database`
  facade the CLI and the web app go through (`migrate`, `probe`, `queries`, `store`, `listener`,
  `notify_refresh`); one connection per call, no pool. Constants, not settings; a `database.url`
  change needs a restart. Tests: `db_url` (conftest) creates a schema per test and skips without
  `DATABASE_URL`; the sink and listener tests use fakes; `tests/fakes/database.py` is the
  `FakeDatabase` the CLI and web tests share.
- `issuebot.web`: the dashboard, imported by `cli` only; imports `config`, `db`, `github` and
  `log`. `app.py`: `create_app(database, settings, *, clock=, now=)` (FastAPI; pages `/`,
  `/issues[?state=<role>]`, `/issues/<n>`, `/issues/<n>/runs/<run_id>/turns/<t>` plus
  `/prompt|stream|stderr` as `text/plain`; `/partials/dashboard` (the htmx live region, every 10 s); `/api/v1/state`,
  `/api/v1/issues/<n>`, `/api/v1/stats?window=<N>d`, `POST /api/v1/refresh` (NOTIFY, throttled to
  one per 5 s, Symphony's `coalesced`), `/healthz` (503 only when the database does not answer;
  `worker` is `ok`, `held` while the worker ticks without claiming, `stale` past three poll
  intervals, or `none`, and `dispatch_hold` names the reason for a held one); `/static` (vendored htmx
  2.0.10 and Chart.js 4.5.1 under `static/vendor/`, kept byte-for-byte); JSON error envelopes
  under `/api/` and `/healthz`, `error.html` elsewhere; `DatabaseError` is 503; the four
  security headers on every response, a CSP without `unsafe-inline`). `views.py`: pure builders
  and template filters (`state_document`, `stats_document`, `issue_document` with
  `runs[].captured_turns`, `dashboard_context`, `describe_event`, `safe_href`, `window_days`,
  `worker_status`, `dispatch_hold`, `age_text`, `stamp_text`, `is_board_state`,
  `issue_filters`, `rate_limit_windows`, `cost_label`, ...). A board column draws at most `BOARD_LIMIT` cards, so its header
  counts `state_counts` rather than the rows it drew, and the difference is an overflow
  link to `/issues?state=<role>` — the list page, which is outside the live region so a
  filter survives the ten-second swap that would collapse an expander or reset a scroll. A snapshot's
  `dispatch_hold` reaches `/api/v1/state` and the dashboard's worker line through
  `dispatch_hold`, which reads it defensively (the column is JSON) and yields nothing for a
  hold that names no reason; `worker_status` reports `held` for a fresh snapshot carrying one,
  `stale` still winning, since a snapshot too old to trust is too old to trust about its hold.
  The worker line separates its parts by drawing them rather than spacing them (#47): the
  runtime figures are `.fact` chips, bounded and `nowrap` like the card's number chip, while
  a verdict (`config valid`, a config error, a held dispatch) is a dot and prose that wraps,
  because `config_error` is one line per invalid setting and no pill would hold it. Two
  same-coloured runs of text a flex gap apart read as one sentence with a double space in it,
  which is what the line used to do. The hero's cost and token tiles are 1d/7d
  sums over `runs` (`run_totals`), so they match the closed and agents-run tiles beside them and
  survive a worker restart; the worker's in-process `ClaudeTotals` restart with it and stay on
  `/api/v1/state` as `claude_totals` and in `issuebot status`, which both say "since start"
  and mean it. The hero is six tiles, each with two windows inside it — closed, agents run,
  cost, tokens, limits, activity — and `.hero` pins its
  column count (6, 3, 2) instead of auto-fitting, because every count has to divide the six
  tiles: an auto-fit grid that lands on five orphans the last one. `activity` is running and
  retrying in one tile, which is what leaves room for `limits`: the account's usage windows
  from `rate_limit_windows`, as percentages used with a `<progress>` bar (a bar's width cannot
  be an inline style under the CSP, and the element narrates itself). That builder holds the
  reset-aware rule — a window whose `resets_at` has passed reads 0% rather than repeating a
  reading that stopped being true at the reset — and yields `[]`, which the tile draws as N/A,
  for a definite `api_key` or no reading at all; an `unknown` credential with a reading still
  shows it, since a probe issuebot could not read is no reason to hide data claude did report.
  `cost_label` names the cost tile `cost (effort)`, `cost (actual)` or plain `cost` from the
  same credential. `/api/v1/state` carries both as `credential` and `rate_limits`. The token figures there go
  through `compact` (`39.2M`), the exact number staying as the window's `title`.
  `transcript.py`: `parse_transcript(stream)`
  turns the stored stream-json into `Block`s (init, text, thinking, tool_use, tool_result, result,
  omitted, unparseable; other status lines counted as `hidden`). Templates render with autoescape and
  `StrictUndefined`; nothing is inlined into HTML (`app.js` fetches the charts' data). One
  connection per request through `Database.queries()`. Constants, not settings; the web reads
  `WORKFLOW.md` once at start. Light and dark are role tokens in `app.css`, declared once for
  light and twice for dark (`@media (prefers-color-scheme: dark)` for the OS preference,
  `:root[data-theme="dark"]` for the operator's own choice, which wins); `static/theme.js` is
  loaded synchronously from `<head>` so the stamp lands before the first paint, persists the
  choice in `localStorage` (`issuebot-theme`; storing nothing keeps the OS in charge) and
  fires `issuebot:themechange`, which `app.js` uses to repaint the canvas the tokens cannot
  reach. Both themes' marks and text are held to WCAG contrast floors by
  `tests/test_web_theme.py`.
- `issuebot.cli`: argparse; `validate` (thirteen checks: three network probes through the
  adapter, the labels one covering `claude.model_labels` and the `no_fault` marker as well as
  the five state labels, a `claude --version` floor of 2.1.259, the `claude auth status --json`
  probe (shared with the worker's startup, see `issuebot.agent`) that names the credential the
  agent would use (`claude.ai`,
  `CLAUDE_CODE_OAUTH_TOKEN` or an API key), fails when logged out, warns when a login and
  `ANTHROPIC_API_KEY` are both set, and warns rather than fails when the subcommand is
  missing so an older-but-permitted `claude` stays green, a `database.url` check that connects and
  reports the server and schema versions (behind warns, ahead or unreachable fails), a
  `notifications.slack` check that warns when `SLACK_WEBHOOK_URL` is unset, requires `https`,
  and with `--slack-probe` posts one test message, and a prompt render against a sample issue),
  `labels ensure` (the five state labels, the `no_fault` marker, and one per
  `claude.model_labels` entry),
  `issues list`, `run-once <number> [--model NAME] [--show-prompt]` (claims `in-progress`,
  runs one session, never sets `review`; `--model` beats both the label and `claude.model`),
  `worker [--workflow PATH]` (the orchestrator until SIGTERM/SIGINT; `[FAIL] startup:` lines
  and exit 1 when the startup probes fail, `claude auth: not logged in; ...` among them), `migrate`,
  `status` (the snapshot as text, with a `dispatch: held (<kind>) since ...` line while
  dispatch is held), `stats [--days N]` (`by_state` from `state_counts`; `--days` 1 to 365), `refresh` and
  `web [--port N] [--bind HOST]` (each `[FAIL] database:` and exit 1 without `DATABASE_URL`);
  `run-once`, `worker` and `web` migrate first when `database.url` is set (a failure is
  `[FAIL] database:` and exit 1); `run-once` and `worker` start the Slack and PostgreSQL sinks
  before and close them after (Slack never for a non-`https` webhook); `worker` also passes
  `on_snapshot`/`on_issues` to the orchestrator and runs the refresh listener; `web` builds
  `create_app` and serves it with uvicorn (`--port`/`--bind` override `server.*`; uvicorn's
  lines go through structlog; SIGTERM/SIGINT exit 0; a port in use is uvicorn's error and exit
  1); exit codes 0/1/2 (ok / failed / workflow unloadable).
  Tests substitute `_which`, `_claude_version`, `_claude_auth`, `_adapter_factory`, `_run_session`,
  `_runner_factory`, `_orchestrator_factory`, `_slack_post`, `_database_factory` and
  `_serve`.

Design documents: `docs/superpowers/specs/` (phased design and one spec per phase),
`docs/superpowers/plans/` (one implementation plan per phase).

## What issuebot is

A bespoke reimplementation of [openai/symphony](https://github.com/openai/symphony)
built on Claude + GitHub instead of Codex + Linear. Full requirements live in
`docs/BLUEPRINT.md`; the essentials:

A service watches for new GitHub issues, picks them up, works on them
autonomously via `claude -p` in auto mode, opens a PR for human review, and files
follow-up issues where needed.

**Issue lifecycle is driven by labels**, and the label is the state machine —
anything reading or writing issue state goes through these:

| Label | Set by |
|---|---|
| `issuebot/todo` | human |
| `issuebot/in-progress` | agent, when work starts |
| `issuebot/review` | agent, when PR opened or no fault found |
| `issuebot/rework` | human, if the PR needs more work |
| `issuebot/no-fault` | agent, beside `review`, when it found no fault (a marker, not a state) |
| `issuebot/complete` | automatically, when the issue closes via linked-PR merge or with `issuebot/no-fault` |

GitHub is reached through the `gh` CLI, not a REST/GraphQL client library.

Two surfaces beyond the worker: a **web dashboard** (Kanban of the label columns
above, plus hero stats — issues closed in 1d/7d, agents spun up, as both
point-in-time numbers and time series) and **Slack notifications** on status
change. Persisting the time-series history is what PostgreSQL is for.

Planned infrastructure: Docker, Python 3.14, PostgreSQL, GitHub Actions CI
(lint, tests, docker build) and Dependabot.

## Operational rules (from AGENTS.md)

`AGENTS.md` is binding for agents in this repo. Read it, and note in particular:

- **Never push to `main`.** Push a feature branch and open a PR for human
  review. Never merge or close PRs — that is a human action.
- **Never run destructive commands**: `rm -rf`, `git reset --hard`,
  `git clean -fd`.
- **The repo is used from both Windows and Linux hosts.** Detect the OS before
  emitting shell commands or scripts: Bash/`.sh`/`&&` on Linux/macOS,
  PowerShell/`.ps1`/`;` on Windows. Never generate `.ps1` on Linux or `.sh` on
  Windows.
- `.pre-commit-config.yaml` must exist, with `trailing-whitespace` and
  `end-of-file-fixer` from pre-commit-hooks. If any `.tf` files are added, also
  wire up `terraform_fmt`, `terraform_validate` and `terraform_tflint` from
  antonbabenko/pre-commit-terraform.

## Creating PRs

Per the user's global instructions, set PR title/body via the REST API rather
than `gh pr create`/`gh pr edit` (the CLI hits a deprecated `projectCards`
GraphQL field and aborts here); a PreToolUse hook enforces this. Write the body
to a temp `.md` file in a **separate** Bash call from the `gh api` call, and pass
it with capital `-F body=@file.md`.
