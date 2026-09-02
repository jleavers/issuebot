# Phase 1: Foundations

Status: Draft for review (2026-09-02)

Parent: [Issuebot phased design](2026-09-02-issuebot-phased-design.md), Phase 1.
This spec owns the detail of Phase 1 only. The architecture, label state machine,
configuration schema and decisions register live in the parent and are not restated
except where this phase pins them down.

## 1. Goal

A runnable, tested, containerised Python project that can load and validate a
`WORKFLOW.md`, log in a structured way, and publish domain events to sinks. No call
to `gh` or `claude` is made in this phase. Everything later phases build on
(configuration types, logging conventions, the event bus, the CLI shape, the image,
CI) is fixed here so they can add modules without touching the foundations.

Out of scope: any GitHub or Claude interaction, scheduling, database schema, HTTP.

## 2. Repository layout after this phase

```
.
├── .dockerignore
├── .env.example
├── .github/
│   ├── dependabot.yml
│   └── workflows/ci.yml
├── .pre-commit-config.yaml         (unchanged)
├── .python-version                 3.14
├── AGENTS.md                       (unchanged)
├── CLAUDE.md                       (commands section updated)
├── Dockerfile
├── README.md                       (development section added)
├── WORKFLOW.md                     dogfood config; placeholder prompt until Phase 3
├── compose.yaml
├── docs/…
├── pyproject.toml
├── uv.lock
├── src/issuebot/
│   ├── __init__.py                 __version__
│   ├── __main__.py                 python -m issuebot
│   ├── cli.py                      argparse entry point
│   ├── log.py                      structlog configuration and context helpers
│   ├── config/
│   │   ├── __init__.py             public re-exports
│   │   ├── errors.py               ConfigError hierarchy
│   │   ├── workflow.py             WORKFLOW.md parsing and load_workflow
│   │   ├── resolve.py              $VAR, ~ and relative-path resolution
│   │   └── settings.py             pydantic models
│   └── events/
│       ├── __init__.py             public re-exports
│       ├── types.py                Event dataclasses and EVENT_KINDS
│       ├── bus.py                  EventBus and EventSink protocol
│       └── log_sink.py             LogSink
└── tests/
    ├── conftest.py
    ├── fixtures/workflows/         sample WORKFLOW.md files
    ├── test_cli.py
    ├── test_events.py
    ├── test_log.py
    ├── test_resolve.py
    ├── test_settings.py
    └── test_workflow.py
```

`src` layout; package name `issuebot`; console script `issuebot = issuebot.cli:main`.
The module is `issuebot.log`, not `issuebot.logging`, to avoid shadowing the
standard library inside the package.

## 3. Tooling and conventions

| Concern | Choice |
|---|---|
| Python | 3.14 (`requires-python = ">=3.14"`, `.python-version` = `3.14`) |
| Packaging | `uv`; `uv.lock` committed; `uv sync --frozen` everywhere |
| Build backend | `hatchling` |
| Lint and format | `ruff` (`target-version = "py314"`, line length 100, rules `E F I UP B N SIM RUF`); `ruff format` |
| Tests | `pytest`, `pytest-asyncio` (`asyncio_mode = "auto"`), `testpaths = ["tests"]` |
| Runtime deps | `pydantic>=2.12`, `pyyaml`, `structlog` |
| Dev deps | `pytest`, `pytest-asyncio`, `ruff`, `pre-commit` |
| Version | `issuebot.__version__` read from package metadata; `0.1.0` in `pyproject.toml` |

The `ruff` version pinned in dev dependencies must match the `ruff-pre-commit` rev
in `.pre-commit-config.yaml`; the plan bumps the pre-commit revs to current
(`pre-commit-hooks` v6.0.0, `ruff-pre-commit` v0.16.5) and pins `ruff==0.16.5`.
Dependabot does not update pre-commit revs, so `pre-commit autoupdate` is a manual
step whenever the uv-managed `ruff` moves.

