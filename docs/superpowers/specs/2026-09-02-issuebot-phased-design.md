# Issuebot: Phased Design

Status: Approved 2026-09-02 (PostgreSQL 18 amendment applied)

This document turns `docs/BLUEPRINT.md` into a target architecture and a sequence of
phases. Each phase is sized to become one spec (`docs/superpowers/specs/`) and one
implementation plan (`docs/superpowers/plans/`). It fixes the cross-cutting decisions
so that per-phase specs do not have to re-derive them; per-phase specs then own the
detail.

Source material: the [Symphony spec](https://github.com/openai/symphony/blob/main/SPEC.md),
the [Elixir reference README](https://github.com/openai/symphony/blob/main/elixir/README.md),
its `WORKFLOW.md` prompt template and `docs/token_accounting.md`. Section references
of the form "Symphony §8.4" point at the Symphony spec.

## 1. What issuebot keeps from Symphony, and what changes

Symphony is a polling scheduler that claims tracker issues, runs a coding agent per
issue in an isolated workspace, keeps the workflow prompt and runtime settings in a
repo-owned `WORKFLOW.md`, and recovers from restarts by re-polling the tracker rather
than from a database. Those ideas all carry over. The table shows the substitutions.

| Concern | Symphony | issuebot |
|---|---|---|
| Coding agent | Codex app-server, one long-lived JSON-RPC thread per run, many turns | `claude -p` subprocess per turn; continuation turns via `--resume <session_id>` |
| Tracker | Linear (also GitHub, Jira, Asana, GitLab adapters) | GitHub Issues in one configured repo, via the `gh` CLI only |
| Issue state | Tracker workflow states (`Todo`, `In Progress`, `Human Review`, `Rework`, `Done`) | Exclusive `issuebot/*` labels (`todo`, `in-progress`, `review`, `rework`, `complete`) |
| Tracker writes | Agent, through host-executed provider tools so the child never sees the token | Orchestrator writes state labels it owns; agent uses `gh` directly for PRs, comments, follow-up issues, and the `review` label |
| Credentials | Stripped from agent child environment | Agent needs a GitHub token to push, so it gets a dedicated, repo-scoped token; branch protection enforces "never push to main" |
| Persistence | None; in-memory state, tracker/filesystem recovery | Same for scheduling correctness. PostgreSQL added as an **observability store** (runs, events, stats) that the worker can run without |
| Workspace bootstrap | Left to `hooks.after_create` | Built-in clone of the configured repo (`gh repo clone`, so auth is handled), plus the same hooks |
| Prompt template | Liquid | Jinja2 with `StrictUndefined` (Symphony's template syntax is a subset) |
| Status surface | Optional Phoenix LiveView dashboard of runtime state | Required FastAPI dashboard: Kanban by label, hero stats, time series |
| Notifications | None | Slack incoming webhook on state transitions and blocked runs |
| Hot reload of `WORKFLOW.md` | Filesystem watch | mtime check on every poll tick (Symphony §6.2 already requires per-tick revalidation) |

Two Symphony rules are deliberately relaxed:

- **Rework keeps the PR.** Symphony closes the PR and restarts from a fresh branch.
  On GitHub the review thread is the valuable context, so rework reuses the branch and
  PR, addresses review comments, and pushes. It does start a fresh Claude session.
- **Blocked runs park in `review`.** When an agent exhausts its turn or retry budget
  without moving the issue on, issuebot sets `issuebot/review` and posts a blocker
  comment. That reuses the five blueprint labels instead of adding a `blocked` one and
  mirrors Symphony's "blocked-access escape hatch".

## 2. Target architecture

### 2.1 Components

Layered as in Symphony §3.2, one Python package `issuebot/`:

| Layer | Module | Responsibility |
|---|---|---|
| Policy | `WORKFLOW.md` (in the target repo or beside the service) | Prompt body plus YAML front matter. The only thing operators normally edit |
| Configuration | `issuebot.config` | Load front matter, apply defaults, resolve `$VAR` and `~`, validate into typed pydantic models |
| Integration | `issuebot.github` | Normalised `Issue` model; `gh`-backed adapter (read candidates, refresh by id, set exclusive state label, find linked PR, comment); in-memory fake for tests; label state machine |
| Execution | `issuebot.agent` | Workspace manager (sanitised keys, root containment, clone, hooks, timeouts); prompt rendering; `claude -p` runner that parses `stream-json`, captures session id, usage and cost, enforces stall and turn timeouts |
| Coordination | `issuebot.orchestrator` | Single-authority in-memory state; poll tick; claims; dispatch ordering and concurrency; retry with backoff; continuation turns; reconciliation; startup cleanup; restart resume; blocked escape |
| Observability | `issuebot.events` | In-process event bus with sinks: structured log (always), PostgreSQL (Phase 6), Slack (Phase 5) |
| Persistence | `issuebot.db` | psycopg 3 connection pool, numbered SQL migrations, query helpers for stats |
| Surface | `issuebot.web` | FastAPI app: HTML dashboard (Jinja2 + HTMX + Chart.js) and `/api/v1/*` |
| Host | `issuebot.cli` | `issuebot worker`, `web`, `migrate`, `validate`, `labels ensure`, `status`, `refresh`, `run-once` |

The orchestrator depends on `github`, `agent`, `config` and `events` only. It never
imports `db` or `web`. That keeps Symphony's invariant that the dashboard is not
required for correctness (§13.4, §13.7) and lets the worker run with no database.

### 2.2 Process topology and deployment

One Docker image, three compose services:

| Service | Entrypoint | Talks to |
|---|---|---|
| `worker` | `issuebot worker` | GitHub (via `gh`), Claude (via `claude`), PostgreSQL (write, optional), Slack (optional) |
| `web` | `issuebot web` | PostgreSQL (read); PostgreSQL `NOTIFY` to ask the worker for a refresh |
| `db` | `postgres:18` | – |

The split matters operationally: restarting `web` never kills running agents, and
the worker keeps working while the dashboard is down. The worker publishes its
runtime snapshot (running sessions, retry queue, totals) into a single-row table on
every tick, which is what the dashboard's "running now" panel reads. The Symphony
`POST /api/v1/refresh` trigger becomes a `NOTIFY issuebot_refresh`; the worker
listens and coalesces.

The image contains `git`, `gh`, `claude` (Claude Code CLI), Python 3.14 and `uv`.
The target repo's own toolchain must also be present for the agent to run its tests;
the Dockerfile is written so a downstream image can `FROM` it and add tooling. For
dogfooding (issuebot working on issuebot) the base image already has everything.

Volumes: `workspaces/` (per-issue checkouts, preserved across restarts) and the
Claude Code home directory (auth plus session transcripts, which `--resume` needs).

Secrets arrive as environment variables only: `GH_TOKEN` (dedicated, repo-scoped),
Claude auth (`ANTHROPIC_API_KEY` or a mounted OAuth login), `DATABASE_URL`,
`SLACK_WEBHOOK_URL`. `WORKFLOW.md` references them as `$VAR`; it never contains them.

### 2.3 Label state machine

Exactly one `issuebot/*` state label is present on a tracked issue at any time. The
adapter's `set_state(issue, label)` removes the others and adds the target in one
`gh issue edit` call, so no observer sees two.

```
                 human                       agent (via gh)
  (no label) ───────────▶ todo ──┐    ┌──▶ review ──────────┐
                                 │    │      │              │ linked PR merged
                        issuebot │    │      │ human        │ (GitHub closes issue;
                        dispatch ▼    │      ▼              │  issuebot observes)
                           in-progress ◀── rework           ▼
                                 ▲                       complete
                                 │ issuebot dispatch
                                 └── (rework)
```

| Transition | Actor | Trigger |
|---|---|---|
| `todo` → `in-progress` | orchestrator | Issue claimed and a worker dispatched |
| `rework` → `in-progress` | orchestrator | Same, with rework context in the prompt |
| `in-progress` → `review` | agent | PR opened (or updated, on rework) and validation green; prompt instructs it |
| `in-progress` → `review` | orchestrator | Turn or retry budget exhausted: the "blocked" escape, with a blocker comment |
| `review` → `rework` | human | Reviewer wants changes |
| `review` → `complete` | orchestrator | Issue is closed **and** a linked PR is merged (`closedByPullRequestsReferences` via `gh api graphql`) |
| any → (labels stripped) | orchestrator | Issue closed without a merged PR: treated as cancelled, worker stopped, workspace removed |

Scheduler vocabulary, mapped onto Symphony §5.3.1:

- `active_states`: `todo`, `rework`, `in-progress`. An `in-progress` issue with no live
  claim is an orphan from a crash or restart and is resumed, not restarted.
- `terminal_states`: `complete`, or GitHub state `closed`.
- `review` is neither: the worker must not run while a human is reviewing.
- `required_labels` is unnecessary because the `issuebot/*` label *is* the opt-in.
  Only a human with triage rights can add `todo`, which is the dispatch gate and the
  main defence against untrusted issue bodies reaching the agent.

Restart recovery is label-driven and filesystem-driven, as in Symphony §14.3: the
worker re-polls, finds `in-progress` issues, reads `<workspace>/.issuebot/session.json`
for the Claude session id and attempt count, and resumes.

### 2.4 Run lifecycle with `claude -p`

A Symphony "turn" becomes one `claude -p` process. The agent's whole tool loop for
that turn happens inside the process; the orchestrator only sees the `stream-json`
event stream and the exit.

First turn:

```
claude -p "<rendered prompt>" \
  --permission-mode auto --output-format stream-json --verbose \
  --session-id <uuid4> --model <claude.model> --max-budget-usd <claude.max_budget_usd> \
  [--allowedTools ...] [--disallowedTools ...] [--append-system-prompt ...]
```

Continuation turn: the same, with `--resume <session_id>` in place of `--session-id`
and a short continuation prompt instead of the full task prompt (Symphony §7.1).

Worker session (Symphony §16.5, adapted):

1. Create or reuse the workspace; clone on first creation; run `before_run` hook.
2. Render the prompt (`issue`, `attempt`, `rework` context, `turn_number`).
3. Run one `claude -p` turn. Parse `system/init` for the session id (persist it to
   `.issuebot/session.json` immediately), `assistant`/`user` events for last-activity
   timestamps, and `result` for `usage`, `total_cost_usd`, `num_turns`, `is_error`.
4. On normal exit, re-fetch the issue. If it has left `in-progress`, exit normally.
   If it is still `in-progress` and `turn_number < agent.max_turns`, run a
   continuation turn. Otherwise exit normally and let the orchestrator apply the
   blocked escape.
5. On any failure (non-zero exit, `is_error`, turn timeout, stall) fail the attempt
   so the orchestrator retries with backoff.
6. Run `after_run` hook best-effort.

Timeouts and failure mapping follow Symphony §10.6 with `claude.*` keys:
`turn_timeout_ms` (silence on stdout), `stall_timeout_ms` (orchestrator-side
inactivity), `read_timeout_ms` is not needed because there is no request/response
handshake. Error categories: `claude_not_found`, `invalid_workspace_cwd`,
`turn_timeout`, `process_exit`, `turn_failed`, `budget_exceeded`, `prompt_error`.

Permission posture (Symphony §10.5 requires this be documented): `auto` mode inside a
container. In `-p` mode a tool call that auto mode declines returns a permission error
to the model rather than prompting, so a run cannot stall on approval. Operators can
switch to `bypassPermissions` per workflow; it is a pass-through setting.

Token and cost accounting: each `claude -p` process reports a single final `result`
event with absolute usage for that turn. Per-run totals are sums over turns. This is
simpler than Symphony's thread-cumulative rules (its `token_accounting.md`) because
there are no streaming cumulative snapshots to de-duplicate.

### 2.5 Workspaces and branches

- Workspace root default `/workspaces` in the container; `<root>/<workspace_key>` where
  the key is `<repo>-<number>` (for example `issuebot-42`). Sanitisation and the
  hash-suffix rule from Symphony §4.2 still apply for safety even though the key is
  already clean.
- Root containment and `cwd == workspace_path` are checked before every launch
  (Symphony §9.5).
- Branch convention (in the prompt, not enforced by code): `issuebot/<number>-<slug>`,
  created with `gh issue develop <number> --name ...` so GitHub links it to the issue.
  PR bodies include `Closes #<number>` so a merge closes the issue.
- Workspaces persist across runs and are removed only when the issue reaches a
  terminal state (with `before_remove`), at startup for already-terminal issues, and
  never on success.

### 2.6 Prompt (policy layer)

issuebot ships a default `WORKFLOW.md` adapted from Symphony's: workpad comment on
the issue (`## Issuebot Workpad`, edited in place via `gh api`), plan and acceptance
criteria, reproduce-first, PR feedback sweep, completion bar before `review`,
rework flow, and the follow-up-issue rule. Follow-up issues are created with
`gh issue create` **without** `issuebot/todo`, so a human triages them before the
bot picks them up; the prompt asks for a `related to #N` line in the body.

The template receives `issue` (normalised fields plus `pr` if a linked PR exists),
`attempt`, `turn_number`, `max_turns`, `rework` (bool) and `self_review` (bool).
Strict rendering: unknown variables and filters fail the attempt.

**Code review is layered, and `review` keeps meaning "a human's turn".** Three
layers, none of which adds a label:

1. **In-run review (Phase 3, prompt).** Before opening the PR, the agent runs a
   fresh-context review of `git diff origin/main...HEAD` (the code-review skill at
   low effort, or a review subagent with a review prompt) and fixes every Critical
   and Important finding. Gated by `agent.self_review` (default on) so cost can be
   traded for polish per workflow. A review the agent spawns itself is a first gate,
   not an independent one.
2. **Independent review on the PR (repository configuration, from Phase 4).** The
   Claude Code GitHub Action reviews every pull request on open, agent-authored or
   human-authored, and posts its findings as PR review comments. The prompt's PR
   feedback sweep already treats every actionable bot or human comment as blocking:
   the agent addresses or explicitly rebuts each one and never sets `issuebot/review`
   while actionable comments remain or checks are failing. A review that lands after
   the agent's turn ended is picked up by the next continuation turn (§2.4), because
   the issue stays `in-progress` until the agent moves it.
3. **Human review** at `issuebot/review`, unchanged.

A separate issuebot-owned reviewer agent with its own state was considered and
deferred (see Later): it would double agent spawns and add an implementer-reviewer
loop to the orchestrator for little that layers 1 and 2 do not already provide.

### 2.7 Persistence (observability store)

PostgreSQL holds history only. Tables:

| Table | Purpose |
|---|---|
| `issues` | Latest snapshot per tracked issue: number, title, state label, GitHub state, url, linked PR, timestamps. Feeds the Kanban without hitting GitHub per page view |
| `runs` | One row per worker session: issue, attempt, session id, started/ended, outcome, turns, tokens, cost, error, workspace and log paths. "Agents spun up" counts these |
| `events` | Append-only: timestamp, issue, run, kind, jsonb payload. Kinds include `state_changed`, `run_started`, `run_ended`, `pr_opened`, `issue_completed`, `notification_sent`. Time series come from `date_trunc` over this table |
| `runtime_snapshot` | Single row: the worker's in-memory snapshot, rewritten every tick |

Migrations are numbered `.sql` files applied by `issuebot migrate` (also run by the
compose entrypoint). No ORM: the schema is four tables and raw SQL keeps queries
explicit and reviewable.

### 2.8 Dashboard

Server-rendered FastAPI + Jinja2 pages with HTMX polling every few seconds, Chart.js
for the time series, both vendored into `static/` so the container has no CDN
dependency. Pages: `/` (hero stats: closed in 1d/7d, agents spun up 1d/7d, running
now, cost; two charts for the 30-day daily series; Kanban with the five label
columns; running-agents panel with last event, turn count, tokens, cost),
`/issues/<n>` (run history, recent events, log links). API: `GET /api/v1/state`,
`GET /api/v1/issues/<n>`, `GET /api/v1/stats?window=`, `POST /api/v1/refresh`,
`GET /healthz`. Shapes follow Symphony §13.7.2 with `claude_totals` in place of
`codex_totals`.

### 2.9 Security posture

Single-tenant, high-trust, intended for repositories the operator owns:

- The container is the sandbox. `auto` permission mode inside it; the workspace
  volume is the only writable path the agent needs.
- GitHub token is a fine-grained PAT (or GitHub App installation token, later) scoped
  to the one repository with contents, issues and pull-requests write. Branch
  protection on `main` makes AGENTS.md's "never push to main" a server-side rule.
- Issue bodies are untrusted input. Dispatch requires a human-applied label.
- Tokens are never logged; config validation reports presence, not value.
- Hooks in `WORKFLOW.md` are trusted configuration, run with a timeout, output
  truncated in logs (Symphony §15.4).

### 2.10 Testing strategy

- `pytest` + `pytest-asyncio`; `ruff` for lint and format; both in pre-commit and CI.
- **Fake `claude`**: a script placed first on `PATH` in tests that emits scripted
  `stream-json` (init, a few assistant/tool events, result) with configurable delays,
  failures and exit codes. Exercises the runner, session-id capture, stall and turn
  timeouts, budget errors.
- **Fake GitHub**: an in-memory implementation of the adapter protocol holding
  issues, labels, PRs and comments. The orchestrator and state-machine tests run
  entirely against it. A thin layer of tests covers the real `gh` wrapper against
  recorded JSON output.
- **PostgreSQL**: CI uses a service container; locally `docker compose up db`. DB
  tests read `DATABASE_URL` and are skipped, and reported as skipped, when it is unset.
- **Live integration** (opt-in, env-gated as in Symphony §17.8): a scratch repository,
  real `gh`, real or fake `claude`, one issue end to end.

### 2.11 Configuration schema

`WORKFLOW.md` front matter. Defaults in brackets.

```yaml
github:
  repo: owner/name              # required
  # token: omitted → GH_TOKEN from the environment; an explicit $VAR must be set
  labels:                       # override label names if desired
    todo: issuebot/todo
    in_progress: issuebot/in-progress
    review: issuebot/review
    rework: issuebot/rework
    complete: issuebot/complete
  request_timeout_ms: 30000
polling:
  interval_ms: 30000
workspace:
  root: /workspaces             # [$ISSUEBOT_WORKSPACE_ROOT or /workspaces]
hooks:
  after_create: |               # runs after the built-in clone
  before_run: |
  after_run: |
  before_remove: |
  timeout_ms: 60000
agent:
  max_concurrent_agents: 3
  max_turns: 5                  # claude -p invocations per worker session
  max_attempts: 3               # failed worker sessions before the blocked escape
  max_retry_backoff_ms: 300000
  self_review: true             # fresh-context review of the diff before the PR is opened
claude:
  command: claude               # executable; args are owned by issuebot
  model: null                   # pass-through to --model when set
  permission_mode: auto
  max_budget_usd: 5.0           # per turn
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
  allowed_tools: []
  disallowed_tools: []
  append_system_prompt: null
  setting_sources: null         # pass-through to --setting-sources; the dogfood file sets [project]
database:
  url: $DATABASE_URL            # optional; worker runs without it
notifications:
  slack:
    webhook_url: $SLACK_WEBHOOK_URL   # optional
    events: [state_changed, blocked]
server:
  port: 8080
  bind: 0.0.0.0                 # container default; 127.0.0.1 outside Docker
```

The Markdown body is the prompt template. `issuebot validate` checks the file, the
environment variables it references, and that `gh` and `claude` are on `PATH` and
authenticated.

## 3. Phases

Ordering rule: phases 1 to 4 build the working loop and are strictly sequential. From
the end of Phase 4 issuebot can work on its own issues, so phases 5 to 7 can be filed
as `issuebot/todo` issues in this repo and partly delegated to it. Phase 5 (Slack) is
independent of 6 and 7 and can move later if the dashboard is the priority.

Each phase lists what it exposes to later phases ("Interfaces") so per-phase specs can
be written against a known boundary.

### Phase 1: Foundations

**Goal.** A runnable, tested, containerised Python project with configuration
loading, before any GitHub or Claude integration.

**Scope.**

- `pyproject.toml` (uv, Python 3.14, `ruff`, `pytest`, `pytest-asyncio`); `src`
  layout under `issuebot/`; `uv.lock`.
- `issuebot.config`: `WORKFLOW.md` loader (front matter split, non-map error,
  missing-file error), pydantic settings models for the schema in §2.11, defaults,
  `$VAR` and `~` resolution, validation error surface (Symphony §5.5, §6.1).
- `issuebot.logging`: structured JSON logs via `structlog`, with `issue_number`,
  `issue_identifier`, `session_id` context binding (Symphony §13.1).
- `issuebot.events`: the bus and the log sink. Event dataclasses for the kinds in
  §2.7 so later phases publish, not print.
- CLI skeleton (`argparse`): `issuebot --version`, `issuebot validate`.
- `Dockerfile` (multi-stage; installs `git`, `gh`, `claude`, `uv`), `compose.yaml` with
  `worker`, `web`, `db` (worker and web are stubs that print "not implemented" and
  exit 0 until their phases land).
- GitHub Actions: `ci.yml` (ruff, pytest with a postgres service, docker build);
  `dependabot.yml` for pip, docker and github-actions.
- `.pre-commit-config.yaml` kept as is (already has ruff); `CLAUDE.md` updated with
  the build/lint/test commands.

**Out of scope.** Any call to `gh` or `claude`.

**Interfaces.** `load_workflow(path) -> Workflow`, `Workflow.config: Settings`,
`Workflow.prompt_template: str`, `EventBus.publish(event)`, `EventSink` protocol.

**Done when.** `uv run pytest` and `uv run ruff check` pass locally and in CI;
`docker compose build` succeeds; `issuebot validate` reports a good and a bad
`WORKFLOW.md` correctly.

### Phase 2: GitHub adapter and label state machine

**Goal.** Everything issuebot needs to read and write issue state through `gh`, with
a fake for tests.

**Scope.**

- `Issue` model normalised as in Symphony §4.1.1 and §11.3: `id` (number as string),
  `identifier` (`<repo>-<number>`), `number`, `title`, `body`, `state_label`,
  `github_state`, `labels` (lowercased), `url`, `assignees`, `created_at`, `updated_at`,
  `dispatchable` (false for pull requests and for issues with no `issuebot/*` label),
  `linked_pr` (number, url, state, merged) or null.
- `GitHubAdapter` protocol: `fetch_issues_by_states(labels)`,
  `fetch_issues_by_ids(numbers)`, `fetch_terminal_issues()` (closed issues still
  carrying a state label), `set_state(number, label)`, `comment(number, body)`,
  `find_workpad_comment(number)`, `update_comment(id, body)`, `ensure_labels()`.
- `GhCli` implementation: `gh issue list --json`, `gh issue view --json`,
  `gh issue edit --add-label/--remove-label`, `gh api graphql` for
  `closedByPullRequestsReferences`; error mapping to the categories in Symphony §11.4;
  rate-limit awareness (`gh api rate_limit` logged when low).
- `FakeGitHub`: in-memory adapter with the same protocol plus test helpers to add
  issues, apply labels as "the human", open and merge PRs, close issues.
- `issuebot.github.state`: the transition table from §2.3 as data, plus
  `is_active`, `is_terminal`, `next_state_for(issue)`; a pure function
  `classify_closed(issue) -> complete | cancelled`.
- CLI: `issuebot labels ensure`, `issuebot issues list` (a table of tracked issues by
  state).

**Out of scope.** Anything that runs Claude; scheduling.

**Interfaces.** `GitHubAdapter` protocol and `Issue` model are frozen for phases 3
and 4; `FakeGitHub` is the test double they use.

**Done when.** Adapter tests pass against `FakeGitHub` and against recorded `gh`
output; `issuebot labels ensure` creates the five labels in a scratch repo
idempotently; `issuebot issues list` shows them.

### Phase 3: Agent runner

**Goal.** Run one Claude session for one issue in an isolated workspace, in the
foreground, with a fake `claude` for tests. This is where the prompt gets written.

**Scope.**

- `issuebot.agent.workspace`: key derivation with sanitisation and hash suffix,
  root containment, create-or-reuse, built-in shallow clone via `gh repo clone`,
  the four hooks with timeout and truncated logging, `remove()` with `before_remove`,
  `.issuebot/session.json` read and write (session id, attempt, turn count,
  last outcome).
- `issuebot.agent.prompt`: Jinja2 strict environment, render with the variables in
  §2.6, continuation prompt, rework context (PR review comments summarised from
  `gh`).
- `issuebot.agent.runner`: build the `claude -p` argv from `claude.*` settings, spawn
  with `cwd=workspace` and a minimal environment (pass through `GH_TOKEN`, Claude
  auth, `PATH`, `HOME`), read `stream-json` lines, emit runtime events to the bus
  (`session_started`, `turn_activity`, `turn_completed`, `turn_failed`,
  `turn_timeout`, `process_exit`), enforce `turn_timeout_ms`, kill on cancel, capture
  stderr to a per-run log file, extract usage and cost from `result`.
- `issuebot.agent.session`: the multi-turn worker loop from §2.4 (turn, re-fetch,
  continue or stop), returning a `RunOutcome`.
- Default `WORKFLOW.md` for this repository (dogfood policy) adapted from Symphony's
  template to GitHub labels and `gh`, including the in-run review step from §2.6:
  when `self_review` is on, the agent must run a fresh-context review of its diff and
  resolve Critical and Important findings before opening the PR, and the completion
  bar before `issuebot/review` requires green checks and no actionable review
  comments (bot or human) on the PR.
- `agent.self_review` setting (bool, default `true`), passed to the template as
  `self_review`.
- Test fixtures: fake `claude` script; recorded `stream-json` samples.
- CLI: `issuebot run-once <number>` runs a full worker session in the foreground with
  logs to the terminal. This is the tool for iterating on the prompt.

**Out of scope.** Polling, claims, retries, concurrency; the GitHub review action
(Phase 4).

**Interfaces.** `run_session(issue, settings, adapter, bus, cancel) -> RunResult`;
`WorkspaceManager`; runtime event kinds.

**Done when.** With a real `claude`, `issuebot run-once` against a trivial issue in a
scratch repo produces a branch, a PR with `Closes #N`, and the `review` label, and the
run's transcript shows the review pass before the PR was opened; all runner behaviour
is covered by tests using the fake `claude`.

### Phase 4: Orchestrator

**Goal.** The long-running worker: poll, claim, dispatch, retry, reconcile, recover.
End of this phase is the first dogfooding milestone.

**Scope.** Symphony §7, §8, §14 and §16, with the label vocabulary from §2.3:

- Runtime state (`running`, `claimed`, `retry_attempts`, `completed`, totals) owned by
  one asyncio task; workers are child tasks reporting back through a queue.
- Poll tick: reconcile, mtime-based `WORKFLOW.md` reload and preflight validation,
  fetch candidates for `todo`, `rework` and orphaned `in-progress`, sort (oldest first;
  `rework` before `todo`), dispatch while slots remain, publish snapshot.
- Dispatch sets `in-progress` before the worker starts; failure to set the label
  aborts the dispatch.
- Retry queue: 1 s continuation retry after normal exit, `10000 * 2^(attempt-1)`
  capped at `max_retry_backoff_ms` after failure; `max_attempts` then the blocked
  escape (`review` plus blocker comment).
- Reconciliation: stall detection; label refresh for running issues (moved to
  `review`/`rework` by a human mid-run stops the worker without cleanup; closed stops
  it and, if a linked PR merged, sets `complete`, otherwise strips labels; either way
  the workspace is removed).
- Startup: terminal-workspace sweep; resume orphaned `in-progress` issues from
  `.issuebot/session.json`.
- Graceful shutdown on SIGTERM: cancel workers, wait for `after_run` hooks, exit.
- Refresh trigger: an in-process `request_refresh()` (wired to PostgreSQL `NOTIFY` in
  Phase 6).
- CLI: `issuebot worker [--workflow PATH]`; compose `worker` service becomes real.
- Repository chore for the dogfooding milestone: install the Claude Code GitHub
  Action with an automated review prompt on pull-request events (needs an
  `ANTHROPIC_API_KEY` repository secret, which is the user's call). issuebot's code
  needs nothing for it; the prompt's feedback sweep and the continuation turns are
  what make the action's comments reach the agent (§2.6, layer 2). Deferred on
  2026-09-03 (Phase 4 spec, decision 12): unguarded, a missing secret fails every
  pull-request check, which the agent's completion bar treats as blocking. It becomes
  a chore issue issuebot can take once it is dogfooding.

**Out of scope.** Database, dashboard, Slack.

**Interfaces.** `Orchestrator.snapshot() -> RuntimeSnapshot` (the shape of
`/api/v1/state`); `request_refresh()`; events `run_started`, `run_ended`,
`state_changed`, `blocked`, `issue_completed`, `issue_cancelled`.

**Done when.** Orchestrator tests against `FakeGitHub` and the fake `claude` cover the
Symphony §17.4 matrix (dispatch order, claims, per-state stop and cleanup,
continuation and failure retries, backoff cap, stall kill, slot exhaustion, restart
resume, blocked escape). `docker compose up worker` against this repository takes a
`todo` issue to a PR and `review`, and a merged PR to `complete`.

### Phase 5: Slack notifications

**Goal.** A Slack channel sees every state transition and every blocked or failed run.

**Scope.**

- `SlackSink` on the event bus: incoming webhook, one message per subscribed event,
  formatted with issue link, transition, PR link when present, and for `blocked` the
  blocker summary. Bounded retry on 429/5xx; failure logged, never blocks the
  orchestrator.
- `notifications.slack` settings from §2.11 with an event allow-list.
- Tests with a local HTTP fake; `issuebot validate` checks the webhook URL is set
  when Slack is configured.

**Out of scope.** Slack app, threads, interactive buttons.

**Done when.** Running the worker with a webhook configured posts messages for
`todo → in-progress → review → complete` on a scratch issue.

Decided 2026-09-03 (Phase 5 spec): the sink lives in `issuebot.notifications` (a
settings-taking sink inside `issuebot.events` would close an import cycle with
`config`); the transport is stdlib `urllib` in a worker thread, no new dependency;
the allow-list is by kind, `run_ended` covers every outcome and stays opt-in, so a
failed run reaches the channel through `blocked` by default; `NotificationSent` is
published after each delivery and never re-notified; `validate` warns when the
webhook is unset, requires `https`, and gains `--slack-probe`; `run-once` posts too;
a webhook or allow-list change needs a restart. Deferred: Block Kit layouts and
attachments, per-channel routing, a reload hook for `notifications.*`.

### Phase 6: Persistence

**Goal.** History survives restarts and the stats the dashboard needs can be queried.

**Scope.**

- Schema and migrations for the four tables in §2.7; `issuebot migrate`; compose
  entrypoint runs it for `worker` and `web`.
- `PostgresSink`: writes `events`, upserts `runs` on `run_started`/`run_ended`,
  upserts `issues` on every poll, rewrites `runtime_snapshot` on every tick.
  Connection loss is logged and retried in the background; the worker never waits on
  it.
- `issuebot.db.queries`: `closed_count(window)`, `runs_count(window)`,
  `daily_series(days)`, `issues_by_state()`, `runs_for_issue(n)`,
  `recent_events(n)`, `snapshot()`.
- `LISTEN issuebot_refresh` in the worker wired to `request_refresh()`.
- CLI: `issuebot status` (prints the snapshot from the database), `issuebot stats`.
- CI already provides a postgres service from Phase 1; DB tests activate.

**Out of scope.** HTTP.

**Interfaces.** The query module's return types are the dashboard's view models.

**Done when.** After a dogfooding run, `issuebot stats` shows correct 1-day and
7-day counts and a daily series; killing and restarting `worker` preserves history
and the snapshot row recovers within one tick.

### Phase 7: Web dashboard

**Goal.** The blueprint's Kanban, hero stats and time series, plus the JSON API.

**Scope.**

- FastAPI app with the routes in §2.8; Jinja2 templates; HTMX partial refresh for
  the Kanban, hero stats and running panel; Chart.js for the two 30-day charts;
  vendored static assets.
- `POST /api/v1/refresh` issues `NOTIFY issuebot_refresh`.
- `/healthz` checks the database and reports snapshot age.
- CLI `issuebot web [--port]`; compose `web` service becomes real; `server.*`
  settings.
- Tests: API contract tests with a seeded database; template smoke tests.

**Out of scope.** Authentication (bind loopback or put it behind a reverse proxy),
write actions on issues from the UI.

**Done when.** `docker compose up` shows the Kanban reflecting label changes within
one poll interval, hero stats matching `issuebot stats`, charts rendering, and the
API returning the Symphony-shaped documents.

### Later (not scheduled)

Recorded so the phase specs do not accidentally absorb them: GitHub App
authentication; multiple target repositories per worker; GitHub webhooks instead of
polling; per-issue log viewer in the dashboard; cost budgets per issue and per day;
SSH or remote workers (Symphony Appendix A); Windows host support for the worker
itself (the repo's cross-OS rule applies to scripts the agent writes, the service is
Linux-in-Docker); an issuebot-owned reviewer agent with its own state or label, to be
revisited only if the in-run review and the GitHub review action (§2.6) prove
insufficient; git worktrees off a shared base clone as the workspace
implementation, a disk and clone-time optimisation over per-issue clones that
would share one `.git` between concurrent agents. A `since` filter on
`fetch_terminal_issues`, so the terminal sweep stops re-reading every completed issue the
repository has (Phase 4 bounds it to every tenth tick instead).

## 4. Decisions to confirm

Each was made to keep moving; any can be changed before Phase 1 without cost.

1. **`claude -p` per turn with `--resume`, not the Claude Agent SDK.** Matches the
   blueprint and keeps the runner a subprocess boundary that is easy to fake in
   tests. The SDK would allow in-process tool interception but couples issuebot to
   the SDK release cycle.
2. **Orchestrator setting `in-progress`, agent setting `review`.** The claim must be
   reliable and immediate, so the service owns it. The agent knows when the PR is
   ready, so it owns `review`, with the orchestrator's blocked escape as backstop.
3. **PostgreSQL is optional for the worker.** Correctness is label- and
   filesystem-driven; the database is history and dashboard only. This is why
   persistence is Phase 6, after the loop works.
4. **Rework reuses the branch and PR** rather than Symphony's close-and-restart.
5. **Blocked runs park in `review` with a comment**, no sixth label.
6. **One target repository per worker instance.** Multi-repo is listed under Later.
7. **Server-rendered dashboard (FastAPI + Jinja2 + HTMX + Chart.js)**, no SPA, no
   Node toolchain.
8. **Raw SQL with psycopg 3 and numbered migrations**, no ORM, no Alembic.
9. **Slack via incoming webhook**, not a Slack app.
10. **`argparse` for the CLI; `pydantic` for config; `structlog` for logs; `uv` for
    packaging.**
11. **Follow-up issues are created unlabelled** so a human decides whether the bot
    takes them.
12. **A thin CLI exists (`validate`, `labels ensure`, `run-once`, `status`, `refresh`,
    `stats`)** but recovery after a connectivity drop is automatic (retry with backoff,
    restart resume). This answers the blueprint's open query: the CLI is for
    inspection and prompt iteration, not the recovery path.
13. **Code review is layered without a sixth label** (decided 2026-09-02): an in-run
    fresh-context review gated by `agent.self_review` (Phase 3), the Claude Code
    GitHub Action reviewing every PR (repository configuration, Phase 4 milestone),
    then the human at `issuebot/review`. A separate reviewer agent with its own state
    is deferred to Later.

## 5. Suggested file naming for the per-phase documents

```
docs/superpowers/specs/YYYY-MM-DD-phase-1-foundations-design.md
docs/superpowers/plans/YYYY-MM-DD-phase-1-foundations.md
docs/superpowers/specs/YYYY-MM-DD-phase-2-github-adapter-design.md
...
```

Each spec should restate only its own scope and interfaces and link back here for
the architecture, the label state machine and the configuration schema.