## 4. Configuration

### 4.1 `WORKFLOW.md` parsing

`parse_workflow_text(text) -> (raw_config: dict, body: str)` is pure:

- Normalise `\r\n` to `\n` first (the repo is edited from Windows hosts).
- If the text starts with a line that is exactly `---`, the front matter is every
  line up to the next line that is exactly `---`; the body is everything after.
  No closing `---` is a `workflow_parse_error`.
- Front matter is parsed with `yaml.safe_load`. `None` (empty block) becomes `{}`.
  Any non-mapping result is `workflow_front_matter_not_a_map`. YAML errors are
  `workflow_parse_error` carrying the YAML message.
- No front matter: `raw_config = {}` and the whole text is the body.
- The body is stripped of leading and trailing whitespace. An empty body is
  allowed here; `validate` warns about it.

`load_workflow(path, *, environ=None) -> Workflow`:

1. Read the file; a missing or unreadable file is `missing_workflow_file`.
2. `parse_workflow_text`.
3. Resolve designated fields (section 4.3) against `environ` (default
   `os.environ`) and the workflow's directory.
4. Validate into `Settings`; pydantic errors become one `invalid_settings` error
   listing every failing field path and message.

```python
@dataclass(frozen=True)
class Workflow:
    path: Path                 # absolute
    config: Settings
    prompt_template: str
    raw_config: dict[str, Any] # front matter as parsed, before resolution
    source_mtime_ns: int       # for Phase 4 hot reload
```

### 4.2 Settings schema

All models use `extra="forbid"`. A misspelt key is an error naming the key. This
deliberately departs from Symphony's "ignore unknown keys": issuebot is a single
implementation with no extension ecosystem, and silent typos are the more likely
failure.

| Field | Type and constraint | Default |
|---|---|---|
| `github.repo` | str matching `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$` | required |
| `github.token` | `SecretStr \| None`, resolved (4.3) | env `GH_TOKEN` |
| `github.labels.todo` | non-empty str | `issuebot/todo` |
| `github.labels.in_progress` | non-empty str | `issuebot/in-progress` |
| `github.labels.review` | non-empty str | `issuebot/review` |
| `github.labels.rework` | non-empty str | `issuebot/rework` |
| `github.labels.complete` | non-empty str | `issuebot/complete` |
| (labels) | the five values must be distinct | |
| `polling.interval_ms` | int ≥ 1000 | 30000 |
| `workspace.root` | `Path`, absolute after resolution (4.3) | env `ISSUEBOT_WORKSPACE_ROOT`, else `/workspaces` |
| `hooks.after_create` | `str \| None`, verbatim | `None` |
| `hooks.before_run` | `str \| None`, verbatim | `None` |
| `hooks.after_run` | `str \| None`, verbatim | `None` |
| `hooks.before_remove` | `str \| None`, verbatim | `None` |
| `hooks.timeout_ms` | int ≥ 1 | 60000 |
| `agent.max_concurrent_agents` | int ≥ 1 | 3 |
| `agent.max_turns` | int ≥ 1 | 5 |
| `agent.max_attempts` | int ≥ 1 | 3 |
| `agent.max_retry_backoff_ms` | int ≥ 1000 | 300000 |
| `claude.command` | non-empty str (executable name or path) | `claude` |
| `claude.model` | `str \| None` | `None` |
| `claude.permission_mode` | one of `auto`, `acceptEdits`, `dontAsk`, `bypassPermissions` | `auto` |
| `claude.max_budget_usd` | float > 0 | 5.0 |
| `claude.turn_timeout_ms` | int ≥ 1 | 3600000 |
| `claude.stall_timeout_ms` | int; ≤ 0 disables | 300000 |
| `claude.allowed_tools` | `list[str]` | `[]` |
| `claude.disallowed_tools` | `list[str]` | `[]` |
| `claude.append_system_prompt` | `str \| None` | `None` |
| `database.url` | `SecretStr \| None`, resolved (4.3) | env `DATABASE_URL` |
| `notifications.slack.webhook_url` | `SecretStr \| None`, resolved (4.3) | env `SLACK_WEBHOOK_URL` |
| `notifications.slack.events` | `list[str]`, each a member of `EVENT_KINDS` (section 6) | `["state_changed", "blocked"]` |
| `server.port` | int in 0..65535 | 8080 |
| `server.bind` | non-empty str | `0.0.0.0` |

`permission_mode` excludes `plan` and `manual` because they cannot run unattended.
Hook scripts are never resolved or interpolated: `$` inside them belongs to the
shell.

### 4.3 Environment and path resolution

Applied to the raw mapping before validation, by `issuebot.config.resolve`:

- **Designated secret fields** `github.token`, `database.url`,
  `notifications.slack.webhook_url`, each with a fallback variable (`GH_TOKEN`,
  `DATABASE_URL`, `SLACK_WEBHOOK_URL`):
  - value absent → fallback variable if set and non-empty, else `None`;
  - value matches `^\$[A-Za-z_][A-Za-z0-9_]*$` → that variable; unset or empty is
    `missing_environment_variable` naming the variable and the field;
  - any other string → used literally (`validate` warns; see section 7).
- **Designated path field** `workspace.root` with fallback `ISSUEBOT_WORKSPACE_ROOT`
  and default `/workspaces`:
  - same `$VAR` and fallback rules as above, then `~` expansion, then relative
    paths are resolved against the directory containing `WORKFLOW.md`, then
    normalised to absolute (`Path.resolve(strict=False)`; the directory need not
    exist yet).
- No other field is touched. There is no embedded interpolation (`prefix-$VAR`).

`environ` is injectable so tests never depend on the real environment.

### 4.4 Errors

```python
class ConfigError(Exception):            # code: str, message: str, path: Path | None
class MissingWorkflowFile(ConfigError)   # code = "missing_workflow_file"
class WorkflowParseError(ConfigError)    # code = "workflow_parse_error"
class FrontMatterNotAMap(ConfigError)    # code = "workflow_front_matter_not_a_map"
class MissingEnvironmentVariable(ConfigError)  # code = "missing_environment_variable"; .variable, .field
class SettingsValidationError(ConfigError)     # code = "invalid_settings"; .errors: list[tuple[str, str]]
```

`str(error)` is a single operator-readable line; `SettingsValidationError` lists
each `field.path: message` on following lines.

### 4.5 Public API

`issuebot.config` exports `load_workflow`, `parse_workflow_text`, `Workflow`,
`Settings` and its sub-models, and the error classes. Nothing else in the package
imports `pydantic` directly; later phases consume `Settings`.

## 5. Logging

`issuebot.log`:

```python
def configure_logging(*, level: str = "INFO", fmt: Literal["json", "console"] = "json",
                      stream: TextIO = sys.stderr) -> None
def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger
def bind_issue_context(*, issue_number: int, issue_identifier: str) -> None
def bind_session_context(*, session_id: str) -> None
def clear_context() -> None
```

- Logs go to **stderr**. Stdout is reserved for CLI output so `issuebot validate
  --show-config | …` is usable.
- `json` renders one JSON object per line with `timestamp` (ISO 8601 UTC),
  `level`, `logger`, `event`, then bound context and call-site keys. `console` is
  structlog's coloured developer renderer.
- Context binding uses `structlog.contextvars`, so it follows asyncio tasks. The
  helper names fix the field names the Symphony spec requires (§13.1):
  `issue_number`, `issue_identifier`, `session_id`.
- Standard-library `logging` is routed through the same processors
  (`ProcessorFormatter`) so third-party libraries added later (uvicorn, psycopg)
  produce identical lines.
- Defaults come from `ISSUEBOT_LOG_LEVEL` and `ISSUEBOT_LOG_FORMAT`; CLI flags
  override.
- `SecretStr` values render as `**********`; nothing in this phase logs secrets,
  and `validate` reports presence only.

## 6. Events

`issuebot.events.types` defines frozen, keyword-only dataclasses. `kind` is a class
attribute; `at` defaults to now (UTC, timezone-aware). `to_dict()` returns a
JSON-safe mapping with `kind`, `at` (ISO 8601) and every field.

| Class | `kind` | Fields beyond `issue_number`, `issue_identifier` |
|---|---|---|
| `StateChanged` | `state_changed` | `from_label: str \| None`, `to_label: str \| None`, `actor: "issuebot" \| "agent" \| "human"`, `pr_url: str \| None` |
| `RunStarted` | `run_started` | `run_id: str`, `attempt: int`, `session_id: str \| None`, `workspace_path: str` |
| `RunEnded` | `run_ended` | `run_id`, `outcome: "succeeded" \| "failed" \| "timed_out" \| "stalled" \| "cancelled"`, `error: str \| None`, `turns: int`, `input_tokens: int`, `output_tokens: int`, `cost_usd: float`, `duration_s: float` |
| `PrOpened` | `pr_opened` | `pr_number: int`, `pr_url: str` |
| `Blocked` | `blocked` | `reason: str` |
| `IssueCompleted` | `issue_completed` | `pr_url: str \| None` |
| `IssueCancelled` | `issue_cancelled` | `reason: str` |
| `NotificationSent` | `notification_sent` | `channel: str`, `about_kind: str` |

`EVENT_KINDS: frozenset[str]` lists every kind and is what
`notifications.slack.events` validates against. Phase 3 adds agent runtime events
in its own module using the same base class.

`issuebot.events.bus`:

```python
class EventSink(Protocol):
    name: str
    def handle(self, event: Event) -> None: ...

class EventBus:
    def __init__(self, sinks: Iterable[EventSink] = ()) -> None
    def add_sink(self, sink: EventSink) -> None
    def remove_sink(self, name: str) -> None
    def publish(self, event: Event) -> None
    failures: dict[str, int]   # per-sink count of swallowed exceptions
```

`publish` is synchronous and calls every sink in order. A sink that raises is logged
at ERROR with the sink name and event kind, its failure count incremented, and the
remaining sinks still run. The bus never raises. Sinks that do IO (PostgreSQL,
Slack in later phases) enqueue in `handle` and drain from their own task, so
`publish` stays cheap and safe to call from the orchestrator's state task.

`LogSink` (`name = "log"`) logs each event at INFO with `event=<kind>` and the
event's fields as keys. It is always installed.

## 7. CLI

`argparse`, entry point `issuebot.cli:main(argv: Sequence[str] | None = None) -> int`.

```
issuebot [--log-level LEVEL] [--log-format json|console] <command>
issuebot --version
issuebot validate [--workflow PATH] [--show-config]
```

- `--workflow` default: `ISSUEBOT_WORKFLOW` if set, else `./WORKFLOW.md` (Symphony
  §5.1 precedence: explicit, then cwd default).
- `validate` prints one line per check to stdout in the form
  `[ OK ]`, `[WARN]`, `[FAIL]` followed by `<subject>: <detail>`, then a summary
  line. Exit codes: `0` no failures, `1` at least one failure, `2` the workflow
  could not be loaded (the error is printed and no checks run).

Checks, in order:

| Subject | Result |
|---|---|
| `workflow` | OK with the absolute path |
| `github.repo` | OK with the value |
| `github.token` | OK "set (from GH_TOKEN)" / OK "set (from $NAME)" / WARN "literal value in WORKFLOW.md; prefer $VAR" / FAIL "not set; export GH_TOKEN or set github.token: $VAR" |
| `workspace.root` | OK with the resolved path; WARN if the parent directory does not exist |
| `claude.command` | OK with the path found by `shutil.which`; FAIL if not found |
| `gh` | OK with the path found; FAIL if not found |
| `database.url` | OK "configured" / OK "not configured (history and dashboard disabled)" |
| `notifications.slack` | OK "configured" / OK "not configured" |
| `prompt` | OK with the body length; WARN if empty |

Presence on `PATH` is the whole check for executables in this phase; authentication
checks arrive with the phases that call them.

`--show-config` prints the effective settings after the checks as YAML on stdout,
via `Settings.model_dump(mode="json")`, which renders every `SecretStr` as
`**********`.

`issuebot` with no command prints help and exits `2`. Unknown commands do the same.

## 8. Container image and compose

### 8.1 `Dockerfile`

Multi-stage:

1. `builder` from `python:3.14-slim`; `COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/`;
   `uv sync --frozen --no-dev --no-install-project` with only `pyproject.toml` and
   `uv.lock` copied (dependency layer cache); then copy `src/` and
   `uv sync --frozen --no-dev`.
2. `runtime` from `python:3.14-slim`; apt installs `git`, `ca-certificates`,
   `curl`, `gh` (from the `cli.github.com` apt repository); a non-root user
   `issuebot` (uid 1000) with `HOME=/home/issuebot`; Claude Code installed as that
   user with the native installer pinned by build arg
   (`curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_CODE_VERSION"`,
   default `2.1.258`), which puts `claude` in `/home/issuebot/.local/bin`; the venv
   copied from `builder` to `/app/.venv`; `PATH` includes both;
   `WORKDIR /app`; `ENTRYPOINT ["issuebot"]`; `CMD ["validate"]`.
3. Declared volumes `/workspaces` and `/home/issuebot/.claude`, owned by `issuebot`.

OCI labels for source and version. `.dockerignore` excludes `.git`, `.venv`,
`.claude`, `.superpowers`, `docs`, `tests`, caches.

### 8.2 `compose.yaml`

```yaml
services:
  db:
    image: postgres:18
    environment: {POSTGRES_USER: issuebot, POSTGRES_PASSWORD: issuebot, POSTGRES_DB: issuebot}
    volumes: [pgdata:/var/lib/postgresql]        # PG18 moved PGDATA under /var/lib/postgresql/18
    ports: ["127.0.0.1:5432:5432"]
    healthcheck: pg_isready -U issuebot -d issuebot
  worker:
    build: .
    command: ["validate"]                          # Phase 4 replaces with ["worker"]
    env_file: [{path: .env, required: false}]
    environment: {DATABASE_URL: "postgresql://issuebot:issuebot@db:5432/issuebot"}
    volumes:
      - ./WORKFLOW.md:/app/WORKFLOW.md:ro
      - workspaces:/workspaces
      - claude-home:/home/issuebot/.claude
    depends_on: {db: {condition: service_healthy}}
volumes: {pgdata: {}, workspaces: {}, claude-home: {}}
```

`docker compose up` in this phase starts the database and runs `validate` once in
the worker container, which is the Phase 1 smoke test. There is no `web` service
until Phase 7. `.env.example` documents `GH_TOKEN`, `ANTHROPIC_API_KEY` and
`SLACK_WEBHOOK_URL`; `.env` is already git-ignored.

## 9. CI and Dependabot

`.github/workflows/ci.yml`, on push to `main` and on pull requests, with a
concurrency group that cancels superseded runs:

| Job | Steps |
|---|---|
| `lint` | `actions/checkout@v7`, `astral-sh/setup-uv@v10`, `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pre-commit run --all-files --show-diff-on-failure` |
| `test` | same setup; a `postgres:18` service with a health check and `DATABASE_URL` exported (unused until Phase 6, present now so that phase does not touch CI); `uv run pytest` |
| `docker` | `docker/setup-buildx-action@v4`, `docker/build-push-action@v7` with `push: false`, `load: true`, GitHub Actions cache; then `docker run --rm <image> --version` |

`.github/dependabot.yml`: weekly updates for `uv`, `docker` and `github-actions`,
each grouping minor and patch bumps into one PR.

## 10. Repository documents

- `CLAUDE.md`: the "Repository state" section is replaced by a "Commands" section
  (`uv sync`, `uv run pytest`, `uv run ruff check .`, `uv run ruff format .`,
  `uv run pre-commit run --all-files`, `uv run issuebot validate`,
  `docker compose build`, `docker compose up`), and the package layout from
  section 2 is summarised.
- `README.md`: a "Development" section with the same quick start and a pointer to
  `docs/superpowers/specs/`.
- `WORKFLOW.md` at the repository root: front matter for dogfooding
  (`github.repo: jleavers/issuebot`, the defaults spelled out for the fields an
  operator is most likely to tune) and a three-line placeholder prompt body that
  Phase 3 replaces.
- `.pre-commit-config.yaml`: revs bumped as in section 3; hook set unchanged.

## 11. Testing

All tests are hermetic: no network, no real environment variables (fixtures pass
`environ`), no Docker.

| File | Covers |
|---|---|
| `test_workflow.py` | front matter split; CRLF input; no front matter; empty front matter; unterminated front matter; non-map YAML; invalid YAML; missing file; body stripping; `source_mtime_ns` populated |
| `test_settings.py` | every default in 4.2; each constraint's failure message; unknown key rejected at top level and nested; distinct-labels rule; `permission_mode` choices; `notifications.slack.events` validated against `EVENT_KINDS` |
| `test_resolve.py` | `$VAR` set, unset, empty; fallback variable set and unset; literal passthrough; `~`; relative `workspace.root` against the workflow directory; hook scripts containing `$` untouched; `missing_environment_variable` carries variable and field |
| `test_events.py` | `to_dict` shape and JSON serialisability for every type; `EVENT_KINDS` complete; bus fan-out order; a raising sink is isolated, counted and logged; `remove_sink`; `LogSink` output contains kind and fields |
| `test_log.py` | JSON lines parse and contain the fixed keys; bound issue and session context appear; stdlib logger output uses the same renderer; console mode does not raise |
| `test_cli.py` | `--version`; `validate` exit codes 0, 1 and 2 with a good, a failing and an unloadable workflow; each check line; `--show-config` masks secrets; missing command exits 2; `ISSUEBOT_WORKFLOW` precedence |

`test_cli.py` calls `main(argv)` in-process with `capsys`, and includes one
`subprocess` test that the installed `issuebot` script runs `--version`. The
executable checks are exercised by substituting the module-level lookup
`issuebot.cli._which` (a reference to `shutil.which`), so the suite does not depend
on `claude` or `gh` being installed on the test host.

## 12. Decisions made in this phase

1. Unknown configuration keys are rejected (section 4.2).
2. An explicit `$VAR` to an unset or empty variable fails loading; an omitted secret
   whose fallback variable is unset is simply `None` (section 4.3).
3. `permission_mode` allows only unattended-safe values.
4. Logs go to stderr; stdout is CLI output.
5. The event bus is synchronous; IO sinks own their own queues.
6. Compose ships `db` and `worker` (running `validate`) and no `web` stub.
7. Claude Code is installed with the native installer, pinned by build arg.
8. `validate` checks executables for presence only.
9. The CI test job carries a PostgreSQL 18 service from the start.
10. `issuebot.log` rather than `issuebot.logging`.

## 13. Done when

- `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run pre-commit run --all-files` and `uv run pytest` pass locally and in CI.
- `docker compose build` succeeds; `docker compose up` brings `db` healthy and the
  `worker` container prints the `validate` report (failing only on the missing
  `GH_TOKEN` when `.env` is absent) and exits.
- `uv run issuebot validate --workflow tests/fixtures/workflows/good.md` exits 0
  with `GH_TOKEN` set and `claude` and `gh` on `PATH`; with an invalid file it
  exits 2 and names the field.
- `CLAUDE.md` documents the commands above.
