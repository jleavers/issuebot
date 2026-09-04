# Phase 6: Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** History survives restarts and the numbers the dashboard needs can be queried: a PostgreSQL store the worker writes to without ever waiting on it, `issuebot migrate`, `status`, `stats` and `refresh`, a real `database.url` check in `validate`, and `LISTEN issuebot_refresh` wired to `request_refresh()`.

**Architecture:** A new `issuebot.db` package: numbered `.sql` migrations applied in one transaction under an advisory lock; `PostgresStore` (the SQL writes: every event appended, `runs` upserted on `run_started`/`run_ended`, `issues` upserted from polls and updated by `state_changed`/`issue_completed`/`issue_cancelled`, every `issues` write guarded by its observation time; one `runtime_snapshot` row rewritten); `PostgresSink` (`handle`, `record_issues` and `record_snapshot` only enqueue; one drain task writes, reconnects with backoff, retries the item in flight; `close()` drains for at most 10 s); `RefreshListener` (its own connection, a callback per NOTIFY); `Queries` (the reads, returning frozen row types that are Phase 7's view models); and a `Database` facade that is the CLI's single seam. Polled issues and the runtime snapshot reach the sink through injected orchestrator callbacks (`on_issues`, the existing `on_snapshot`), not through the bus. Three bounded Phase 4 amendments (the observer, the `review` grace measured on the monotonic clock, a final snapshot at shutdown) and one Phase 1 amendment (`RunEnded.log_dir`).

**Tech Stack:** Python 3.14, asyncio, `psycopg[binary]>=3.3` (async connections, `Jsonb`, `dict_row`; the phase's one new dependency), PostgreSQL 18 (`docker compose` locally, the CI service container), the Phase 1 `EventBus`/`EventSink`, pydantic settings (`DatabaseSettings` unchanged), structlog, pytest + pytest-asyncio (`asyncio_mode = "auto"`), ruff 0.16.5.

**Spec:** `docs/superpowers/specs/2026-09-04-phase-6-persistence-design.md` (parent: `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`; Phase 4: `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md`; Phase 5, the sink pattern: `docs/superpowers/specs/2026-09-03-phase-5-slack-notifications-design.md`).

**Pre-verified:** every code task below was built and run in a throwaway worktree before this plan was written; the file contents and the edit blocks are transcribed verbatim from it and the test counts are the ones it produced (baseline on `main`: 580 passed). Treat a different count as a finding, not as noise.

## Global Constraints

Every task's requirements include this section. Every implementer and reviewer dispatch must carry it verbatim.

- Python >=3.14, `uv run` for everything. One new dependency, added in Task 1 with `uv add 'psycopg[binary]>=3.3'` (resolves to psycopg 3.3.5 with a CPython 3.14 wheel that bundles libpq; the Dockerfile does not change); `pyproject.toml` and `uv.lock` change in that task only. No `psycopg_pool`, no ORM, no Alembic.
- Work on branch `phase-6-postgres`; the spec and this plan are its first two commits. Never push to `main`, never merge or close PRs, never `rm -rf`, `git reset --hard` or `git clean -fd`; the SDD workspaces under `.superpowers/sdd/` are left for the operator to delete. Linux host: Bash, `&&` chaining.
- A Bash-level hook on this host blocks any shell command whose text contains the dot-env filename (the literal `.` + `env`, including `.example` and heredoc bodies); such files are written with Write/Edit, staged with `git add --all` after `git status --short`; say "dot-env" in commit messages and reports.
- The ruff-format pre-commit hook (v0.16.5) reflows Python fences inside docs/**/*.md; write fences pre-formatted (double quotes, line length 100, trailing commas) and re-`git add` after `pre-commit run --all-files`.
- ruff rules E F I UP B N SIM RUF, target py314. SIM300 ranks literal > ALL_CAPS name > other expression and flags a comparison whose left side ranks higher; apply ruff's fix, never suppress, never a per-file ignore. SIM105 wants `contextlib.suppress` over `try/except/pass`. N818: exception classes end in `Error` (`StoreUnavailableError`, `StoreError`, `MigrationError`, `DatabaseError`). RUF022 sorts `__all__`; RUF006 stores `create_task` results; RUF005 wants `[*a, *b]` over list concatenation; RUF043 wants `re.escape` or a raw string for a `match=` pattern with metacharacters. UP037: no quoted annotations. The formatter writes `except A, B:` without parentheses only when there is no `as` clause (PEP 758); `except (OSError, ValueError) as exc:` keeps its parentheses. Syntax-check Python fences with `uv run python`, never the system `python3`.
- Commit messages: conventional prefix plus the attribution trailer the harness requires as the last lines (blank line before them). Before every commit: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`; before every push also `uv run pre-commit run --all-files`.
- Tests hermetic by default: `FakeGitHub`, `tests/fakes/claude`, `tests/fakes/gh`, `tmp_path`; the sink tests use a `FakeStore` and a recording `sleep`, the listener tests a fake connection, the CLI tests a `FakeDatabase`. Tests marked by the `db_url` fixture need a real PostgreSQL: they read `DATABASE_URL` (captured at conftest import, before the `clean_env` fixture clears it), create one schema per test and drop it afterwards, and are **skipped, and reported as skipped, when the variable is unset**. Run the suite both ways before every commit: `uv run pytest -q` (expect the stated `passed, skipped`) and `DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot uv run pytest -q` (expect the stated `passed`, no skips), with the compose database up: `ISSUEBOT_DB_PORT=5440 docker compose up -d db` from the repository root (port 5432 is taken on this host; 5440 is free). Tests that spawn the fakes or send signals are `skipif(sys.platform == "win32")`. Run long test commands under `timeout`.
- Frozen inputs, used as they are: `issuebot.agent` except the one-line `log_dir` population in `run_session` (Task 2), `issuebot.github`, `EventBus`/`EventSink`, `EVENT_KINDS` (no kind is added), `DatabaseSettings` (no field is added; queue, timeout and backoff knobs are constants), `issuebot.notifications`. `issuebot.orchestrator` changes only as Task 2 says.
- Package rules: `issuebot.db` imports `config`, `events`, `github` and `log` only (never `orchestrator`: the sink takes the snapshot through the structural `SnapshotLike`); `orchestrator` and `agent` never import `db`; `cli` wires it. The sink does no I/O in `handle`, `record_issues` or `record_snapshot`.
- Secrets: the database URL carries the password and is never logged or printed; log lines carry `describe(url)` (no password); every error string that leaves `issuebot.db` passes through `redact`; `validate` lines describe the connection, never the URL; tests assert `"s3cret" not in` the output or log stream.
- A live worker must not run under the Bash tool's `run_in_background` (the harness kills that shell after a few minutes); start it detached with `setsid nohup ... >> log 2>&1 < /dev/null &` and record the python pid with `pgrep`. A `Monitor` on the log does not wake an idle session; wait with `run_in_background` and a bounded `timeout N bash -c 'until grep -q ...; do sleep 10; done'`.
- The live-check task runs against jleavers/issuebot-scratch (issues #1 and #5 closed `complete`; #3 in `review` with PR #4 rebased and mergeable; host dirs `~/issuebot-scratch` and `~/issuebot-workspaces`) with `GH_TOKEN`, `SLACK_WEBHOOK_URL` and `DATABASE_URL` exported in the same command (from `gh auth token`, the operator's file `~/issuebot-scratch/slack-webhook` (mode 600), and the compose database on port 5440), spends real Claude budget under the operator's subscription login (about $0.60 to $0.80 per run; no `ANTHROPIC_API_KEY`), and never prints any of the three values. The operator, not the executor, merges the scratch PR the check needs merged.
- The fake `claude` reads `CLAUDE_FAKE_*` only; in orchestrator tests a worker exit needs several loop turns to become visible (the harness's `drain()` loops `sleep(0)` ten times); the sink and listener tests use a `settle()` that loops `sleep(0)` twenty times.

---

## File map

| Path | Responsibility | Task |
|---|---|---|
| `pyproject.toml`, `uv.lock` | `psycopg[binary]>=3.3` | 1 |
| `src/issuebot/db/__init__.py` | re-exports (grows in Task 5) | 1, 5 |
| `src/issuebot/db/errors.py` | `DatabaseError`, `StoreUnavailableError`, `StoreError`, `MigrationError` | 1 |
| `src/issuebot/db/connection.py` | `connect`, `describe`, `redact`, `error_text`, `classify`, `is_postgres_url`, `reconnect_delay`, constants | 1 |
| `src/issuebot/db/migrations/0001_initial.sql` | the four tables and their indexes | 1 |
| `src/issuebot/db/migrate.py` | `Migration`, `MigrationResult`, `discover_migrations`, `schema_version`, `apply_migrations`, `migrate` | 1 |
| `tests/conftest.py` | `_DATABASE_URL`, `with_search_path`, the `db_url` fixture | 1 |
| `tests/test_db_connection.py`, `tests/test_db_migrate.py` | connection helpers; discovery (hermetic) and application (DB) | 1 |
| `src/issuebot/events/types.py`, `src/issuebot/agent/session.py` | `RunEnded.log_dir` | 2 |
| `src/issuebot/orchestrator/state.py`, `orchestrator.py`, `__init__.py` | `on_issues`, `OBSERVED_STATES`, `review_seen_mono`, the shutdown snapshot | 2 |
| `tests/test_events.py`, `tests/test_agent_session.py`, `tests/test_orchestrator.py` | the amendments | 2 |
| `src/issuebot/db/store.py` | `IssueSnapshot`, `Store` protocol, the SQL, `PostgresStore` | 3 |
| `tests/test_db_store.py` | every write rule against the database | 3 |
| `src/issuebot/db/sink.py` | `SnapshotLike`, `PostgresSink` | 4 |
| `tests/test_db_sink.py` | the sink with a `FakeStore` | 4 |
| `src/issuebot/db/listen.py`, `queries.py`, `database.py` | `RefreshListener`; view models and `Queries`; the `Database` facade and `Probe` | 5 |
| `tests/test_db_listen.py`, `tests/test_db_queries.py`, `tests/test_db_database.py` | fake connection and a real NOTIFY; seeded queries; the facade | 5 |
| `src/issuebot/cli.py` | `_database_factory` seam, `_database_check`, `_Sinks`, `_open_database`, `_build_sinks`, `migrate`/`status`/`stats`/`refresh`, `render_status`, `render_stats`, worker and run-once wiring | 6 |
| `tests/test_cli.py` | `FakeDatabase` and friends, the six validate lines, the four commands, the wiring | 6 |
| `CLAUDE.md`, `README.md`, `compose.yaml`, the dot-env example, roadmap, Phase 1 and Phase 4 specs | documentation | 7 |
| (scratch repository, compose database, Slack test channel) | live check | 8 |

Test counts along the way (`uv run pytest -q` without a database / with `DATABASE_URL`):

| After task | Without a database | With a database |
|---|---|---|
| (main) | 580 passed | 580 passed |
| 1 | 596 passed, 5 skipped | 601 passed |
| 2 | 602 passed, 5 skipped | 607 passed |
| 3 | 603 passed, 20 skipped | 623 passed |
| 4 | 619 passed, 20 skipped | 639 passed |
| 5 | 629 passed, 30 skipped | 659 passed |
| 6, 7 | 654 passed, 30 skipped | 684 passed |

---

### Task 1: The dependency, connection helpers, errors, migrations and the `db_url` fixture

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (via `uv add`)
- Create: `src/issuebot/db/__init__.py`, `src/issuebot/db/errors.py`, `src/issuebot/db/connection.py`, `src/issuebot/db/migrations/0001_initial.sql`, `src/issuebot/db/migrate.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_db_connection.py`, `tests/test_db_migrate.py`

**Interfaces:**
- Consumes: nothing of this phase.
- Produces: `issuebot.db.errors` (`DatabaseError(message)` with `.message`; `StoreUnavailableError`, `StoreError`, `MigrationError`); `issuebot.db.connection` (`Connector = Callable[[str], Awaitable[AsyncConnection]]`, `async connect(url)`, `describe(url) -> str`, `redact(text, url) -> str`, `error_text(exc) -> str`, `classify(exc, url) -> DatabaseError`, `is_postgres_url(url) -> bool`, `reconnect_delay(attempt) -> float`, `CONNECT_TIMEOUT_S = 5`, `RECONNECT_DELAYS_S`, `REDACTED = "<database url>"`); `issuebot.db.migrate` (`Migration(version, name, sql)` with `.label`, `MigrationResult(applied, version)`, `discover_migrations(root=MIGRATIONS_ROOT)`, `async schema_version(conn) -> int`, `async apply_migrations(conn, migrations=None)`, `async migrate(url, *, connect=connect)`); the `db_url` fixture and `with_search_path(url, schema)` in `tests/conftest.py`.

Spec: §3, §7, §12 (the fixture).

- [ ] **Step 1: Add the dependency and bring the database up**

```bash
uv add 'psycopg[binary]>=3.3' && uv run python -c "import psycopg; print(psycopg.__version__, psycopg.pq.__impl__)" && ISSUEBOT_DB_PORT=5440 docker compose up -d db && sleep 6 && docker compose ps db
```

Expected: `pyproject.toml` gains `"psycopg[binary]>=3.3",` in `dependencies` (keep the list alphabetical: after `jinja2`, before `pydantic`; `uv add` puts it there); `uv.lock` gains `psycopg` and `psycopg-binary` 3.3.5; the probe prints `3.3.5 binary`; `docker compose ps db` shows `issuebot-db-1 ... Up ... (healthy)` on `127.0.0.1:5440->5432/tcp`. `uv run pytest -q` still says 580 passed.

- [ ] **Step 2: The `db_url` fixture**


In `tests/conftest.py` replace

```
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from issuebot.github.models import Issue, StateLabel
```

with

```
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from issuebot.github.models import Issue, StateLabel

# Read before the clean_env fixture removes it from the environment for every test.
_DATABASE_URL = os.environ.get("DATABASE_URL")
```

Append to `tests/conftest.py`:

```
def with_search_path(url: str, schema: str) -> str:
    """The same URL with ``options=-c search_path=<schema>`` appended to its query string."""
    parts = urlsplit(url)
    extra = "options=" + quote(f"-c search_path={schema}", safe="")
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit(parts._replace(query=query))


@pytest.fixture
def db_url() -> Iterator[str]:
    """A DATABASE_URL scoped to a fresh schema; skipped when no database is configured."""
    if not _DATABASE_URL:
        pytest.skip("DATABASE_URL is not set; database tests need a PostgreSQL server")
    schema = f"issuebot_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(_DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield with_search_path(_DATABASE_URL, schema)
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
```


- [ ] **Step 3: Write the failing tests**


Create `tests/test_db_connection.py` with the Write tool:

```python
"""Tests for the database connection helpers (no database needed)."""

import psycopg
import pytest

from issuebot.db import (
    RECONNECT_DELAYS_S,
    REDACTED,
    StoreError,
    StoreUnavailableError,
    classify,
    describe,
    error_text,
    is_postgres_url,
    reconnect_delay,
    redact,
)

URL = "postgresql://issuebot:s3cret@db.example:5433/issuebot?sslmode=require"


def test_is_postgres_url_accepts_both_schemes_and_rejects_others() -> None:
    assert is_postgres_url(URL)
    assert is_postgres_url("postgres://u@h/db")
    assert not is_postgres_url("mysql://u@h/db")
    assert not is_postgres_url("not a url")
    assert not is_postgres_url("http://[bad")


def test_describe_drops_the_password_and_keeps_the_rest() -> None:
    assert describe(URL) == "postgresql://issuebot@db.example:5433/issuebot"
    assert describe("postgresql://db/issuebot") == "postgresql://db/issuebot"
    assert describe("postgresql://[bad") == REDACTED
    assert describe("postgresql://u@h:notaport/db") == REDACTED


def test_redact_removes_the_url_and_the_bare_password() -> None:
    text = f"connection to {URL} failed: password s3cret rejected"
    assert redact(text, URL) == f"connection to {REDACTED} failed: password {REDACTED} rejected"
    assert redact("nothing here", URL) == "nothing here"


def test_redact_copes_without_a_password_or_with_a_malformed_url() -> None:
    assert redact("x postgresql://u@h/db y", "postgresql://u@h/db") == f"x {REDACTED} y"
    assert redact("left alone", "postgresql://[bad") == "left alone"


def test_reconnect_delay_walks_the_table_and_then_repeats_the_last_value() -> None:
    assert [reconnect_delay(n) for n in range(1, 9)] == [1, 2, 4, 8, 16, 30, 30, 30]
    assert reconnect_delay(0) == RECONNECT_DELAYS_S[0]


def test_error_text_keeps_the_first_line_only() -> None:
    assert error_text(psycopg.OperationalError("refused\n\tIs the server running?")) == "refused"
    assert error_text(psycopg.OperationalError("")) == "OperationalError"


def test_classify_separates_connection_failures_from_statement_failures() -> None:
    lost = classify(psycopg.OperationalError(f"server closed {URL}"), URL)
    assert isinstance(lost, StoreUnavailableError)
    assert lost.message == f"OperationalError: server closed {REDACTED}"
    broken = classify(psycopg.InterfaceError("the connection is closed"), URL)
    assert isinstance(broken, StoreUnavailableError)
    bad = classify(psycopg.DataError("invalid input for s3cret"), URL)
    assert isinstance(bad, StoreError)
    assert bad.message == f"DataError: invalid input for {REDACTED}"


@pytest.mark.parametrize("exc", [psycopg.OperationalError("x"), psycopg.DataError("y")])
def test_classify_never_leaks_the_url(exc: psycopg.Error) -> None:
    assert "s3cret" not in classify(exc, URL).message
```


Create `tests/test_db_migrate.py` with the Write tool:

```python
"""Tests for migration discovery (hermetic) and application (needs DATABASE_URL)."""

from pathlib import Path

import psycopg
import pytest

from issuebot.db import (
    Migration,
    MigrationError,
    apply_migrations,
    connect,
    discover_migrations,
    migrate,
    schema_version,
)

TABLES = {"issues", "runs", "events", "runtime_snapshot", "schema_migrations"}


# --- discovery (no database) -------------------------------------------------------------


def test_the_package_ships_the_initial_migration() -> None:
    migrations = discover_migrations()
    assert [m.label for m in migrations] == ["0001_initial"]
    assert migrations[0].version == 1
    assert "CREATE TABLE issues" in migrations[0].sql
    assert "CREATE TABLE runtime_snapshot" in migrations[0].sql


def test_discovery_sorts_by_version_and_reads_the_sql(tmp_path: Path) -> None:
    (tmp_path / "0002_second.sql").write_text("SELECT 2", encoding="utf-8")
    (tmp_path / "0001_first.sql").write_text("SELECT 1", encoding="utf-8")
    found = discover_migrations(tmp_path)
    assert [(m.version, m.name, m.sql) for m in found] == [
        (1, "first", "SELECT 1"),
        (2, "second", "SELECT 2"),
    ]


def test_discovery_rejects_a_gap(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "0003_third.sql").write_text("SELECT 3", encoding="utf-8")
    with pytest.raises(MigrationError, match="without gaps; found 0003_third where 0002"):
        discover_migrations(tmp_path)


def test_discovery_rejects_a_duplicate_version(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "0001_again.sql").write_text("SELECT 1", encoding="utf-8")
    with pytest.raises(MigrationError, match="duplicate migration version 0001"):
        discover_migrations(tmp_path)


def test_discovery_rejects_a_stray_file(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(MigrationError, match=r"unexpected file in migrations: notes\.txt"):
        discover_migrations(tmp_path)


def test_an_empty_directory_has_no_migrations(tmp_path: Path) -> None:
    assert discover_migrations(tmp_path) == ()


# --- application (database) --------------------------------------------------------------


async def _tables(conn: psycopg.AsyncConnection) -> set[str]:
    cursor = await conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    )
    return {row[0] for row in await cursor.fetchall()}


async def test_migrate_applies_the_initial_migration_once(db_url: str) -> None:
    first = await migrate(db_url)
    assert (first.applied, first.version) == (("0001_initial",), 1)
    second = await migrate(db_url)
    assert (second.applied, second.version) == ((), 1)
    conn = await connect(db_url)
    try:
        assert await _tables(conn) == TABLES
        assert await schema_version(conn) == 1
    finally:
        await conn.close()


async def test_schema_version_is_zero_before_any_migration(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        assert await schema_version(conn) == 0
        assert await _tables(conn) == set()
    finally:
        await conn.close()


async def test_a_newer_recorded_version_is_refused(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await apply_migrations(conn)
        await conn.execute("INSERT INTO schema_migrations (version, name) VALUES (7, 'future')")
        with pytest.raises(MigrationError, match=r"schema version 7 is newer .* knows \(1\)"):
            await apply_migrations(conn)
    finally:
        await conn.close()


async def test_a_renamed_recorded_migration_is_refused(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await apply_migrations(conn)
        await conn.execute("UPDATE schema_migrations SET name = 'other' WHERE version = 1")
        with pytest.raises(MigrationError, match="recorded as 'other', not 'initial'"):
            await apply_migrations(conn)
    finally:
        await conn.close()


async def test_a_failing_migration_rolls_back_the_whole_run(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        good = Migration(version=1, name="table", sql="CREATE TABLE t (id int)")
        bad = Migration(version=2, name="broken", sql="CREATE TABLE t (id int)")
        with pytest.raises(psycopg.Error):
            await apply_migrations(conn, [good, bad])
        assert await _tables(conn) == set()
        assert await schema_version(conn) == 0
    finally:
        await conn.close()


async def test_migrate_reports_an_unreachable_server_without_the_url() -> None:
    url = "postgresql://issuebot:s3cret@127.0.0.1:1/issuebot"
    with pytest.raises(MigrationError, match="cannot connect") as exc:
        await migrate(url)
    assert "s3cret" not in exc.value.message
```


- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_db_connection.py tests/test_db_migrate.py`
Expected: `2 errors` during collection, both `ModuleNotFoundError: No module named 'issuebot.db'`.

- [ ] **Step 5: The package**


Create `src/issuebot/db/errors.py` with the Write tool:

```python
"""Errors raised by the database package. Messages never contain the database URL."""


class DatabaseError(Exception):
    """Base class; ``message`` has passed through ``redact``."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class StoreUnavailableError(DatabaseError):
    """A connection-level failure: reconnect and retry the write."""


class StoreError(DatabaseError):
    """A statement failed for a reason a retry would not fix: drop the item."""


class MigrationError(DatabaseError):
    """Migrations could not be discovered or applied."""
```


Create `src/issuebot/db/connection.py` with the Write tool:

```python
"""Connection helpers: connect, describe and redact a database URL, reconnect backoff."""

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import psycopg
from psycopg import AsyncConnection

from issuebot.db.errors import DatabaseError, StoreError, StoreUnavailableError

CONNECT_TIMEOUT_S = 5
RECONNECT_DELAYS_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
REDACTED = "<database url>"
POSTGRES_SCHEMES = ("postgresql", "postgres")
APPLICATION_NAME = "issuebot"

Connector = Callable[[str], Awaitable[AsyncConnection]]


def is_postgres_url(url: str) -> bool:
    try:
        return urlsplit(url).scheme in POSTGRES_SCHEMES
    except ValueError:
        return False


def describe(url: str) -> str:
    """``postgresql://user@host:port/db`` without the password, for log lines."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return REDACTED
    user = f"{parts.username}@" if parts.username else ""
    host = parts.hostname or ""
    suffix = f":{port}" if port else ""
    return f"{parts.scheme}://{user}{host}{suffix}{parts.path}"


def redact(text: str, url: str) -> str:
    """Replace the full URL and, on its own, its password with a placeholder."""
    redacted = text.replace(url, REDACTED)
    try:
        password = urlsplit(url).password
    except ValueError:
        return redacted
    if password:
        redacted = redacted.replace(password, REDACTED)
    return redacted


def reconnect_delay(attempt: int) -> float:
    """The delay before reconnect attempt ``attempt`` (1-based); the last value from then on."""
    index = min(max(attempt, 1), len(RECONNECT_DELAYS_S)) - 1
    return RECONNECT_DELAYS_S[index]


def error_text(exc: BaseException) -> str:
    """The first line of an exception's message (libpq appends hints on further lines)."""
    lines = str(exc).strip().splitlines()
    return lines[0] if lines else type(exc).__name__


def classify(exc: psycopg.Error, url: str) -> DatabaseError:
    """StoreUnavailableError for connection-level failures, StoreError otherwise; redacted."""
    message = redact(f"{type(exc).__name__}: {error_text(exc)}", url)
    if isinstance(exc, psycopg.OperationalError | psycopg.InterfaceError):
        return StoreUnavailableError(message)
    return StoreError(message)


async def connect(url: str) -> AsyncConnection:
    """One autocommit connection with a bounded connect timeout and a UTC session time zone."""
    conn = await AsyncConnection.connect(
        url,
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT_S,
        application_name=APPLICATION_NAME,
    )
    await conn.execute("SET TIME ZONE 'UTC'")
    return conn
```


Create `src/issuebot/db/migrations/0001_initial.sql` with the Write tool:

```sql
-- Phase 6: the observability store (roadmap §2.7). Timestamps are timestamptz; every
-- issuebot connection runs with a UTC session time zone.

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
    seen_at       timestamptz NOT NULL  -- when this snapshot was observed
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
    written_at timestamptz NOT NULL,    -- now() at the write; the dashboard's snapshot age
    data       jsonb NOT NULL           -- RuntimeSnapshot.to_dict()
);
```


Create `src/issuebot/db/migrate.py` with the Write tool:

```python
"""Numbered SQL migrations applied in one transaction under an advisory lock."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

import psycopg
from psycopg import AsyncConnection

from issuebot.db.connection import Connector, connect, error_text, redact
from issuebot.db.errors import MigrationError

MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
ADVISORY_LOCK_KEY = 0x49535355  # "ISSU": the same key in every process that migrates
MIGRATIONS_ROOT = files("issuebot.db") / "migrations"
SCHEMA_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    integer PRIMARY KEY,
    name       text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)"""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied: tuple[str, ...]
    version: int


def discover_migrations(root: Traversable | Path = MIGRATIONS_ROOT) -> tuple[Migration, ...]:
    """Every ``NNNN_name.sql`` under ``root``, sorted; versions must run 1, 2, 3 ... with no gap."""
    found: dict[int, Migration] = {}
    for entry in root.iterdir():
        match = MIGRATION_NAME.match(entry.name)
        if match is None:
            raise MigrationError(f"unexpected file in migrations: {entry.name}")
        version = int(match.group(1))
        if version in found:
            raise MigrationError(f"duplicate migration version {version:04d}")
        found[version] = Migration(
            version=version, name=match.group(2), sql=entry.read_text(encoding="utf-8")
        )
    ordered = tuple(found[version] for version in sorted(found))
    for expected, migration in enumerate(ordered, start=1):
        if migration.version != expected:
            raise MigrationError(
                "migration versions must run 1, 2, 3 ... without gaps; "
                f"found {migration.label} where {expected:04d} was expected"
            )
    return ordered


async def schema_version(conn: AsyncConnection) -> int:
    """``max(version)`` in ``schema_migrations``; 0 when the table does not exist."""
    exists = await (await conn.execute("SELECT to_regclass('schema_migrations')")).fetchone()
    if exists is None or exists[0] is None:
        return 0
    row = await (
        await conn.execute("SELECT coalesce(max(version), 0) FROM schema_migrations")
    ).fetchone()
    return int(row[0]) if row is not None else 0


async def apply_migrations(
    conn: AsyncConnection, migrations: Sequence[Migration] | None = None
) -> MigrationResult:
    """Apply every pending migration in one transaction under an advisory lock."""
    known = tuple(migrations) if migrations is not None else discover_migrations()
    latest = known[-1].version if known else 0
    applied: list[str] = []
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
        await conn.execute(SCHEMA_TABLE_SQL)
        cursor = await conn.execute("SELECT version, name FROM schema_migrations ORDER BY version")
        recorded = {int(version): str(name) for version, name in await cursor.fetchall()}
        newest = max(recorded, default=0)
        if newest > latest:
            raise MigrationError(
                f"schema version {newest} is newer than this issuebot knows ({latest})"
            )
        for migration in known:
            name = recorded.get(migration.version)
            if name is not None:
                if name != migration.name:
                    raise MigrationError(
                        f"migration {migration.version:04d} is recorded as {name!r}, "
                        f"not {migration.name!r}"
                    )
                continue
            await conn.execute(migration.sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (migration.version, migration.name),
            )
            applied.append(migration.label)
    return MigrationResult(applied=tuple(applied), version=latest)


async def migrate(url: str, *, connect: Connector = connect) -> MigrationResult:
    """Connect, apply the pending migrations, close; failures are MigrationError, redacted."""
    try:
        conn = await connect(url)
    except psycopg.Error as exc:
        raise MigrationError(redact(f"cannot connect: {error_text(exc)}", url)) from exc
    try:
        return await apply_migrations(conn)
    except psycopg.Error as exc:
        raise MigrationError(redact(f"{type(exc).__name__}: {error_text(exc)}", url)) from exc
    finally:
        await conn.close()
```


Create `src/issuebot/db/__init__.py` with the Write tool:

```python
"""Persistence: psycopg 3 connections, numbered SQL migrations, the sink and the queries."""

from issuebot.db.connection import (
    APPLICATION_NAME,
    CONNECT_TIMEOUT_S,
    POSTGRES_SCHEMES,
    RECONNECT_DELAYS_S,
    REDACTED,
    Connector,
    classify,
    connect,
    describe,
    error_text,
    is_postgres_url,
    reconnect_delay,
    redact,
)
from issuebot.db.errors import DatabaseError, MigrationError, StoreError, StoreUnavailableError
from issuebot.db.migrate import (
    ADVISORY_LOCK_KEY,
    MIGRATIONS_ROOT,
    Migration,
    MigrationResult,
    apply_migrations,
    discover_migrations,
    migrate,
    schema_version,
)

__all__ = [
    "ADVISORY_LOCK_KEY",
    "APPLICATION_NAME",
    "CONNECT_TIMEOUT_S",
    "MIGRATIONS_ROOT",
    "POSTGRES_SCHEMES",
    "RECONNECT_DELAYS_S",
    "REDACTED",
    "Connector",
    "DatabaseError",
    "Migration",
    "MigrationError",
    "MigrationResult",
    "StoreError",
    "StoreUnavailableError",
    "apply_migrations",
    "classify",
    "connect",
    "describe",
    "discover_migrations",
    "error_text",
    "is_postgres_url",
    "migrate",
    "reconnect_delay",
    "redact",
    "schema_version",
]
```


- [ ] **Step 6: Run the tests to verify they pass, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && timeout 300 uv run pytest -q`
Expected: lint clean; `596 passed, 5 skipped` (the five DB-backed migration tests skip: `DATABASE_URL is not set; database tests need a PostgreSQL server`, visible with `-rs`).

Run: `DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q`
Expected: `601 passed`. The migration tests prove the multi-statement `execute` of a `.sql` file, the `options=-c search_path=` scoping, the advisory lock and the rollback of a failing run all work against PostgreSQL 18.

Also: `uv build --wheel -q && unzip -l dist/*.whl | grep migrations` lists `issuebot/db/migrations/0001_initial.sql` (hatchling ships package data; the editable install the Dockerfile uses does too). Remove `dist/` afterwards (`rm -r dist` is fine; it is a build artefact you just created).

- [ ] **Step 7: Commit**

```bash
git add --all
git commit -m "feat: add psycopg, the db connection helpers, numbered migrations and the db_url fixture"
```

(`git status --short` before adding: `pyproject.toml`, `uv.lock`, `tests/conftest.py`, the two test files and the five package files; nothing else.)

---

### Task 2: Phase 1 and Phase 4 amendments: `RunEnded.log_dir`, `on_issues`, the grace in time, the shutdown snapshot

**Files:**
- Modify: `src/issuebot/events/types.py`, `src/issuebot/agent/session.py`, `src/issuebot/orchestrator/state.py`, `src/issuebot/orchestrator/orchestrator.py`, `src/issuebot/orchestrator/__init__.py`
- Test: `tests/test_events.py`, `tests/test_agent_session.py`, `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: nothing of this phase.
- Produces: `RunEnded.log_dir: str | None = None` (filled by `run_session` from `RunResult.log_dir`); `Orchestrator(..., on_issues: Callable[[Sequence[Issue]], None] | None = None)`, called with every non-empty successful fetch (the tick's candidates, reconcile's refresh, the terminal sweep, a fired retry's refresh), exceptions logged `issues_consumer_failed` and swallowed; `OBSERVED_STATES = (*CANDIDATE_STATES, StateLabel.REVIEW)` fetched by the tick when `on_issues` is set; `RunningEntry.review_seen_mono: float | None` replaces `review_seen_tick` and the worker is stopped once `clock() - review_seen_mono >= polling.interval_ms / 1000`; `REVIEW_GRACE_TICKS` is removed from `state.py` and the package exports; `shutdown()` ends with `_publish_snapshot()`.

Spec: §8.

- [ ] **Step 1: Write the failing tests**


In `tests/test_events.py` replace

```
def test_state_changed_pr_url_defaults_to_none() -> None:
```

with

```
def test_run_ended_log_dir_defaults_to_none_and_serialises() -> None:
    ended = ALL_EVENTS[2]
    assert isinstance(ended, RunEnded)
    assert ended.log_dir is None
    assert ended.to_dict()["log_dir"] is None
    with_dir = RunEnded(
        issue_number=1,
        issue_identifier="repo-1",
        run_id="run-1",
        outcome="failed",
        error="x",
        turns=0,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        duration_s=0.1,
        log_dir="/workspaces/repo-1/.issuebot/runs/run-1",
    )
    assert json.loads(json.dumps(with_dir.to_dict()))["log_dir"] == (
        "/workspaces/repo-1/.issuebot/runs/run-1"
    )


def test_state_changed_pr_url_defaults_to_none() -> None:
```

In `tests/test_agent_session.py` replace

```
    assert (ended.outcome, ended.error, ended.turns) == ("succeeded", None, 1)
    assert (ended.input_tokens, ended.output_tokens, ended.cost_usd) == (60, 5, 0.25)
    record = h.workspaces.read_session(h.workspace)
```

with

```
    assert (ended.outcome, ended.error, ended.turns) == ("succeeded", None, 1)
    assert (ended.input_tokens, ended.output_tokens, ended.cost_usd) == (60, 5, 0.25)
    assert ended.log_dir == str(h.workspace / ".issuebot" / "runs" / "run-1")
    record = h.workspaces.read_session(h.workspace)
```

In `tests/test_orchestrator.py` replace

```
import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
```

with

```
import asyncio
import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
```

In `tests/test_orchestrator.py` replace

```
from issuebot.github import WORKPAD_MARKER, FakeGitHub, GhResult, Issue, StateLabel
```

with

```
from issuebot.github import WORKPAD_MARKER, FakeGitHub, GhResult, Issue, StateLabel
from issuebot.log import configure_logging
```

In `tests/test_orchestrator.py` replace

```
        claude: str = "claude",
        real_sessions: bool = False,
    ) -> None:
        self.tmp_path = tmp_path
```

with

```
        claude: str = "claude",
        real_sessions: bool = False,
        observe_issues: bool = False,
    ) -> None:
        self.tmp_path = tmp_path
```

In `tests/test_orchestrator.py` replace

```
        self.snapshots: list[Any] = []
        self.which_missing: set[str] = set()
        self.orchestrator = Orchestrator(
```

with

```
        self.snapshots: list[Any] = []
        self.polled: list[list[Issue]] = []
        self.which_missing: set[str] = set()
        self.orchestrator = Orchestrator(
```

In `tests/test_orchestrator.py` replace

```
            on_snapshot=self.snapshots.append,
        )

    # --- construction helpers
```

with

```
            on_snapshot=self.snapshots.append,
            on_issues=self.record_polled if observe_issues else None,
        )

    def record_polled(self, issues: Any) -> None:
        self.polled.append(list(issues))

    # --- construction helpers
```

In `tests/test_orchestrator.py` replace

```
async def test_reconcile_gives_review_one_tick_of_grace(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.tick()
    entry = h.entry(1)
    assert entry.review_seen_tick == 1
    assert not entry.cancel.is_set()
    assert [event.actor for event in h.recorder.of(StateChanged)] == ["issuebot", "agent"]
    await h.tick()
    assert entry.cancel.is_set()
```

with

```
async def test_reconcile_gives_review_one_interval_of_grace(tmp_path: Path) -> None:
    h = Harness(tmp_path, interval_ms=30_000)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")
    h.github.human_set_state(1, StateLabel.REVIEW)
    h.clock.advance(30)
    await h.tick()
    entry = h.entry(1)
    assert entry.review_seen_mono == h.clock.value
    assert not entry.cancel.is_set()
    assert [event.actor for event in h.recorder.of(StateChanged)] == ["issuebot", "agent"]
    h.clock.advance(1)  # a refresh-driven tick inside the interval leaves the worker alone
    await h.tick()
    assert not entry.cancel.is_set()
    h.clock.advance(29)
    await h.tick()
    assert entry.cancel.is_set()
```

In `tests/test_orchestrator.py` replace

```
# --- the loop -----------------------------------------------------------------------------
```

with

```
# --- the issues observer (Phase 6) ----------------------------------------------------------


async def test_on_issues_receives_every_fetch(tmp_path: Path) -> None:
    h = Harness(tmp_path, observe_issues=True)
    h.add_issue(1, "todo")
    h.add_issue(2, "review")
    closed = h.add_issue(3, "review")
    h.github.open_pr(3, pr_number=7)
    h.github.merge_pr(7)
    h.github.close_issue(3)
    await h.tick()
    # the first tick: the sweep's closed issue, then the candidate fetch (review included)
    assert [[issue.number for issue in batch] for batch in h.polled] == [[3], [1, 2]]
    assert h.polled[0][0].github_state == "closed"
    assert closed.number == 3
    assert list(h.orchestrator.running) == ["1"]
    assert h.calls("fetch_issues_by_states")[-1] == (
        (StateLabel.IN_PROGRESS, StateLabel.REWORK, StateLabel.TODO, StateLabel.REVIEW),
    )
    await h.tick()
    # the second tick: reconcile's refresh of the running issue, then the candidate fetch
    assert [[issue.number for issue in batch] for batch in h.polled[2:]] == [[1], [1, 2]]
    assert h.polled[2][0].state is StateLabel.IN_PROGRESS


async def test_on_issues_receives_a_fired_retry_refresh(tmp_path: Path) -> None:
    h = Harness(tmp_path, observe_issues=True)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1))
    h.polled.clear()
    await h.fire(1.0)
    assert [[issue.number for issue in batch] for batch in h.polled] == [[1]]
    assert h.polled[0][0].state is StateLabel.REVIEW
    assert h.orchestrator.retries == {}


async def test_without_an_observer_review_is_not_fetched(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "review")
    await h.tick()
    assert h.polled == []
    assert h.calls("fetch_issues_by_states")[-1] == (
        (StateLabel.IN_PROGRESS, StateLabel.REWORK, StateLabel.TODO),
    )
    assert h.orchestrator.running == {}


async def test_a_raising_issues_consumer_is_logged_and_the_tick_continues(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    configure_logging(fmt="json", level="INFO", stream=stream)
    h = Harness(tmp_path, observe_issues=True)

    def explode(issues: Any) -> None:
        raise RuntimeError("consumer bug")

    h.orchestrator._on_issues = explode
    h.add_issue(1, "todo")
    await h.tick()
    assert list(h.orchestrator.running) == ["1"]
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    failed = [line for line in lines if line["event"] == "issues_consumer_failed"]
    assert len(failed) == 1  # the candidate fetch; the sweep found nothing to report
    assert failed[0]["count"] == 1
    assert "consumer bug" in failed[0]["exception"]


async def test_shutdown_publishes_a_final_snapshot(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    assert len(h.snapshots) == 1
    assert len(h.snapshots[-1].running) == 1
    await h.orchestrator.shutdown()
    assert len(h.snapshots) == 2
    assert h.snapshots[-1].running == ()
    assert h.snapshots[-1].retrying == ()
    assert h.snapshots[-1].counters.runs_ended == 1


# --- the loop -----------------------------------------------------------------------------
```


- [ ] **Step 2: Run the tests to verify they fail**

Run: `timeout 300 uv run pytest -q tests/test_events.py tests/test_agent_session.py tests/test_orchestrator.py`
Expected: `59 failed, 27 passed`: every test in `test_orchestrator.py` fails in the `Harness` with `TypeError: Orchestrator.__init__() got an unexpected keyword argument 'on_issues'`, and the two new assertions fail with `AttributeError: 'RunEnded' object has no attribute 'log_dir'`.

- [ ] **Step 3: The amendments**


In `src/issuebot/events/types.py` replace

```
    cost_usd: float
    duration_s: float


@dataclass(frozen=True, kw_only=True)
class PrOpened(IssueEvent):
```

with

```
    cost_usd: float
    duration_s: float
    log_dir: str | None = None


@dataclass(frozen=True, kw_only=True)
class PrOpened(IssueEvent):
```

In `src/issuebot/agent/session.py` replace

```
                cost_usd=result.cost_usd,
                duration_s=result.duration_s,
            )
        )
        log.info(
            "run_finished",
```

with

```
                cost_usd=result.cost_usd,
                duration_s=result.duration_s,
                log_dir=str(result.log_dir) if result.log_dir is not None else None,
            )
        )
        log.info(
            "run_finished",
```

In `src/issuebot/orchestrator/state.py` replace

```
TERMINAL_SWEEP_EVERY_TICKS = 10
REVIEW_GRACE_TICKS = 1
```

with

```
TERMINAL_SWEEP_EVERY_TICKS = 10
```

In `src/issuebot/orchestrator/state.py` replace

```
    review_seen_tick: int | None = None
```

with

```
    review_seen_mono: float | None = None  # first sight of `review`; the grace runs from here
```

In `src/issuebot/orchestrator/orchestrator.py` replace

```
from collections.abc import Awaitable, Callable, Mapping
```

with

```
from collections.abc import Awaitable, Callable, Mapping, Sequence
```

In `src/issuebot/orchestrator/orchestrator.py` replace

```
from issuebot.orchestrator.state import (
    CONTINUATION_DELAY_MS,
    REVIEW_GRACE_TICKS,
    TERMINAL_SWEEP_EVERY_TICKS,
```

with

```
from issuebot.orchestrator.state import (
    CONTINUATION_DELAY_MS,
    TERMINAL_SWEEP_EVERY_TICKS,
```

In `src/issuebot/orchestrator/orchestrator.py` replace

```
CANDIDATE_STATES: tuple[StateLabel, ...] = (
    StateLabel.IN_PROGRESS,
    StateLabel.REWORK,
    StateLabel.TODO,
)
SHUTDOWN_MARGIN_S = 10.0
```

with

```
CANDIDATE_STATES: tuple[StateLabel, ...] = (
    StateLabel.IN_PROGRESS,
    StateLabel.REWORK,
    StateLabel.TODO,
)
# Fetched instead of CANDIDATE_STATES when an on_issues observer is attached: review is polled
# for the history store only; the dispatch loop never runs it (Phase 6 spec §8.1).
OBSERVED_STATES: tuple[StateLabel, ...] = (*CANDIDATE_STATES, StateLabel.REVIEW)
SHUTDOWN_MARGIN_S = 10.0
```

In `src/issuebot/orchestrator/orchestrator.py` replace

```
        environ: Mapping[str, str] | None = None,
        on_snapshot: Callable[[RuntimeSnapshot], None] | None = None,
    ) -> None:
```

with

```
        environ: Mapping[str, str] | None = None,
        on_snapshot: Callable[[RuntimeSnapshot], None] | None = None,
        on_issues: Callable[[Sequence[Issue]], None] | None = None,
    ) -> None:
```

In `src/issuebot/orchestrator/orchestrator.py` replace

```
        self._on_snapshot = on_snapshot
        self._adapter = adapter_factory(workflow.config.github)
```

with

```
        self._on_snapshot = on_snapshot
        self._on_issues = on_issues
        self._adapter = adapter_factory(workflow.config.github)
```

In `src/issuebot/orchestrator/orchestrator.py` replace

```
    def _publish_snapshot(self) -> None:
        if self._on_snapshot is None:
            return
        try:
            self._on_snapshot(self.snapshot())
        except Exception:
            self._log.exception("snapshot_consumer_failed")
```

with

```
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
```

In `src/issuebot/orchestrator/orchestrator.py` replace

```
    async def _dispatch_candidates(self) -> int:
        try:
            issues = await self._adapter.fetch_issues_by_states(CANDIDATE_STATES)
        except GitHubError as exc:
            self._log.warning("candidates_fetch_failed", error=str(exc))
            return 0
        dispatched = 0
```

with

```
    async def _dispatch_candidates(self) -> int:
        states = OBSERVED_STATES if self._on_issues is not None else CANDIDATE_STATES
        try:
            issues = await self._adapter.fetch_issues_by_states(states)
        except GitHubError as exc:
            self._log.warning("candidates_fetch_failed", error=str(exc))
            return 0
        self._report_issues(issues)
        dispatched = 0
```

In `src/issuebot/orchestrator/orchestrator.py` replace

```
        except GitHubError as exc:
            self._log.warning("reconcile_refresh_failed", error=str(exc))
            return
        by_id = {issue.id: issue for issue in refreshed}
```

with

```
        except GitHubError as exc:
            self._log.warning("reconcile_refresh_failed", error=str(exc))
            return
        self._report_issues(refreshed)
        by_id = {issue.id: issue for issue in refreshed}
```

In `src/issuebot/orchestrator/orchestrator.py` replace

```
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
```

with

```
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
```

In `src/issuebot/orchestrator/orchestrator.py` replace

```
    def _stop_entry(self, entry: RunningEntry, cause: StopCause, detail: str) -> None:
```

with

```
    def _review_grace_s(self) -> float:
        """One poll interval: the grace before a worker whose issue reached review is stopped.

        Measured on the monotonic clock rather than in ticks, so a refresh-driven tick (a
        NOTIFY, Phase 6) cannot cut it short (Phase 6 spec §8.2).
        """
        return self._workflow.config.polling.interval_ms / 1000

    def _stop_entry(self, entry: RunningEntry, cause: StopCause, detail: str) -> None:
```

In `src/issuebot/orchestrator/orchestrator.py` replace

```
        except GitHubError as exc:
            self._log.warning("terminal_sweep_failed", error=str(exc))
            return
        for issue in issues:
            if issue.id in self._running:
```

with

```
        except GitHubError as exc:
            self._log.warning("terminal_sweep_failed", error=str(exc))
            return
        self._report_issues(issues)
        for issue in issues:
            if issue.id in self._running:
```

In `src/issuebot/orchestrator/orchestrator.py` replace

```
            return
        if not issues:
            self._release(entry, "missing")
            return
        issue = issues[0]
```

with

```
            return
        self._report_issues(issues)
        if not issues:
            self._release(entry, "missing")
            return
        issue = issues[0]
```

In `src/issuebot/orchestrator/orchestrator.py` replace

```
            blocked=counters.blocked,
            cost_usd=self._totals.cost_usd,
        )
```

with

```
            blocked=counters.blocked,
            cost_usd=self._totals.cost_usd,
        )
        self._publish_snapshot()
```

In `src/issuebot/orchestrator/__init__.py` replace

```
    CANDIDATE_STATES,
    Orchestrator,
```

with

```
    CANDIDATE_STATES,
    OBSERVED_STATES,
    Orchestrator,
```

In `src/issuebot/orchestrator/__init__.py` replace

```
    CONTINUATION_DELAY_MS,
    REVIEW_GRACE_TICKS,
    TERMINAL_SWEEP_EVERY_TICKS,
```

with

```
    CONTINUATION_DELAY_MS,
    TERMINAL_SWEEP_EVERY_TICKS,
```

In `src/issuebot/orchestrator/__init__.py` replace

```
    "CONTINUATION_DELAY_MS",
    "REVIEW_GRACE_TICKS",
    "TERMINAL_SWEEP_EVERY_TICKS",
```

with

```
    "CONTINUATION_DELAY_MS",
    "OBSERVED_STATES",
    "TERMINAL_SWEEP_EVERY_TICKS",
```


- [ ] **Step 4: Run the tests to verify they pass, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && timeout 300 uv run pytest -q`
Expected: lint clean; `602 passed, 5 skipped`.

Run: `DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q`
Expected: `607 passed`.

- [ ] **Step 5: Commit**

```bash
git add --all
git commit -m "feat: hand polled issues and the snapshot to observers, time the review grace, carry log_dir on run_ended"
```

---

### Task 3: The store (`store.py`)

**Files:**
- Create: `src/issuebot/db/store.py`
- Test: `tests/test_db_store.py`

**Interfaces:**
- Consumes: Task 1 (`connect`, `classify`, the errors, `migrate` in the tests); `GitHubLabels`; `role_for` from `issuebot.github`; the event types.
- Produces: `IssueSnapshot(issue, seen_at)`; the `Store` protocol (`connect`, `close`, `apply_event(event)`, `upsert_issues(snapshots)`, `write_snapshot(at, data)`); `PostgresStore(url, *, labels, connect=connect)` with `.connected`; the row helpers `issue_row`, `event_row`, `run_started_row`, `run_ended_row` and the SQL constants.

Spec: §5.

- [ ] **Step 1: Write the failing tests**


Create `tests/test_db_store.py` with the Write tool:

```python
"""Tests for PostgresStore against a real database (skipped without DATABASE_URL)."""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from issuebot.config import GitHubLabels
from issuebot.db import StoreError, StoreUnavailableError, connect, migrate
from issuebot.db.store import IssueSnapshot, PostgresStore
from issuebot.events import (
    Blocked,
    Event,
    IssueCancelled,
    IssueCompleted,
    NotificationSent,
    PrOpened,
    RunEnded,
    RunStarted,
    StateChanged,
)
from issuebot.github import Issue, LinkedPr, StateLabel

T0 = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
async def store(db_url: str) -> AsyncIterator[PostgresStore]:
    await migrate(db_url)
    store = PostgresStore(db_url, labels=GitHubLabels())
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


async def rows(db_url: str, query: str, *params: Any) -> list[dict[str, Any]]:
    conn = await connect(db_url)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(query, params or None)
            names = [column.name for column in cursor.description or []]
            return [dict(zip(names, row, strict=True)) for row in await cursor.fetchall()]
    finally:
        await conn.close()


def started(**overrides: Any) -> RunStarted:
    fields: dict[str, Any] = {
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "run_id": "run-1",
        "attempt": 2,
        "session_id": "sess-1",
        "workspace_path": "/workspaces/repo-42",
        "at": at(0),
    }
    fields.update(overrides)
    return RunStarted(**fields)


def ended(**overrides: Any) -> RunEnded:
    fields: dict[str, Any] = {
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "run_id": "run-1",
        "outcome": "succeeded",
        "error": None,
        "turns": 3,
        "input_tokens": 1000,
        "output_tokens": 50,
        "cost_usd": 0.75,
        "duration_s": 90.0,
        "log_dir": "/workspaces/repo-42/.issuebot/runs/run-1",
        "at": at(90),
    }
    fields.update(overrides)
    return RunEnded(**fields)


def moved(**overrides: Any) -> StateChanged:
    fields: dict[str, Any] = {
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "from_label": "issuebot/todo",
        "to_label": "issuebot/in-progress",
        "actor": "issuebot",
        "at": at(10),
    }
    fields.update(overrides)
    return StateChanged(**fields)


# --- runs -------------------------------------------------------------------------------------


async def test_run_started_then_ended_fills_one_row(store: PostgresStore, db_url: str) -> None:
    await store.apply_event(started())
    await store.apply_event(ended())
    (row,) = await rows(db_url, "SELECT * FROM runs")
    assert row["run_id"] == "run-1"
    assert (row["issue_number"], row["issue_identifier"], row["attempt"]) == (42, "repo-42", 2)
    assert (row["session_id"], row["workspace_path"]) == ("sess-1", "/workspaces/repo-42")
    assert (row["started_at"], row["ended_at"]) == (at(0), at(90))
    assert (row["outcome"], row["error"], row["turns"]) == ("succeeded", None, 3)
    assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (1000, 50, 0.75)
    assert row["duration_s"] == 90.0
    assert row["log_dir"] == "/workspaces/repo-42/.issuebot/runs/run-1"


async def test_run_ended_before_started_gives_the_same_row(
    store: PostgresStore, db_url: str
) -> None:
    await store.apply_event(ended())
    await store.apply_event(started())
    (row,) = await rows(db_url, "SELECT * FROM runs")
    assert (row["attempt"], row["session_id"], row["started_at"]) == (2, "sess-1", at(0))
    assert (row["outcome"], row["ended_at"], row["cost_usd"]) == ("succeeded", at(90), 0.75)


async def test_run_ended_alone_computes_the_start(store: PostgresStore, db_url: str) -> None:
    await store.apply_event(ended(outcome="failed", error="turn_failed: boom"))
    (row,) = await rows(db_url, "SELECT * FROM runs")
    assert row["started_at"] == at(0)
    assert (row["attempt"], row["session_id"], row["workspace_path"]) == (0, None, None)
    assert (row["outcome"], row["error"]) == ("failed", "turn_failed: boom")


async def test_an_empty_workspace_path_is_stored_as_null(store: PostgresStore, db_url: str) -> None:
    await store.apply_event(started(workspace_path=""))
    (row,) = await rows(db_url, "SELECT workspace_path FROM runs")
    assert row["workspace_path"] is None


# --- events -----------------------------------------------------------------------------------


EVERY_KIND: list[Event] = [
    moved(),
    started(),
    ended(),
    PrOpened(issue_number=42, issue_identifier="repo-42", pr_number=7, pr_url="u", at=at(1)),
    Blocked(issue_number=42, issue_identifier="repo-42", reason="stuck", at=at(2)),
    IssueCompleted(issue_number=42, issue_identifier="repo-42", pr_url="u", at=at(3)),
    IssueCancelled(issue_number=42, issue_identifier="repo-42", reason="closed", at=at(4)),
    NotificationSent(
        issue_number=42, issue_identifier="repo-42", channel="slack", about_kind="blocked", at=at(5)
    ),
]


async def test_every_kind_lands_in_events_with_its_payload(
    store: PostgresStore, db_url: str
) -> None:
    for event in EVERY_KIND:
        await store.apply_event(event)
    found = await rows(db_url, "SELECT * FROM events ORDER BY id")
    assert [row["kind"] for row in found] == [event.kind for event in EVERY_KIND]
    assert {row["issue_number"] for row in found} == {42}
    assert [row["run_id"] for row in found[:3]] == [None, "run-1", "run-1"]
    assert found[0]["at"] == at(10)
    assert found[0]["payload"] == moved().to_dict()
    assert found[2]["payload"]["log_dir"] == "/workspaces/repo-42/.issuebot/runs/run-1"
    assert found[4]["payload"] == {
        "kind": "blocked",
        "at": at(2).isoformat(),
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "reason": "stuck",
    }


# --- issues -----------------------------------------------------------------------------------


def snapshot(issue: Issue, seconds: float) -> IssueSnapshot:
    return IssueSnapshot(issue=issue, seen_at=at(seconds))


async def test_upsert_issues_inserts_and_keeps_the_newer_snapshot(
    store: PostgresStore, db_url: str, make_issue: Callable[..., Issue]
) -> None:
    pr = LinkedPr(number=7, url="https://x/pull/7", state="merged", merged_at=at(50))
    first = make_issue(title="First", labels=("issuebot/todo", "bug"))
    newer = make_issue(
        title="Renamed",
        state=StateLabel.REVIEW,
        state_labels=("issuebot/review",),
        labels=("issuebot/review", "bug"),
        linked_pr=pr,
        updated_at=at(40),
    )
    await store.upsert_issues([snapshot(first, 0)])
    await store.upsert_issues([snapshot(newer, 20), snapshot(first, 5)])
    (row,) = await rows(db_url, "SELECT * FROM issues")
    assert (row["number"], row["identifier"], row["title"]) == (42, "repo-42", "Renamed")
    assert (row["state"], row["state_label"]) == ("review", "issuebot/review")
    assert (row["github_state"], row["labels"]) == ("open", ["issuebot/review", "bug"])
    assert (row["pr_number"], row["pr_url"], row["pr_state"]) == (7, "https://x/pull/7", "merged")
    assert row["pr_merged_at"] == at(50)
    assert (row["updated_at"], row["closed_at"], row["seen_at"]) == (at(40), None, at(20))


async def test_upsert_issues_handles_conflict_and_unlabelled_issues(
    store: PostgresStore, db_url: str, make_issue: Callable[..., Issue]
) -> None:
    conflict = make_issue(
        number=1,
        identifier="repo-1",
        state=None,
        state_labels=("issuebot/todo", "issuebot/rework"),
        labels=("issuebot/todo", "issuebot/rework"),
    )
    bare = make_issue(number=2, identifier="repo-2", state=None, state_labels=(), labels=())
    await store.upsert_issues([snapshot(conflict, 0), snapshot(bare, 0)])
    found = await rows(db_url, "SELECT number, state, state_label FROM issues ORDER BY number")
    assert found == [
        {"number": 1, "state": None, "state_label": None},
        {"number": 2, "state": None, "state_label": None},
    ]


async def test_upsert_issues_with_nothing_is_a_noop(store: PostgresStore) -> None:
    await store.upsert_issues([])


async def test_state_changed_updates_the_row_when_newer(
    store: PostgresStore, db_url: str, make_issue: Callable[..., Issue]
) -> None:
    await store.upsert_issues([snapshot(make_issue(), 0)])
    await store.apply_event(moved(at=at(10)))
    (row,) = await rows(db_url, "SELECT state, state_label, seen_at FROM issues")
    assert row == {"state": "in_progress", "state_label": "issuebot/in-progress", "seen_at": at(10)}
    await store.apply_event(moved(to_label="issuebot/review", at=at(5)))  # older: ignored
    (row,) = await rows(db_url, "SELECT state, seen_at FROM issues")
    assert row == {"state": "in_progress", "seen_at": at(10)}
    await store.apply_event(moved(to_label="Issuebot/Review", actor="agent", at=at(20)))
    (row,) = await rows(db_url, "SELECT state, state_label FROM issues")
    assert row == {"state": "review", "state_label": "Issuebot/Review"}
    await store.apply_event(moved(to_label="unrelated", actor="human", at=at(30)))
    (row,) = await rows(db_url, "SELECT state, state_label FROM issues")
    assert row == {"state": None, "state_label": "unrelated"}
    await store.apply_event(moved(to_label=None, at=at(40)))
    (row,) = await rows(db_url, "SELECT state, state_label FROM issues")
    assert row == {"state": None, "state_label": None}


async def test_state_changed_for_an_unknown_issue_is_harmless(
    store: PostgresStore, db_url: str
) -> None:
    await store.apply_event(moved())
    assert await rows(db_url, "SELECT * FROM issues") == []
    assert len(await rows(db_url, "SELECT * FROM events")) == 1


async def test_completed_and_cancelled_close_the_row(
    store: PostgresStore, db_url: str, make_issue: Callable[..., Issue]
) -> None:
    await store.upsert_issues([snapshot(make_issue(), 0)])
    await store.apply_event(
        IssueCompleted(issue_number=42, issue_identifier="repo-42", pr_url=None, at=at(10))
    )
    (row,) = await rows(db_url, "SELECT github_state, closed_at, seen_at FROM issues")
    assert row == {"github_state": "closed", "closed_at": at(10), "seen_at": at(10)}
    await store.upsert_issues([snapshot(make_issue(closed_at=at(8), github_state="closed"), 20)])
    await store.apply_event(
        IssueCancelled(issue_number=42, issue_identifier="repo-42", reason="x", at=at(30))
    )
    (row,) = await rows(db_url, "SELECT github_state, closed_at, seen_at FROM issues")
    assert row == {"github_state": "closed", "closed_at": at(8), "seen_at": at(30)}


# --- snapshot ---------------------------------------------------------------------------------


async def test_write_snapshot_keeps_exactly_one_row(store: PostgresStore, db_url: str) -> None:
    await store.write_snapshot(at(0), {"tick_count": 1, "running": []})
    await store.write_snapshot(at(30), {"tick_count": 2, "running": [{"issue_number": 1}]})
    (row,) = await rows(db_url, "SELECT at, written_at, data FROM runtime_snapshot")
    assert row["at"] == at(30)
    assert row["data"] == {"tick_count": 2, "running": [{"issue_number": 1}]}
    assert row["written_at"].tzinfo is not None
    assert row["written_at"] > at(30)


# --- errors -----------------------------------------------------------------------------------


async def test_writes_need_a_connection(db_url: str) -> None:
    await migrate(db_url)
    store = PostgresStore(db_url, labels=GitHubLabels())
    with pytest.raises(StoreUnavailableError, match="not connected"):
        await store.apply_event(moved())
    await store.connect()
    assert store.connected
    await store.close()
    assert not store.connected
    with pytest.raises(StoreUnavailableError, match="not connected"):
        await store.write_snapshot(at(0), {})


async def test_a_bad_value_is_a_store_error_not_an_outage(store: PostgresStore) -> None:
    with pytest.raises(StoreError, match="TypeError"):
        await store.write_snapshot(at(0), {"bad": {1, 2}})
    assert store.connected
    await store.write_snapshot(at(0), {"ok": True})


async def test_a_missing_table_is_a_store_error(db_url: str) -> None:
    store = PostgresStore(db_url, labels=GitHubLabels())
    await store.connect()
    try:
        with pytest.raises(StoreError, match="UndefinedTable"):
            await store.write_snapshot(at(0), {})
    finally:
        await store.close()


async def test_connect_failure_is_unavailable_and_redacted() -> None:
    store = PostgresStore("postgresql://u:s3cret@127.0.0.1:1/db", labels=GitHubLabels())
    with pytest.raises(StoreUnavailableError) as exc:
        await store.connect()
    assert "s3cret" not in exc.value.message
    assert not store.connected
```


- [ ] **Step 2: Run the tests to verify they fail**

Run: `DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 120 uv run pytest -q tests/test_db_store.py`
Expected: `1 error` during collection: `ModuleNotFoundError: No module named 'issuebot.db.store'`.

- [ ] **Step 3: The store**


Create `src/issuebot/db/store.py` with the Write tool:

```python
"""The SQL writes behind the sink: one connection, three methods, errors mapped and redacted."""

import contextlib
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from issuebot.config import GitHubLabels
from issuebot.db.connection import Connector, classify, connect
from issuebot.db.errors import StoreError, StoreUnavailableError
from issuebot.events import (
    Event,
    IssueCancelled,
    IssueCompleted,
    IssueEvent,
    RunEnded,
    RunStarted,
    StateChanged,
)
from issuebot.github import Issue, role_for


@dataclass(frozen=True, slots=True)
class IssueSnapshot:
    """One polled issue and when it was observed; the write is guarded by ``seen_at``."""

    issue: Issue
    seen_at: datetime


class Store(Protocol):
    """What the sink writes through. ``PostgresStore`` is the real one; tests use a fake."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def apply_event(self, event: Event) -> None: ...

    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None: ...

    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None: ...


INSERT_EVENT = """
INSERT INTO events (at, kind, issue_number, run_id, payload)
VALUES (%(at)s, %(kind)s, %(issue_number)s, %(run_id)s, %(payload)s)
"""

RUN_STARTED = """
INSERT INTO runs (run_id, issue_number, issue_identifier, attempt, session_id, started_at,
                  workspace_path)
VALUES (%(run_id)s, %(issue_number)s, %(issue_identifier)s, %(attempt)s, %(session_id)s,
        %(started_at)s, %(workspace_path)s)
ON CONFLICT (run_id) DO UPDATE SET
    issue_number = EXCLUDED.issue_number,
    issue_identifier = EXCLUDED.issue_identifier,
    attempt = EXCLUDED.attempt,
    session_id = EXCLUDED.session_id,
    started_at = EXCLUDED.started_at,
    workspace_path = EXCLUDED.workspace_path
"""

RUN_ENDED = """
INSERT INTO runs (run_id, issue_number, issue_identifier, started_at, ended_at, outcome, error,
                  turns, input_tokens, output_tokens, cost_usd, duration_s, log_dir)
VALUES (%(run_id)s, %(issue_number)s, %(issue_identifier)s, %(started_at)s, %(ended_at)s,
        %(outcome)s, %(error)s, %(turns)s, %(input_tokens)s, %(output_tokens)s, %(cost_usd)s,
        %(duration_s)s, %(log_dir)s)
ON CONFLICT (run_id) DO UPDATE SET
    ended_at = EXCLUDED.ended_at,
    outcome = EXCLUDED.outcome,
    error = EXCLUDED.error,
    turns = EXCLUDED.turns,
    input_tokens = EXCLUDED.input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    cost_usd = EXCLUDED.cost_usd,
    duration_s = EXCLUDED.duration_s,
    log_dir = EXCLUDED.log_dir
"""

STATE_CHANGED = """
UPDATE issues SET state = %(state)s, state_label = %(state_label)s, seen_at = %(at)s
WHERE number = %(number)s AND seen_at <= %(at)s
"""

ISSUE_CLOSED = """
UPDATE issues
SET github_state = 'closed', closed_at = coalesce(closed_at, %(at)s), seen_at = %(at)s
WHERE number = %(number)s AND seen_at <= %(at)s
"""

UPSERT_ISSUE = """
INSERT INTO issues (number, identifier, title, state, state_label, github_state, url, labels,
                    pr_number, pr_url, pr_state, pr_merged_at, created_at, updated_at, closed_at,
                    seen_at)
VALUES (%(number)s, %(identifier)s, %(title)s, %(state)s, %(state_label)s, %(github_state)s,
        %(url)s, %(labels)s, %(pr_number)s, %(pr_url)s, %(pr_state)s, %(pr_merged_at)s,
        %(created_at)s, %(updated_at)s, %(closed_at)s, %(seen_at)s)
ON CONFLICT (number) DO UPDATE SET
    identifier = EXCLUDED.identifier,
    title = EXCLUDED.title,
    state = EXCLUDED.state,
    state_label = EXCLUDED.state_label,
    github_state = EXCLUDED.github_state,
    url = EXCLUDED.url,
    labels = EXCLUDED.labels,
    pr_number = EXCLUDED.pr_number,
    pr_url = EXCLUDED.pr_url,
    pr_state = EXCLUDED.pr_state,
    pr_merged_at = EXCLUDED.pr_merged_at,
    created_at = EXCLUDED.created_at,
    updated_at = EXCLUDED.updated_at,
    closed_at = EXCLUDED.closed_at,
    seen_at = EXCLUDED.seen_at
WHERE issues.seen_at <= EXCLUDED.seen_at
"""

WRITE_SNAPSHOT = """
INSERT INTO runtime_snapshot (id, at, written_at, data) VALUES (true, %(at)s, now(), %(data)s)
ON CONFLICT (id) DO UPDATE SET at = EXCLUDED.at, written_at = now(), data = EXCLUDED.data
"""


def issue_row(snapshot: IssueSnapshot) -> dict[str, Any]:
    """The bound parameters of UPSERT_ISSUE for one polled issue."""
    issue = snapshot.issue
    pr = issue.linked_pr
    return {
        "number": issue.number,
        "identifier": issue.identifier,
        "title": issue.title,
        "state": issue.state.value if issue.state is not None else None,
        "state_label": issue.state_labels[0] if len(issue.state_labels) == 1 else None,
        "github_state": issue.github_state,
        "url": issue.url,
        "labels": list(issue.labels),
        "pr_number": pr.number if pr is not None else None,
        "pr_url": pr.url if pr is not None else None,
        "pr_state": pr.state if pr is not None else None,
        "pr_merged_at": pr.merged_at if pr is not None else None,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "closed_at": issue.closed_at,
        "seen_at": snapshot.seen_at,
    }


class PostgresStore:
    """Writes events, runs, issues and the runtime snapshot over one autocommit connection."""

    def __init__(self, url: str, *, labels: GitHubLabels, connect: Connector = connect) -> None:
        self._url = url
        self._labels = labels
        self._connect = connect
        self._conn: AsyncConnection | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    async def connect(self) -> None:
        """Open the connection, replacing a previous one (broken or not)."""
        await self.close()
        async with self._guard():
            self._conn = await self._connect(self._url)

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(psycopg.Error):
                await conn.close()

    async def apply_event(self, event: Event) -> None:
        """Append the event; then upsert the run or update the issue it is about."""
        conn = self._require()
        async with self._guard(), conn.transaction():
            await conn.execute(INSERT_EVENT, event_row(event))
            if isinstance(event, RunStarted):
                await conn.execute(RUN_STARTED, run_started_row(event))
            elif isinstance(event, RunEnded):
                await conn.execute(RUN_ENDED, run_ended_row(event))
            elif isinstance(event, StateChanged):
                await conn.execute(STATE_CHANGED, self._state_changed_row(event))
            elif isinstance(event, IssueCompleted | IssueCancelled):
                await conn.execute(ISSUE_CLOSED, {"number": event.issue_number, "at": event.at})

    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None:
        if not issues:
            return
        conn = self._require()
        async with self._guard(), conn.transaction(), conn.cursor() as cursor:
            await cursor.executemany(UPSERT_ISSUE, [issue_row(snapshot) for snapshot in issues])

    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None:
        conn = self._require()
        async with self._guard():
            await conn.execute(WRITE_SNAPSHOT, {"at": at, "data": Jsonb(dict(data))})

    def _require(self) -> AsyncConnection:
        if self._conn is None or self._conn.closed:
            raise StoreUnavailableError("not connected")
        return self._conn

    @asynccontextmanager
    async def _guard(self) -> AsyncIterator[None]:
        """Map psycopg errors to StoreUnavailableError/StoreError with the URL redacted."""
        try:
            yield
        except psycopg.Error as exc:
            raise classify(exc, self._url) from exc
        except (TypeError, ValueError) as exc:
            raise StoreError(f"{type(exc).__name__}: {exc}") from exc

    def _state_changed_row(self, event: StateChanged) -> dict[str, Any]:
        role = role_for(self._labels, event.to_label) if event.to_label is not None else None
        return {
            "number": event.issue_number,
            "state": role.value if role is not None else None,
            "state_label": event.to_label,
            "at": event.at,
        }


def event_row(event: Event) -> dict[str, Any]:
    payload = event.to_dict()
    return {
        "at": event.at,
        "kind": event.kind,
        "issue_number": event.issue_number if isinstance(event, IssueEvent) else None,
        "run_id": payload.get("run_id"),
        "payload": Jsonb(payload),
    }


def run_started_row(event: RunStarted) -> dict[str, Any]:
    return {
        "run_id": event.run_id,
        "issue_number": event.issue_number,
        "issue_identifier": event.issue_identifier,
        "attempt": event.attempt,
        "session_id": event.session_id,
        "started_at": event.at,
        "workspace_path": event.workspace_path or None,
    }


def run_ended_row(event: RunEnded) -> dict[str, Any]:
    return {
        "run_id": event.run_id,
        "issue_number": event.issue_number,
        "issue_identifier": event.issue_identifier,
        "started_at": event.at - timedelta(seconds=event.duration_s),
        "ended_at": event.at,
        "outcome": event.outcome,
        "error": event.error,
        "turns": event.turns,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "cost_usd": event.cost_usd,
        "duration_s": event.duration_s,
        "log_dir": event.log_dir,
    }
```


- [ ] **Step 4: Run the tests to verify they pass, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && timeout 300 uv run pytest -q`
Expected: lint clean; `603 passed, 20 skipped` (fifteen store tests need the database; `test_connect_failure_is_unavailable_and_redacted` does not).

Run: `DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q`
Expected: `623 passed`.

- [ ] **Step 5: Commit**

```bash
git add --all
git commit -m "feat: PostgresStore writes events, runs, issues and the runtime snapshot"
```

---

### Task 4: The sink (`sink.py`)

**Files:**
- Create: `src/issuebot/db/sink.py`
- Test: `tests/test_db_sink.py`

**Interfaces:**
- Consumes: Task 3's `Store` protocol and `IssueSnapshot`; Task 1's `reconnect_delay` and errors.
- Produces: `SnapshotLike` (structural: `at` and `to_dict()`); `PostgresSink(store, *, sleep=asyncio.sleep, now=utcnow)` with `name = "postgres"`, `handle(event)`, `record_issues(issues)`, `record_snapshot(snapshot)`, `start()`, `async close()`, counters `written`, `failed`, `dropped`, `reconnects`; `QUEUE_LIMIT = 1000`, `DRAIN_TIMEOUT_S = 10.0`.

Spec: §4.

- [ ] **Step 1: Write the failing tests**


Create `tests/test_db_sink.py` with the Write tool:

```python
"""Tests for PostgresSink with a fake store (no database)."""

import asyncio
import io
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from issuebot.db import StoreError, StoreUnavailableError
from issuebot.db import sink as sink_module
from issuebot.db.sink import PostgresSink
from issuebot.db.store import IssueSnapshot
from issuebot.events import Blocked, Event, EventBus, LogSink, StateChanged
from issuebot.github import Issue
from issuebot.log import configure_logging

T0 = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class FakeStore:
    """Records every write; raises what the test scripts; can hang."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.connects = 0
        self.closed = False
        self.fail_connect: list[Exception] = []
        self.fail_next: list[Exception] = []
        self.hang = False

    async def connect(self) -> None:
        self.connects += 1
        if self.fail_connect:
            raise self.fail_connect.pop(0)

    async def close(self) -> None:
        self.closed = True

    async def _gate(self) -> None:
        if self.hang:
            await asyncio.Event().wait()
        if self.fail_next:
            raise self.fail_next.pop(0)

    async def apply_event(self, event: Event) -> None:
        await self._gate()
        self.calls.append(("event", event))

    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None:
        await self._gate()
        self.calls.append(("issues", list(issues)))

    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None:
        await self._gate()
        self.calls.append(("snapshot", at, dict(data)))


class FakeSnapshot:
    def __init__(self, at: datetime, **data: Any) -> None:
        self.at = at
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class Harness:
    def __init__(self) -> None:
        self.store = FakeStore()
        self.sleeps: list[float] = []
        self.clock = Clock()
        self.stream = io.StringIO()
        configure_logging(fmt="json", level="DEBUG", stream=self.stream)  # type: ignore[arg-type]
        self.sink = PostgresSink(self.store, sleep=self.sleep, now=self.clock)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    def logged(self, event: str) -> list[dict[str, Any]]:
        lines = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        return [line for line in lines if line["event"] == event]


def blocked(number: int, reason: str = "x") -> Blocked:
    return Blocked(issue_number=number, issue_identifier=f"repo-{number}", reason=reason)


@pytest.fixture
def h() -> Harness:
    return Harness()


# --- enqueue and order --------------------------------------------------------------------


async def test_events_published_before_start_are_written_in_order(h: Harness) -> None:
    bus = EventBus([LogSink(), h.sink])
    bus.publish(blocked(1))
    bus.publish(blocked(2))
    h.sink.start()
    await h.sink.close()
    assert [call[1].issue_number for call in h.store.calls] == [1, 2]
    assert (h.sink.written, h.sink.failed, h.sink.dropped) == (2, 0, 0)
    assert h.store.connects == 1 and h.store.closed
    assert h.logged("db_connected")[0]["attempt"] == 1
    closed = h.logged("db_sink_closed")[0]
    assert (closed["written"], closed["failed"], closed["dropped"]) == (2, 0, 0)


async def test_the_cap_drops_and_logs_but_delivery_continues(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sink_module, "QUEUE_LIMIT", 2)
    for number in (1, 2, 3):
        h.sink.handle(blocked(number))
    assert h.sink.dropped == 1
    full = h.logged("db_queue_full")[0]
    assert (full["kind"], full["issue_number"], full["limit"]) == ("blocked", 3, 2)
    h.sink.start()
    await h.sink.close()
    assert [call[1].issue_number for call in h.store.calls] == [1, 2]


async def test_record_issues_merges_by_number_and_stamps_seen_at(
    h: Harness, make_issue: Callable[..., Issue]
) -> None:
    first = make_issue(number=1, identifier="repo-1", title="one")
    second = make_issue(number=2, identifier="repo-2", title="two")
    renamed = make_issue(number=1, identifier="repo-1", title="one, renamed")
    h.sink.record_issues([first, second])
    h.sink.record_issues([renamed])
    h.sink.record_issues([])
    h.sink.start()
    await h.sink.close()
    assert len(h.store.calls) == 1
    kind, batch = h.store.calls[0]
    assert kind == "issues"
    assert [(s.issue.number, s.issue.title, s.seen_at) for s in batch] == [
        (1, "one, renamed", T0 + timedelta(seconds=2)),
        (2, "two", T0 + timedelta(seconds=1)),
    ]
    assert h.sink.written == 1


async def test_record_snapshot_keeps_only_the_latest(h: Harness) -> None:
    h.sink.record_snapshot(FakeSnapshot(T0, tick_count=1))
    h.sink.record_snapshot(FakeSnapshot(T0 + timedelta(seconds=30), tick_count=2))
    h.sink.start()
    await h.sink.close()
    assert h.store.calls == [("snapshot", T0 + timedelta(seconds=30), {"tick_count": 2})]


async def test_markers_are_requeued_after_the_drain_took_them(
    h: Harness, make_issue: Callable[..., Issue]
) -> None:
    h.sink.start()
    h.sink.record_issues([make_issue(number=1, identifier="repo-1")])
    h.sink.record_snapshot(FakeSnapshot(T0, tick_count=1))
    await h.settle()
    h.sink.record_issues([make_issue(number=2, identifier="repo-2")])
    h.sink.record_snapshot(FakeSnapshot(T0, tick_count=2))
    await h.sink.close()
    kinds = [call[0] for call in h.store.calls]
    assert kinds == ["issues", "snapshot", "issues", "snapshot"]
    assert [s.issue.number for s in h.store.calls[2][1]] == [2]
    assert h.store.calls[3][2] == {"tick_count": 2}


async def test_events_and_markers_keep_publication_order(
    h: Harness, make_issue: Callable[..., Issue]
) -> None:
    h.sink.record_issues([make_issue()])
    h.sink.handle(blocked(1))
    h.sink.record_snapshot(FakeSnapshot(T0))
    h.sink.handle(blocked(2))
    h.sink.start()
    await h.sink.close()
    assert [call[0] for call in h.store.calls] == ["issues", "event", "snapshot", "event"]


# --- failures -----------------------------------------------------------------------------


async def test_a_lost_connection_reconnects_and_retries_the_item(h: Harness) -> None:
    h.store.fail_next = [StoreUnavailableError("server closed the connection")]
    h.sink.handle(blocked(1))
    h.sink.handle(blocked(2))
    h.sink.start()
    await h.sink.close()
    assert [call[1].issue_number for call in h.store.calls] == [1, 2]
    assert (h.sink.written, h.sink.failed, h.sink.reconnects) == (2, 0, 1)
    assert h.store.connects == 2
    assert h.sleeps == []
    retry = h.logged("db_write_retry")[0]
    assert (retry["kind"], retry["issue_number"]) == ("blocked", 1)
    assert retry["error"] == "server closed the connection"


async def test_connect_failures_back_off_before_the_first_write(h: Harness) -> None:
    h.store.fail_connect = [StoreUnavailableError("refused") for _ in range(3)]
    h.sink.handle(blocked(1))
    h.sink.start()
    await h.sink.close()
    assert h.sleeps == [1.0, 2.0, 4.0]
    assert h.store.connects == 4
    assert (h.sink.written, h.sink.reconnects) == (1, 0)
    failed = h.logged("db_connect_failed")
    assert [line["attempt"] for line in failed] == [1, 2, 3]
    assert failed[-1]["delay_s"] == 4.0
    assert h.logged("db_connected")[0]["attempt"] == 4


async def test_a_statement_failure_drops_the_item_and_continues(h: Harness) -> None:
    h.store.fail_next = [StoreError("DataError: bad value")]
    h.sink.handle(blocked(1))
    h.sink.handle(blocked(2))
    h.sink.start()
    await h.sink.close()
    assert [call[1].issue_number for call in h.store.calls] == [2]
    assert (h.sink.written, h.sink.failed, h.sink.dropped) == (1, 1, 0)
    assert h.store.connects == 1
    failed = h.logged("db_write_failed")[0]
    assert (failed["issue_number"], failed["error"]) == (1, "DataError: bad value")


async def test_an_unexpected_exception_is_logged_and_the_loop_survives(h: Harness) -> None:
    h.store.fail_next = [RuntimeError("bug")]
    h.sink.handle(blocked(1))
    h.sink.handle(blocked(2))
    h.sink.start()
    await h.sink.close()
    assert [call[1].issue_number for call in h.store.calls] == [2]
    assert (h.sink.written, h.sink.failed) == (1, 1)
    crashed = h.logged("db_write_crashed")[0]
    assert crashed["issue_number"] == 1
    assert "bug" in crashed["exception"]


# --- lifetime -----------------------------------------------------------------------------


async def test_close_times_out_on_a_hanging_store(
    h: Harness, monkeypatch: pytest.MonkeyPatch, make_issue: Callable[..., Issue]
) -> None:
    monkeypatch.setattr(sink_module, "DRAIN_TIMEOUT_S", 0.05)
    h.store.hang = True
    h.sink.handle(blocked(1))
    h.sink.handle(blocked(2))
    h.sink.record_issues([make_issue()])
    h.sink.start()
    await h.sink.close()
    assert h.store.calls == []
    assert (h.sink.written, h.sink.failed, h.sink.dropped) == (0, 1, 2)
    assert h.store.closed
    timeout = h.logged("db_drain_timeout")[0]
    assert (timeout["left"], timeout["timeout_s"]) == (2, 0.05)
    assert h.logged("db_write_cancelled")[0]["issue_number"] == 1
    assert h.sink._task is not None and h.sink._task.cancelled()


async def test_close_twice_and_close_before_start_are_noops(h: Harness) -> None:
    await h.sink.close()
    assert not h.store.closed
    h.sink.start()
    await h.sink.close()
    await h.sink.close()
    assert len(h.logged("db_sink_closed")) == 1


async def test_start_twice_raises(h: Harness) -> None:
    h.sink.start()
    with pytest.raises(RuntimeError, match="already started"):
        h.sink.start()
    await h.sink.close()


async def test_after_close_events_are_dropped_and_records_ignored(
    h: Harness, make_issue: Callable[..., Issue]
) -> None:
    h.sink.start()
    await h.sink.close()
    h.sink.handle(blocked(1))
    h.sink.record_issues([make_issue()])
    h.sink.record_snapshot(FakeSnapshot(T0))
    assert h.sink.dropped == 1
    assert h.logged("db_sink_closed_drop")[0]["issue_number"] == 1
    assert h.sink._queue.qsize() == 0


async def test_a_bare_event_has_no_issue_number(h: Harness) -> None:
    class Bare(Event):
        pass

    h.sink.handle(Bare())
    h.sink.start()
    await h.sink.close()
    assert h.store.calls[0][1].kind == "event"
    assert h.sink.written == 1


async def test_state_changed_reaches_the_store_through_the_bus(h: Harness) -> None:
    bus = EventBus([h.sink])
    h.sink.start()
    bus.publish(
        StateChanged(
            issue_number=1,
            issue_identifier="repo-1",
            from_label=None,
            to_label="issuebot/todo",
            actor="human",
        )
    )
    await h.settle()
    assert h.store.calls[0][1].kind == "state_changed"
    await h.sink.close()
```


- [ ] **Step 2: Run the tests to verify they fail**

Run: `timeout 120 uv run pytest -q tests/test_db_sink.py`
Expected: `1 error` during collection: `ModuleNotFoundError: No module named 'issuebot.db.sink'`.

- [ ] **Step 3: The sink**


Create `src/issuebot/db/sink.py` with the Write tool:

```python
"""PostgresSink: enqueue in ``handle``/``record_*``; one drain task writes and reconnects."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from issuebot.db.connection import reconnect_delay
from issuebot.db.errors import StoreError, StoreUnavailableError
from issuebot.db.store import IssueSnapshot, Store
from issuebot.events import Event, IssueEvent
from issuebot.github import Issue
from issuebot.log import get_logger

QUEUE_LIMIT = 1000
DRAIN_TIMEOUT_S = 10.0


class SnapshotLike(Protocol):
    """What ``record_snapshot`` needs from a RuntimeSnapshot; keeps ``db`` off ``orchestrator``."""

    @property
    def at(self) -> datetime: ...

    def to_dict(self) -> dict[str, Any]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _EventItem:
    event: Event


_Marker = Literal["issues", "snapshot"]
_Item = _EventItem | _Marker
_Work = (
    _EventItem
    | tuple[Literal["issues"], list[IssueSnapshot]]
    | tuple[Literal["snapshot"], datetime, dict[str, Any]]
)


class PostgresSink:
    """Writes events, polled issues and the runtime snapshot from one background task.

    ``handle``, ``record_issues`` and ``record_snapshot`` only enqueue; ``start`` creates
    the drain task; ``close`` drains what is queued (bounded) and stops it. A lost
    connection is retried with backoff and the item in flight is retried, not dropped.
    """

    name = "postgres"

    def __init__(
        self,
        store: Store,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._sleep = sleep
        self._now = now
        self._queue: asyncio.Queue[_Item | None] = asyncio.Queue()
        self._issues: dict[int, IssueSnapshot] = {}
        self._issues_queued = False
        self._snapshot: tuple[datetime, dict[str, Any]] | None = None
        self._snapshot_queued = False
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._connected = False
        self._ever_connected = False
        self.written = 0
        self.failed = 0
        self.dropped = 0
        self.reconnects = 0
        self._log = get_logger(__name__)

    # --- the enqueue side (never blocks, never raises) ------------------------------------

    def handle(self, event: Event) -> None:
        number = event.issue_number if isinstance(event, IssueEvent) else None
        if self._closed:
            self.dropped += 1
            self._log.debug("db_sink_closed_drop", kind=event.kind, issue_number=number)
            return
        if self._queue.qsize() >= QUEUE_LIMIT:
            self.dropped += 1
            self._log.warning(
                "db_queue_full", kind=event.kind, issue_number=number, limit=QUEUE_LIMIT
            )
            return
        self._queue.put_nowait(_EventItem(event))

    def record_issues(self, issues: Sequence[Issue]) -> None:
        """Merge polled snapshots into the pending batch; a later snapshot replaces an earlier."""
        if self._closed or not issues:
            return
        seen_at = self._now()
        for issue in issues:
            self._issues[issue.number] = IssueSnapshot(issue=issue, seen_at=seen_at)
        if not self._issues_queued:
            self._issues_queued = True
            self._queue.put_nowait("issues")

    def record_snapshot(self, snapshot: SnapshotLike) -> None:
        """Keep the latest runtime snapshot; it is written once the drain task gets to it."""
        if self._closed:
            return
        self._snapshot = (snapshot.at, snapshot.to_dict())
        if not self._snapshot_queued:
            self._snapshot_queued = True
            self._queue.put_nowait("snapshot")

    # --- lifetime ------------------------------------------------------------------------

    def start(self) -> None:
        """Create the drain task on the running loop; the first connect happens there."""
        if self._task is not None:
            raise RuntimeError("PostgresSink is already started")
        self._task = asyncio.create_task(self._drain(), name="issuebot-postgres-sink")
        self._log.info("db_sink_started")

    async def close(self) -> None:
        """Write what is queued for at most DRAIN_TIMEOUT_S, then stop and close the store."""
        if self._task is None or self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)
        try:
            await asyncio.wait_for(self._task, DRAIN_TIMEOUT_S)
        except TimeoutError:
            left = 0
            while True:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is not None:
                    left += 1
            self.dropped += left
            self._log.warning("db_drain_timeout", left=left, timeout_s=DRAIN_TIMEOUT_S)
        await self._store.close()
        self._log.info(
            "db_sink_closed",
            written=self.written,
            failed=self.failed,
            dropped=self.dropped,
            reconnects=self.reconnects,
        )

    # --- the drain task --------------------------------------------------------------------

    async def _drain(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            work = self._take(item)
            try:
                await self._write(work)
            except asyncio.CancelledError:
                self.failed += 1
                self._log.warning("db_write_cancelled", **_describe(work))
                raise
            except Exception:
                self.failed += 1
                self._log.exception("db_write_crashed", **_describe(work))

    def _take(self, item: _Item) -> _Work:
        """Resolve a queue item into the work to do, taking the pending batch or slot now."""
        if isinstance(item, _EventItem):
            return item
        if item == "issues":
            batch = list(self._issues.values())
            self._issues = {}
            self._issues_queued = False
            return ("issues", batch)
        at, data = self._snapshot or (self._now(), {})
        self._snapshot = None
        self._snapshot_queued = False
        return ("snapshot", at, data)

    async def _write(self, work: _Work) -> None:
        while True:
            if not self._connected:
                await self._ensure_connected()
            try:
                await self._apply(work)
            except StoreUnavailableError as exc:
                self._connected = False
                self._log.warning("db_write_retry", error=exc.message, **_describe(work))
                continue
            except StoreError as exc:
                self.failed += 1
                self._log.warning("db_write_failed", error=exc.message, **_describe(work))
                return
            self.written += 1
            return

    async def _ensure_connected(self) -> None:
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._store.connect()
            except StoreUnavailableError as exc:
                delay = reconnect_delay(attempt)
                self._log.warning(
                    "db_connect_failed", attempt=attempt, error=exc.message, delay_s=delay
                )
                await self._sleep(delay)
                continue
            self._connected = True
            if self._ever_connected:
                self.reconnects += 1
            self._ever_connected = True
            self._log.info("db_connected", attempt=attempt, reconnects=self.reconnects)
            return

    async def _apply(self, work: _Work) -> None:
        if isinstance(work, _EventItem):
            await self._store.apply_event(work.event)
        elif work[0] == "issues":
            await self._store.upsert_issues(work[1])
        else:
            await self._store.write_snapshot(work[1], work[2])


def _describe(work: _Work) -> dict[str, Any]:
    if isinstance(work, _EventItem):
        event = work.event
        number = event.issue_number if isinstance(event, IssueEvent) else None
        return {"kind": event.kind, "issue_number": number}
    if work[0] == "issues":
        return {"kind": "issues", "count": len(work[1])}
    return {"kind": "snapshot"}
```


- [ ] **Step 4: Run the tests to verify they pass, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && timeout 300 uv run pytest -q`
Expected: lint clean; `619 passed, 20 skipped` (all sixteen sink tests are hermetic).

Run: `DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q`
Expected: `639 passed`.

- [ ] **Step 5: Commit**

```bash
git add --all
git commit -m "feat: PostgresSink queues events, polled issues and snapshots and writes them in the background"
```

---

### Task 5: The refresh listener, the query module and the `Database` facade

**Files:**
- Create: `src/issuebot/db/listen.py`, `src/issuebot/db/queries.py`, `src/issuebot/db/database.py`
- Modify: `src/issuebot/db/__init__.py` (the final export list)
- Test: `tests/test_db_listen.py`, `tests/test_db_queries.py`, `tests/test_db_database.py`

**Interfaces:**
- Consumes: Tasks 1, 3 and 4.
- Produces: `RefreshListener(url, on_notify, *, connect=connect, sleep=asyncio.sleep)` with `start()`, `async close()`, `notified`, `reconnects`; `REFRESH_CHANNEL = "issuebot_refresh"`; the view models `IssueRow`, `RunRow`, `EventRow`, `DailyPoint(day, closed, runs)`, `SnapshotRow(at, written_at, data)`; `Queries(conn)` with `closed_count(window)`, `runs_count(window)`, `daily_series(days)`, `issues_by_state()`, `runs_for_issue(number)`, `recent_events(limit)`, `snapshot()`; `COMPLETE_LIMIT = 50`; `Probe(server_version, schema_version, latest_version)` with `.behind`/`.ahead`; `Database(url, *, connect=connect)` with `description`, `async migrate()`, `async probe()`, `queries()` (an async context manager yielding `Queries`; psycopg errors inside become `DatabaseError`), `store(labels)`, `listener(on_notify)`, `async notify_refresh()`.

Spec: §6, §7 (the listener), §9.1.

- [ ] **Step 1: Write the failing tests**


Create `tests/test_db_listen.py` with the Write tool:

```python
"""Tests for RefreshListener: a fake connection (hermetic) and one real NOTIFY round trip."""

import asyncio
import io
import json
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest

from issuebot.db import connect, migrate
from issuebot.db.database import Database
from issuebot.db.listen import REFRESH_CHANNEL, RefreshListener
from issuebot.log import configure_logging

URL = "postgresql://issuebot:s3cret@db.example/issuebot"


class FakeNotify:
    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.payload = ""
        self.pid = 1


class FakeConnection:
    """``notifies()`` yields what the test feeds; an exception fed ends the stream with it."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.feed: asyncio.Queue[FakeNotify | Exception] = asyncio.Queue()
        self.closed = False

    async def execute(self, query: str, params: Any = None) -> None:
        self.executed.append(query)

    async def close(self) -> None:
        self.closed = True

    async def notifies(self) -> AsyncIterator[FakeNotify]:
        while True:
            item = await self.feed.get()
            if isinstance(item, Exception):
                raise item
            yield item


class Harness:
    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self.fail_connect: list[Exception] = []
        self.sleeps: list[float] = []
        self.calls = 0
        self.stream = io.StringIO()
        configure_logging(fmt="json", level="DEBUG", stream=self.stream)  # type: ignore[arg-type]
        self.listener = RefreshListener(URL, self.on_notify, connect=self.connect, sleep=self.sleep)

    async def connect(self, url: str) -> Any:
        assert url == URL
        if self.fail_connect:
            raise self.fail_connect.pop(0)
        conn = FakeConnection()
        self.connections.append(conn)
        return conn

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)

    def on_notify(self) -> None:
        self.calls += 1

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    def logged(self, event: str) -> list[dict[str, Any]]:
        lines = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        return [line for line in lines if line["event"] == event]


@pytest.fixture
def h() -> Harness:
    return Harness()


async def test_a_notification_calls_the_callback(h: Harness) -> None:
    h.listener.start()
    await h.settle()
    (conn,) = h.connections
    assert conn.executed == [f"LISTEN {REFRESH_CHANNEL}"]
    assert h.logged("db_listen_started")[0]["channel"] == "issuebot_refresh"
    conn.feed.put_nowait(FakeNotify(REFRESH_CHANNEL))
    conn.feed.put_nowait(FakeNotify(REFRESH_CHANNEL))
    await h.settle()
    assert (h.calls, h.listener.notified) == (2, 2)
    assert len(h.logged("db_refresh_received")) == 2
    await h.listener.close()
    assert conn.closed
    closed = h.logged("db_listen_closed")[0]
    assert (closed["notified"], closed["reconnects"]) == (2, 0)


async def test_a_lost_connection_reconnects_with_backoff(h: Harness) -> None:
    h.listener.start()
    await h.settle()
    h.connections[0].feed.put_nowait(psycopg.OperationalError(f"server closed {URL}"))
    await h.settle()
    assert len(h.connections) == 2
    assert h.connections[0].closed
    assert h.sleeps == [1.0]
    assert h.listener.reconnects == 1
    lost = h.logged("db_listen_lost")[0]
    assert lost["error"] == "OperationalError: server closed <database url>"
    assert (lost["attempt"], lost["delay_s"]) == (1, 1.0)
    assert "s3cret" not in h.stream.getvalue()
    h.connections[1].feed.put_nowait(FakeNotify(REFRESH_CHANNEL))
    await h.settle()
    assert h.calls == 1
    await h.listener.close()


async def test_connect_failures_back_off_and_count_consecutively(h: Harness) -> None:
    h.fail_connect = [psycopg.OperationalError("refused"), psycopg.InterfaceError("refused")]
    h.listener.start()
    await h.settle()
    assert h.sleeps == [1.0, 2.0]
    assert len(h.connections) == 1
    assert h.listener.reconnects == 0
    assert [line["attempt"] for line in h.logged("db_listen_lost")] == [1, 2]
    await h.listener.close()


async def test_a_raising_callback_is_logged_and_listening_continues(h: Harness) -> None:
    def explode() -> None:
        raise RuntimeError("bug")

    h.listener._on_notify = explode
    h.listener.start()
    await h.settle()
    h.connections[0].feed.put_nowait(FakeNotify(REFRESH_CHANNEL))
    h.connections[0].feed.put_nowait(FakeNotify(REFRESH_CHANNEL))
    await h.settle()
    assert h.listener.notified == 2
    assert len(h.logged("db_refresh_callback_failed")) == 2
    await h.listener.close()


async def test_close_is_idempotent_and_a_noop_before_start(h: Harness) -> None:
    await h.listener.close()
    assert h.logged("db_listen_closed") == []
    h.listener.start()
    with pytest.raises(RuntimeError, match="already started"):
        h.listener.start()
    await h.settle()
    await h.listener.close()
    await h.listener.close()
    assert len(h.logged("db_listen_closed")) == 1
    assert h.listener._task is not None and h.listener._task.cancelled()


# --- against a real server -------------------------------------------------------------------


async def test_a_real_notify_reaches_the_callback(db_url: str) -> None:
    await migrate(db_url)
    received = asyncio.Event()
    listener = RefreshListener(db_url, received.set)
    listener.start()
    try:
        for _ in range(50):  # wait for LISTEN to be in place
            if listener._conn is not None and listener._conn.pgconn.status == 0:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        await Database(db_url).notify_refresh()
        await asyncio.wait_for(received.wait(), timeout=2.0)
        assert listener.notified == 1
        conn = await connect(db_url)
        try:
            await conn.execute(f"NOTIFY {REFRESH_CHANNEL}")
        finally:
            await conn.close()
        for _ in range(100):
            if listener.notified == 2:
                break
            await asyncio.sleep(0.02)
        assert listener.notified == 2
    finally:
        await listener.close()
```


Create `tests/test_db_queries.py` with the Write tool:

```python
"""Tests for the query module against a seeded database (skipped without DATABASE_URL)."""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from issuebot.config import GitHubLabels
from issuebot.db import StoreError, migrate
from issuebot.db.database import Database
from issuebot.db.queries import COMPLETE_LIMIT, DailyPoint, EventRow, IssueRow, RunRow
from issuebot.db.store import IssueSnapshot, PostgresStore
from issuebot.events import Blocked, RunEnded, RunStarted
from issuebot.github import Issue, StateLabel

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


@pytest.fixture
async def seeded(db_url: str, make_issue: Callable[..., Issue]) -> AsyncIterator[Database]:
    """Issues closed 1 h, 3 d and 10 d ago; a cancelled one; open issues; runs; events."""
    await migrate(db_url)
    store = PostgresStore(db_url, labels=GitHubLabels())
    await store.connect()

    def issue(number: int, state: StateLabel | None, **overrides: Any) -> Issue:
        fields: dict[str, Any] = {
            "number": number,
            "identifier": f"repo-{number}",
            "title": f"Issue {number}",
            "state": state,
            "state_labels": (f"issuebot/{state.value}",) if state else (),
            "labels": (f"issuebot/{state.value}",) if state else (),
            "updated_at": NOW - number * HOUR,
        }
        fields.update(overrides)
        return make_issue(**fields)

    def closed(
        number: int, ago: timedelta, state: StateLabel | None = StateLabel.COMPLETE
    ) -> Issue:
        return issue(number, state, github_state="closed", closed_at=NOW - ago)

    issues = [
        issue(1, StateLabel.TODO),
        issue(2, StateLabel.IN_PROGRESS),
        issue(3, StateLabel.REVIEW),
        issue(4, StateLabel.TODO),
        issue(5, None),  # unlabelled: on no board column
        closed(10, HOUR),
        closed(11, 3 * DAY),
        closed(12, 10 * DAY),
        closed(13, HOUR, state=StateLabel.REVIEW),  # closed, awaiting the sweep: not counted
        closed(14, HOUR, state=None),  # cancelled: not counted
    ]
    await store.upsert_issues([IssueSnapshot(issue=i, seen_at=NOW) for i in issues])

    def started(run_id: str, number: int, ago: timedelta) -> RunStarted:
        return RunStarted(
            issue_number=number,
            issue_identifier=f"repo-{number}",
            run_id=run_id,
            attempt=1,
            session_id="s",
            workspace_path="/w",
            at=NOW - ago,
        )

    await store.apply_event(started("r1", 2, HOUR))
    await store.apply_event(started("r2", 2, 2 * DAY))
    await store.apply_event(started("r3", 10, 10 * DAY))
    await store.apply_event(
        RunEnded(
            issue_number=2,
            issue_identifier="repo-2",
            run_id="r2",
            outcome="failed",
            error="turn_failed: x",
            turns=1,
            input_tokens=10,
            output_tokens=1,
            cost_usd=0.1,
            duration_s=30.0,
            at=NOW - 2 * DAY + timedelta(seconds=30),
        )
    )
    await store.apply_event(
        Blocked(issue_number=2, issue_identifier="repo-2", reason="stuck", at=NOW - HOUR)
    )
    await store.close()
    yield Database(db_url)


async def test_counts_by_window(seeded: Database) -> None:
    async with seeded.queries() as q:
        assert await q.closed_count(DAY) == 1
        assert await q.closed_count(7 * DAY) == 2
        assert await q.runs_count(DAY) == 1
        assert await q.runs_count(7 * DAY) == 2
        assert await q.runs_count(30 * DAY) == 3


async def test_daily_series_zero_fills_and_ends_today(seeded: Database) -> None:
    async with seeded.queries() as q:
        series = await q.daily_series(4)
    assert len(series) == 4
    assert all(isinstance(point, DailyPoint) for point in series)
    today = NOW.date()
    assert [point.day for point in series] == [today - timedelta(days=n) for n in (3, 2, 1, 0)]
    by_day = {point.day: point for point in series}
    assert (by_day[today].closed, by_day[today].runs) == (1, 1)
    three_days_ago = (NOW - 3 * DAY).date()
    assert (by_day[three_days_ago].closed, by_day[three_days_ago].runs) == (1, 0)
    two_days_ago = (NOW - 2 * DAY).date()
    assert (by_day[two_days_ago].closed, by_day[two_days_ago].runs) == (0, 1)


async def test_issues_by_state_groups_every_role(seeded: Database) -> None:
    async with seeded.queries() as q:
        groups = await q.issues_by_state()
    assert list(groups) == ["todo", "in_progress", "review", "rework", "complete"]
    assert [row.number for row in groups["todo"]] == [1, 4]  # updated_at desc
    assert [row.number for row in groups["in_progress"]] == [2]
    assert [row.number for row in groups["review"]] == [3]
    assert groups["rework"] == []
    assert [row.number for row in groups["complete"]] == [10, 11, 12]  # closed_at desc
    row = groups["todo"][0]
    assert isinstance(row, IssueRow)
    assert (row.identifier, row.title, row.state_label) == ("repo-1", "Issue 1", "issuebot/todo")
    assert row.labels == ["issuebot/todo"]
    assert row.seen_at == NOW


async def test_issues_by_state_caps_the_complete_column(
    db_url: str, make_issue: Callable[..., Issue]
) -> None:
    await migrate(db_url)
    store = PostgresStore(db_url, labels=GitHubLabels())
    await store.connect()
    snapshots = [
        IssueSnapshot(
            issue=make_issue(
                number=n,
                identifier=f"repo-{n}",
                state=StateLabel.COMPLETE,
                github_state="closed",
                closed_at=NOW - n * HOUR,
            ),
            seen_at=NOW,
        )
        for n in range(1, COMPLETE_LIMIT + 6)
    ]
    await store.upsert_issues(snapshots)
    await store.close()
    async with Database(db_url).queries() as q:
        groups = await q.issues_by_state()
    assert len(groups["complete"]) == COMPLETE_LIMIT
    assert groups["complete"][0].number == 1


async def test_runs_for_issue_newest_first(seeded: Database) -> None:
    async with seeded.queries() as q:
        runs = await q.runs_for_issue(2)
        assert await q.runs_for_issue(99) == []
    assert [run.run_id for run in runs] == ["r1", "r2"]
    assert all(isinstance(run, RunRow) for run in runs)
    assert (runs[0].outcome, runs[0].ended_at) == (None, None)
    assert (runs[1].outcome, runs[1].error, runs[1].cost_usd) == ("failed", "turn_failed: x", 0.1)
    assert runs[1].ended_at == runs[1].started_at + timedelta(seconds=30)


async def test_recent_events_newest_first_and_limited(seeded: Database) -> None:
    async with seeded.queries() as q:
        events = await q.recent_events(3)
        everything = await q.recent_events(100)
    assert [event.kind for event in events] == ["blocked", "run_ended", "run_started"]
    assert len(everything) == 5
    assert all(isinstance(event, EventRow) for event in events)
    assert events[0].payload["reason"] == "stuck"
    assert (events[0].issue_number, events[0].run_id) == (2, None)
    assert events[1].run_id == "r2"


async def test_snapshot_is_none_until_written(seeded: Database, db_url: str) -> None:
    async with seeded.queries() as q:
        assert await q.snapshot() is None
    store = PostgresStore(db_url, labels=GitHubLabels())
    await store.connect()
    await store.write_snapshot(NOW, {"tick_count": 3})
    await store.close()
    async with seeded.queries() as q:
        row = await q.snapshot()
    assert row is not None
    assert (row.at, row.data) == (NOW, {"tick_count": 3})
    assert row.written_at >= NOW


async def test_queries_on_an_empty_schema_report_a_database_error(db_url: str) -> None:
    with pytest.raises(StoreError, match="UndefinedTable"):
        async with Database(db_url).queries() as q:
            await q.snapshot()
```


Create `tests/test_db_database.py` with the Write tool:

```python
"""Tests for the Database facade: hermetic where possible, otherwise against DATABASE_URL."""

from typing import Any

import psycopg
import pytest

from issuebot.config import GitHubLabels
from issuebot.db import StoreUnavailableError
from issuebot.db.database import Database, Probe
from issuebot.db.listen import RefreshListener
from issuebot.db.store import PostgresStore

URL = "postgresql://issuebot:s3cret@db.example:5433/issuebot"


async def refuse(url: str) -> Any:
    raise psycopg.OperationalError(f"connection to {url} failed: refused")


def test_description_hides_the_password() -> None:
    assert Database(URL).description == "postgresql://issuebot@db.example:5433/issuebot"


def test_store_and_listener_are_built_with_the_url() -> None:
    database = Database(URL, connect=refuse)
    store = database.store(GitHubLabels())
    assert isinstance(store, PostgresStore)
    assert store._url == URL
    listener = database.listener(lambda: None)
    assert isinstance(listener, RefreshListener)
    assert listener._url == URL


async def test_probe_reports_an_unreachable_server_without_the_url() -> None:
    with pytest.raises(StoreUnavailableError, match="cannot connect") as exc:
        await Database(URL, connect=refuse).probe()
    assert "s3cret" not in exc.value.message
    assert "<database url>" in exc.value.message


async def test_queries_and_notify_report_an_unreachable_server() -> None:
    database = Database(URL, connect=refuse)
    with pytest.raises(StoreUnavailableError, match="cannot connect"):
        async with database.queries():
            pass
    with pytest.raises(StoreUnavailableError, match="cannot connect"):
        await database.notify_refresh()


def test_probe_flags() -> None:
    assert Probe(server_version="PostgreSQL 18.1", schema_version=0, latest_version=1).behind
    assert Probe(server_version="PostgreSQL 18.1", schema_version=2, latest_version=1).ahead
    current = Probe(server_version="PostgreSQL 18.1", schema_version=1, latest_version=1)
    assert not current.behind and not current.ahead


# --- against a real server -------------------------------------------------------------------


async def test_probe_before_and_after_migrate(db_url: str) -> None:
    database = Database(db_url)
    before = await database.probe()
    assert before.server_version.startswith("PostgreSQL ")
    assert (before.schema_version, before.latest_version, before.behind) == (0, 1, True)
    result = await database.migrate()
    assert result.applied == ("0001_initial",)
    after = await database.probe()
    assert (after.schema_version, after.behind, after.ahead) == (1, False, False)
```


- [ ] **Step 2: Run the tests to verify they fail**

Run: `DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 120 uv run pytest -q tests/test_db_listen.py tests/test_db_queries.py tests/test_db_database.py`
Expected: `3 errors` during collection, each `ModuleNotFoundError: No module named 'issuebot.db.database'` (the first missing import in every file).

- [ ] **Step 3: The listener, the queries and the facade**


Create `src/issuebot/db/listen.py` with the Write tool:

```python
"""LISTEN issuebot_refresh: one connection, one task, a callback per notification."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import psycopg
from psycopg import AsyncConnection

from issuebot.db.connection import Connector, connect, error_text, reconnect_delay, redact
from issuebot.log import get_logger

REFRESH_CHANNEL = "issuebot_refresh"
_LOST = (psycopg.OperationalError, psycopg.InterfaceError)


class RefreshListener:
    """Calls ``on_notify`` for every NOTIFY on the refresh channel; reconnects with backoff."""

    def __init__(
        self,
        url: str,
        on_notify: Callable[[], None],
        *,
        connect: Connector = connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = url
        self._on_notify = on_notify
        self._connect = connect
        self._sleep = sleep
        self._conn: AsyncConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._connected_once = False
        self.notified = 0
        self.reconnects = 0
        self._log = get_logger(__name__)

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("RefreshListener is already started")
        self._task = asyncio.create_task(self._run(), name="issuebot-refresh-listener")

    async def close(self) -> None:
        """Stop listening and close the connection; idempotent, a no-op before ``start``."""
        if self._task is None or self._closed:
            return
        self._closed = True
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        await self._close_connection()
        self._log.info("db_listen_closed", notified=self.notified, reconnects=self.reconnects)

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                conn = await self._connect(self._url)
                self._conn = conn
                await conn.execute(f"LISTEN {REFRESH_CHANNEL}")
            except _LOST as exc:
                failures += 1
                await self._lost(exc, failures)
                continue
            failures = 0
            if self._connected_once:
                self.reconnects += 1
            self._connected_once = True
            self._log.info("db_listen_started", channel=REFRESH_CHANNEL, reconnects=self.reconnects)
            try:
                async for notification in conn.notifies():
                    self.notified += 1
                    self._log.info("db_refresh_received", channel=notification.channel)
                    try:
                        self._on_notify()
                    except Exception:
                        self._log.exception("db_refresh_callback_failed")
            except _LOST as exc:
                failures = 1
                await self._lost(exc, failures)

    async def _lost(self, exc: Exception, failures: int) -> None:
        await self._close_connection()
        delay = reconnect_delay(failures)
        self._log.warning(
            "db_listen_lost",
            error=redact(f"{type(exc).__name__}: {error_text(exc)}", self._url),
            attempt=failures,
            delay_s=delay,
        )
        await self._sleep(delay)

    async def _close_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(psycopg.Error):
                await conn.close()
```


Create `src/issuebot/db/queries.py` with the Write tool:

```python
"""The reads: view models (Phase 7's) and a Queries object bound to one connection."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from issuebot.github import StateLabel

COMPLETE_LIMIT = 50


@dataclass(frozen=True, kw_only=True, slots=True)
class IssueRow:
    number: int
    identifier: str
    title: str
    state: str | None
    state_label: str | None
    github_state: str
    url: str
    labels: list[str]
    pr_number: int | None
    pr_url: str | None
    pr_state: str | None
    pr_merged_at: datetime | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    seen_at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class RunRow:
    run_id: str
    issue_number: int
    issue_identifier: str
    attempt: int
    session_id: str | None
    started_at: datetime
    ended_at: datetime | None
    outcome: str | None
    error: str | None
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_s: float | None
    workspace_path: str | None
    log_dir: str | None


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


CLOSED_COUNT = """
SELECT count(*) AS n FROM issues
WHERE state = 'complete' AND closed_at >= now() - %(window)s
"""

RUNS_COUNT = "SELECT count(*) AS n FROM runs WHERE started_at >= now() - %(window)s"

DAILY_SERIES = """
WITH days AS (
    SELECT generate_series(
        date_trunc('day', now()) - (%(days)s - 1) * interval '1 day',
        date_trunc('day', now()),
        interval '1 day'
    ) AS day
)
SELECT day::date AS day,
       (SELECT count(*) FROM issues
        WHERE state = 'complete' AND closed_at >= day AND closed_at < day + interval '1 day')
           AS closed,
       (SELECT count(*) FROM runs
        WHERE started_at >= day AND started_at < day + interval '1 day') AS runs
FROM days ORDER BY day
"""

OPEN_ISSUES = """
SELECT * FROM issues WHERE github_state = 'open' AND state IS NOT NULL
ORDER BY updated_at DESC, number DESC
"""

COMPLETE_ISSUES = """
SELECT * FROM issues WHERE github_state = 'closed' AND state = 'complete'
ORDER BY closed_at DESC NULLS LAST, number DESC LIMIT %(limit)s
"""

RUNS_FOR_ISSUE = """
SELECT * FROM runs WHERE issue_number = %(number)s ORDER BY started_at DESC, run_id DESC
"""

RECENT_EVENTS = "SELECT * FROM events ORDER BY id DESC LIMIT %(limit)s"

SNAPSHOT = "SELECT at, written_at, data FROM runtime_snapshot WHERE id"


class Queries:
    """Read-only queries over one connection; every method is one round trip or two."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def closed_count(self, window: timedelta) -> int:
        """Issues in ``complete`` whose GitHub ``closed_at`` falls inside the window."""
        return await self._count(CLOSED_COUNT, {"window": window})

    async def runs_count(self, window: timedelta) -> int:
        """Worker sessions started inside the window ("agents spun up")."""
        return await self._count(RUNS_COUNT, {"window": window})

    async def daily_series(self, days: int) -> list[DailyPoint]:
        """One point per UTC day for the last ``days`` days, today last, zero-filled."""
        rows = await self._rows(DAILY_SERIES, {"days": days})
        return [DailyPoint(day=row["day"], closed=row["closed"], runs=row["runs"]) for row in rows]

    async def issues_by_state(self) -> dict[str, list[IssueRow]]:
        """Open issues with a state, by StateLabel value, plus the latest complete ones."""
        groups: dict[str, list[IssueRow]] = {role.value: [] for role in StateLabel}
        for row in await self._rows(OPEN_ISSUES):
            groups.setdefault(row["state"], []).append(IssueRow(**row))
        for row in await self._rows(COMPLETE_ISSUES, {"limit": COMPLETE_LIMIT}):
            groups[StateLabel.COMPLETE.value].append(IssueRow(**row))
        return groups

    async def runs_for_issue(self, number: int) -> list[RunRow]:
        """Newest first."""
        return [RunRow(**row) for row in await self._rows(RUNS_FOR_ISSUE, {"number": number})]

    async def recent_events(self, limit: int) -> list[EventRow]:
        """Newest first."""
        return [EventRow(**row) for row in await self._rows(RECENT_EVENTS, {"limit": limit})]

    async def snapshot(self) -> SnapshotRow | None:
        rows = await self._rows(SNAPSHOT)
        return SnapshotRow(**rows[0]) if rows else None

    async def _count(self, query: str, params: dict[str, Any]) -> int:
        rows = await self._rows(query, params)
        return int(rows[0]["n"])

    async def _rows(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchall()
```


Create `src/issuebot/db/database.py` with the Write tool:

```python
"""The Database facade: everything the CLI does with the database, behind one object."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import psycopg
from psycopg import AsyncConnection

from issuebot.config import GitHubLabels
from issuebot.db.connection import Connector, classify, connect, describe, error_text, redact
from issuebot.db.errors import StoreUnavailableError
from issuebot.db.listen import REFRESH_CHANNEL, RefreshListener
from issuebot.db.migrate import MigrationResult, discover_migrations, migrate, schema_version
from issuebot.db.queries import Queries
from issuebot.db.store import PostgresStore


@dataclass(frozen=True, slots=True)
class Probe:
    server_version: str  # "PostgreSQL 18.1"
    schema_version: int  # applied
    latest_version: int  # known to this issuebot

    @property
    def behind(self) -> bool:
        return self.schema_version < self.latest_version

    @property
    def ahead(self) -> bool:
        return self.schema_version > self.latest_version


class Database:
    """One URL; migrate, probe, read, and build the sink's store and the refresh listener."""

    def __init__(self, url: str, *, connect: Connector = connect) -> None:
        self._url = url
        self._connect = connect

    @property
    def description(self) -> str:
        return describe(self._url)

    async def migrate(self) -> MigrationResult:
        return await migrate(self._url, connect=self._connect)

    async def probe(self) -> Probe:
        """Server version and schema version; raises DatabaseError when unreachable."""
        latest = len(discover_migrations())
        async with self._open() as conn:
            row = await (await conn.execute("SELECT version()")).fetchone()
            version = " ".join(str(row[0]).split()[:2]) if row else "PostgreSQL ?"
            applied = await schema_version(conn)
        return Probe(server_version=version, schema_version=applied, latest_version=latest)

    @asynccontextmanager
    async def queries(self) -> AsyncIterator[Queries]:
        """A Queries object on one connection; psycopg errors inside become DatabaseError."""
        async with self._open() as conn:
            yield Queries(conn)

    def store(self, labels: GitHubLabels) -> PostgresStore:
        return PostgresStore(self._url, labels=labels, connect=self._connect)

    def listener(self, on_notify: Callable[[], None]) -> RefreshListener:
        return RefreshListener(self._url, on_notify, connect=self._connect)

    async def notify_refresh(self) -> None:
        """NOTIFY the refresh channel, which makes a listening worker tick at once."""
        async with self._open() as conn:
            await conn.execute(f"NOTIFY {REFRESH_CHANNEL}")

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[AsyncConnection]:
        try:
            conn = await self._connect(self._url)
        except psycopg.Error as exc:
            message = redact(f"cannot connect: {error_text(exc)}", self._url)
            raise StoreUnavailableError(message) from exc
        try:
            yield conn
        except psycopg.Error as exc:
            raise classify(exc, self._url) from exc
        finally:
            await conn.close()
```


Replace the whole of `src/issuebot/db/__init__.py` (the Task 1 version) with the final export list, using the Write tool:

```python
"""Persistence: psycopg 3 connections, numbered SQL migrations, the sink and the queries."""

from issuebot.db.connection import (
    APPLICATION_NAME,
    CONNECT_TIMEOUT_S,
    POSTGRES_SCHEMES,
    RECONNECT_DELAYS_S,
    REDACTED,
    Connector,
    classify,
    connect,
    describe,
    error_text,
    is_postgres_url,
    reconnect_delay,
    redact,
)
from issuebot.db.database import Database, Probe
from issuebot.db.errors import DatabaseError, MigrationError, StoreError, StoreUnavailableError
from issuebot.db.listen import REFRESH_CHANNEL, RefreshListener
from issuebot.db.migrate import (
    ADVISORY_LOCK_KEY,
    MIGRATIONS_ROOT,
    Migration,
    MigrationResult,
    apply_migrations,
    discover_migrations,
    migrate,
    schema_version,
)
from issuebot.db.queries import (
    COMPLETE_LIMIT,
    DailyPoint,
    EventRow,
    IssueRow,
    Queries,
    RunRow,
    SnapshotRow,
)
from issuebot.db.sink import DRAIN_TIMEOUT_S, QUEUE_LIMIT, PostgresSink, SnapshotLike
from issuebot.db.store import IssueSnapshot, PostgresStore, Store

__all__ = [
    "ADVISORY_LOCK_KEY",
    "APPLICATION_NAME",
    "COMPLETE_LIMIT",
    "CONNECT_TIMEOUT_S",
    "DRAIN_TIMEOUT_S",
    "MIGRATIONS_ROOT",
    "POSTGRES_SCHEMES",
    "QUEUE_LIMIT",
    "RECONNECT_DELAYS_S",
    "REDACTED",
    "REFRESH_CHANNEL",
    "Connector",
    "DailyPoint",
    "Database",
    "DatabaseError",
    "EventRow",
    "IssueRow",
    "IssueSnapshot",
    "Migration",
    "MigrationError",
    "MigrationResult",
    "PostgresSink",
    "PostgresStore",
    "Probe",
    "Queries",
    "RefreshListener",
    "RunRow",
    "SnapshotLike",
    "SnapshotRow",
    "Store",
    "StoreError",
    "StoreUnavailableError",
    "apply_migrations",
    "classify",
    "connect",
    "describe",
    "discover_migrations",
    "error_text",
    "is_postgres_url",
    "migrate",
    "reconnect_delay",
    "redact",
    "schema_version",
]
```


- [ ] **Step 4: Run the tests to verify they pass, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && timeout 300 uv run pytest -q`
Expected: lint clean; `629 passed, 30 skipped` (the ten database-backed tests of this task: one listener round trip, eight query tests, one probe test).

Run: `DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q`
Expected: `659 passed`. `test_a_real_notify_reaches_the_callback` is the proof that `LISTEN`/`NOTIFY` works end to end on the compose server.

- [ ] **Step 5: Commit**

```bash
git add --all
git commit -m "feat: refresh listener, the query module and the Database facade"
```

---

### Task 6: CLI: `migrate`, `status`, `stats`, `refresh`, the `database.url` check, worker and run-once wiring

**Files:**
- Modify: `src/issuebot/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 5's `Database`/`Probe`/`Queries`/row types, Task 4's `PostgresSink`, Task 1's `is_postgres_url`, `DatabaseError`; Task 2's `on_snapshot`/`on_issues` and `request_refresh`.
- Produces: `cli._database_factory: Callable[[str], Database] = Database` (the test seam); `_database_check(settings) -> Check`; `_Sinks(bus, slack, postgres, database)` with `start()`, `async close()`, `record_issues(issues)`; `async _open_database(settings) -> Database | None` (migrates; raises `DatabaseError`); `async _build_sinks(settings) -> _Sinks`; `_database_or_report(settings) -> Database | None`; `cmd_migrate`, `cmd_status`, `cmd_stats`, `cmd_refresh`; `StatsView`; `render_status(row, *, now) -> str`; `render_stats(view) -> str`; `_claim_and_run(..., record=None)`.

Spec: §9.

- [ ] **Step 1: Write the failing tests**


In `tests/test_cli.py` replace

```
import asyncio
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from issuebot import __version__
from issuebot.agent import RunResult, SessionRecord, WorkspaceManager
from issuebot.cli import main, not_runnable, render_issue_table, render_run_summary
from issuebot.config import GitHubSettings, Settings
from issuebot.events import Event, StateChanged
from issuebot.github import FakeGitHub, GitHubError, Issue, LinkedPr, StateLabel
from issuebot.notifications import PostResult
from issuebot.orchestrator import OrchestratorStartupError
```

with

```
import asyncio
import json
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from issuebot import __version__
from issuebot.agent import RunResult, SessionRecord, WorkspaceManager
from issuebot.cli import (
    StatsView,
    main,
    not_runnable,
    render_issue_table,
    render_run_summary,
    render_stats,
    render_status,
)
from issuebot.config import GitHubLabels, GitHubSettings, Settings
from issuebot.db import DatabaseError, MigrationResult, Probe, StoreError, StoreUnavailableError
from issuebot.db.queries import DailyPoint, SnapshotRow
from issuebot.db.store import IssueSnapshot
from issuebot.events import Event, StateChanged
from issuebot.github import FakeGitHub, GitHubError, Issue, LinkedPr, StateLabel
from issuebot.notifications import PostResult
from issuebot.orchestrator import OrchestratorStartupError
```

In `tests/test_cli.py` replace

```
@pytest.fixture
def slack_post(monkeypatch: pytest.MonkeyPatch) -> FakeSlackPost:
    fake = FakeSlackPost()
    monkeypatch.setattr("issuebot.cli._slack_post", fake)
    return fake
```

with

```
@pytest.fixture
def slack_post(monkeypatch: pytest.MonkeyPatch) -> FakeSlackPost:
    fake = FakeSlackPost()
    monkeypatch.setattr("issuebot.cli._slack_post", fake)
    return fake


DB_URL = "postgresql://issuebot:s3cret@db.example:5432/issuebot"
PROBE_OK = Probe(server_version="PostgreSQL 18.1", schema_version=1, latest_version=1)


class FakeStore:
    """The sink's store: records every write."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.issues: list[list[IssueSnapshot]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    async def apply_event(self, event: Event) -> None:
        self.events.append(event)

    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None:
        self.issues.append(list(issues))

    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None:
        self.snapshots.append(dict(data))


class FakeQueries:
    """Canned answers for status and stats."""

    def __init__(self) -> None:
        self.snapshot_row: SnapshotRow | None = None
        self.closed = {1: 0, 7: 0}
        self.runs = {1: 0, 7: 0}
        self.groups: dict[str, list[object]] = {role.value: [] for role in StateLabel}
        self.series: list[DailyPoint] = []
        self.error: DatabaseError | None = None
        self.days_asked: int | None = None

    def _check(self) -> None:
        if self.error is not None:
            raise self.error

    async def snapshot(self) -> SnapshotRow | None:
        self._check()
        return self.snapshot_row

    async def closed_count(self, window: timedelta) -> int:
        self._check()
        return self.closed[window.days]

    async def runs_count(self, window: timedelta) -> int:
        return self.runs[window.days]

    async def issues_by_state(self) -> dict[str, list[object]]:
        self._check()
        return self.groups

    async def daily_series(self, days: int) -> list[DailyPoint]:
        self.days_asked = days
        return self.series


class FakeListener:
    def __init__(self, on_notify: Callable[[], None]) -> None:
        self.on_notify = on_notify
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True


class FakeDatabase:
    """Stands in for issuebot.db.Database: one instance per test with canned results."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.migrations = 0
        self.migrate_result = MigrationResult(applied=(), version=1)
        self.migrate_error: DatabaseError | None = None
        self.probe_result = PROBE_OK
        self.probe_error: DatabaseError | None = None
        self.queries_obj = FakeQueries()
        self.store_obj = FakeStore()
        self.labels: GitHubLabels | None = None
        self.listeners: list[FakeListener] = []
        self.notified = 0
        self.notify_error: DatabaseError | None = None

    def factory(self, url: str) -> FakeDatabase:
        self.urls.append(url)
        return self

    @property
    def description(self) -> str:
        return "postgresql://issuebot@db.example:5432/issuebot"

    async def migrate(self) -> MigrationResult:
        self.migrations += 1
        if self.migrate_error is not None:
            raise self.migrate_error
        return self.migrate_result

    async def probe(self) -> Probe:
        if self.probe_error is not None:
            raise self.probe_error
        return self.probe_result

    @asynccontextmanager
    async def queries(self) -> AsyncIterator[FakeQueries]:
        yield self.queries_obj

    def store(self, labels: GitHubLabels) -> FakeStore:
        self.labels = labels
        return self.store_obj

    def listener(self, on_notify: Callable[[], None]) -> FakeListener:
        listener = FakeListener(on_notify)
        self.listeners.append(listener)
        return listener

    async def notify_refresh(self) -> None:
        if self.notify_error is not None:
            raise self.notify_error
        self.notified += 1


@pytest.fixture(autouse=True)
def fake_database(monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    """Every CLI command talks to this stand-in instead of a real PostgreSQL server."""
    fake = FakeDatabase()
    monkeypatch.setattr("issuebot.cli._database_factory", fake.factory)
    return fake
```

In `tests/test_cli.py` replace

```
    assert main(["validate", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] database.url: configured" in out
    assert (
        "[WARN] notifications.slack: configured (blocked, state_changed); the URL is not a "
        "hooks.slack.com/services/ webhook (a compatible endpoint is fine)" in out
    )
    assert "hooks.example" not in out
```

with

```
    assert main(["validate", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] database.url: connected (PostgreSQL 18.1); schema version 1" in out
    assert (
        "[WARN] notifications.slack: configured (blocked, state_changed); the URL is not a "
        "hooks.slack.com/services/ webhook (a compatible endpoint is fine)" in out
    )
    assert "hooks.example" not in out
    assert "12 checks: 0 failed, 1 warnings" in out


def _validate_with_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str = DB_URL
) -> int:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", url)
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\nnotifications:\n  slack:\n    events: []\n---\nBody")
    return main(["validate", "--workflow", str(path)])


def test_validate_rejects_a_non_postgres_database_url(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    fake_database: FakeDatabase,
) -> None:
    assert _validate_with_database(tmp_path, monkeypatch, "mysql://u:p@h/db") == 1
    out = capsys.readouterr().out
    assert "[FAIL] database.url: not a postgresql:// URL" in out
    assert "12 checks: 1 failed, 0 warnings" in out
    assert fake_database.urls == []


def test_validate_reports_an_unreachable_database_without_the_url(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    fake_database: FakeDatabase,
) -> None:
    fake_database.probe_error = StoreUnavailableError(
        "cannot connect: connection to server at <database url> failed"
    )
    assert _validate_with_database(tmp_path, monkeypatch) == 1
    out = capsys.readouterr().out
    assert (
        "[FAIL] database.url: cannot connect: connection to server at <database url> failed"
        in out
    )
    assert "s3cret" not in out
    assert fake_database.urls == [DB_URL]


def test_validate_warns_when_the_schema_is_behind(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    fake_database: FakeDatabase,
) -> None:
    fake_database.probe_result = Probe(
        server_version="PostgreSQL 18.1", schema_version=0, latest_version=1
    )
    assert _validate_with_database(tmp_path, monkeypatch) == 0
    out = capsys.readouterr().out
    assert (
        "[WARN] database.url: connected (PostgreSQL 18.1); schema version 0 of 1; "
        "run issuebot migrate" in out
    )
    assert "12 checks: 0 failed, 1 warnings" in out


def test_validate_fails_when_the_schema_is_ahead(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    fake_database: FakeDatabase,
) -> None:
    fake_database.probe_result = Probe(
        server_version="PostgreSQL 18.1", schema_version=2, latest_version=1
    )
    assert _validate_with_database(tmp_path, monkeypatch) == 1
    out = capsys.readouterr().out
    assert (
        "[FAIL] database.url: connected (PostgreSQL 18.1); schema version 2 is newer than "
        "this issuebot knows (1)" in out
    )
```

In `tests/test_cli.py` replace

```
        self.workflow = workflow
        self.kwargs = kwargs
        self.stops = 0
        StubOrchestrator.instances.append(self)
```

with

```
        self.workflow = workflow
        self.kwargs = kwargs
        self.stops = 0
        self.refreshes = 0
        StubOrchestrator.instances.append(self)
```

In `tests/test_cli.py` replace

```
    def request_stop(self) -> None:
        self.stops += 1

    async def run(self) -> None:
        if StubOrchestrator.next_problems is not None:
```

with

```
    def request_stop(self) -> None:
        self.stops += 1

    def request_refresh(self) -> None:
        self.refreshes += 1

    async def run(self) -> None:
        if StubOrchestrator.next_problems is not None:
```

Append to `tests/test_cli.py`:

```
# --- migrate, status, stats, refresh ---------------------------------------------------------


def _db_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, url: str | None = DB_URL) -> Path:
    if url is not None:
        monkeypatch.setenv("DATABASE_URL", url)
    return _write(tmp_path, "---\ngithub:\n  repo: example/repo\n---\nBody")


@pytest.mark.parametrize("command", [["migrate"], ["status"], ["stats"], ["refresh"]])
def test_database_commands_need_a_configured_url(
    command: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    path = _db_workflow(tmp_path, monkeypatch, url=None)
    assert main([*command, "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == (
        "[FAIL] database: not configured; export DATABASE_URL or set database.url: $VAR\n"
    )
    assert fake_database.urls == []


@pytest.mark.parametrize("command", [["migrate"], ["status"], ["stats"], ["refresh"]])
def test_database_commands_exit_two_on_an_unloadable_workflow(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*command, "--workflow", str(INVALID)]) == 2
    assert "[FAIL] workflow:" in capsys.readouterr().out


def test_migrate_reports_what_it_applied(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    fake_database.migrate_result = MigrationResult(applied=("0001_initial",), version=1)
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["migrate", "--workflow", str(path)]) == 0
    assert capsys.readouterr().out == (
        "[ OK ] migration 0001_initial: applied\n[ OK ] database: schema version 1\n"
    )
    assert fake_database.urls == [DB_URL]
    assert fake_database.migrations == 1


def test_migrate_reports_nothing_to_do_and_failures(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["migrate", "--workflow", str(path)]) == 0
    assert capsys.readouterr().out == "[ OK ] database: unchanged at schema version 1\n"
    fake_database.migrate_error = StoreUnavailableError("cannot connect: refused")
    assert main(["migrate", "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"


SNAPSHOT_AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
SNAPSHOT_DATA: dict[str, Any] = {
    "at": SNAPSHOT_AT.isoformat(),
    "workflow_path": "/app/WORKFLOW.md",
    "workflow_mtime_ns": 1,
    "config_valid": True,
    "config_error": None,
    "poll_interval_ms": 30000,
    "max_concurrent_agents": 2,
    "tick_count": 42,
    "last_tick_at": "2026-09-04T11:59:58+00:00",
    "running": [
        {
            "issue_number": 7,
            "identifier": "repo-7",
            "title": "Seven",
            "url": "https://github.com/example/repo/issues/7",
            "state": "in_progress",
            "attempt": 2,
            "rework": False,
            "resumed": True,
            "run_id": "20260904T115000Z-abc123",
            "session_id": "s",
            "started_at": "2026-09-04T11:50:00+00:00",
            "last_activity_at": "2026-09-04T11:59:00+00:00",
            "last_event": "turn_activity:Edit",
            "turns": 1,
            "stop_cause": None,
        }
    ],
    "retrying": [
        {
            "issue_number": 9,
            "identifier": "repo-9",
            "url": "https://github.com/example/repo/issues/9",
            "attempt": 3,
            "kind": "failure",
            "due_at": "2026-09-04T12:00:40+00:00",
            "error": "turn_failed: boom",
        }
    ],
    "totals": {
        "input_tokens": 1000,
        "output_tokens": 234,
        "cost_usd": 1.2345,
        "seconds_running": 321.4,
        "total_tokens": 1234,
    },
    "counters": {
        "runs_started": 3,
        "runs_ended": 2,
        "issues_completed": 1,
        "issues_cancelled": 0,
        "blocked": 1,
    },
}


def test_render_status_lists_running_and_retrying_entries() -> None:
    row = SnapshotRow(
        at=SNAPSHOT_AT, written_at=SNAPSHOT_AT + timedelta(seconds=1), data=SNAPSHOT_DATA
    )
    text = render_status(row, now=SNAPSHOT_AT + timedelta(seconds=13))
    assert text.splitlines() == [
        "snapshot: 2026-09-04T12:00:00Z (written 2026-09-04T12:00:01Z, 12 s ago)",
        "workflow: /app/WORKFLOW.md (config valid)",
        "tick 42, last tick 2026-09-04T11:59:58Z, poll 30000 ms, 2 slots",
        "running: 1",
        "  NUMBER  ATTEMPT  TURNS  RUN_ID                   LAST_EVENT          "
        "STARTED               IDENTIFIER",
        "  7       2        1      20260904T115000Z-abc123  turn_activity:Edit  "
        "2026-09-04T11:50:00Z  repo-7",
        "retrying: 1",
        "  NUMBER  KIND     ATTEMPT  DUE                   ERROR",
        "  9       failure  3        2026-09-04T12:00:40Z  turn_failed: boom",
        "totals: 3 runs started, 2 ended, 1 completed, 0 cancelled, 1 blocked; 1234 tokens, "
        "$1.23, 321 s running",
    ]


def test_render_status_copes_with_an_empty_or_broken_snapshot() -> None:
    row = SnapshotRow(at=SNAPSHOT_AT, written_at=SNAPSHOT_AT, data={"config_error": "bad yaml"})
    text = render_status(row, now=SNAPSHOT_AT - timedelta(seconds=5))
    assert text.splitlines() == [
        "snapshot: 2026-09-04T12:00:00Z (written 2026-09-04T12:00:00Z, 0 s ago)",
        "workflow: None (config error: bad yaml)",
        "tick None, last tick -, poll None ms, None slots",
        "running: 0",
        "retrying: 0",
        "totals: 0 runs started, 0 ended, 0 completed, 0 cancelled, 0 blocked; 0 tokens, $0.00, "
        "0 s running",
    ]


def test_status_prints_the_snapshot_or_says_there_is_none(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["status", "--workflow", str(path)]) == 0
    assert capsys.readouterr().out == (
        "no runtime snapshot yet (has the worker run against this database?)\n"
    )
    fake_database.queries_obj.snapshot_row = SnapshotRow(
        at=SNAPSHOT_AT, written_at=SNAPSHOT_AT, data=SNAPSHOT_DATA
    )
    assert main(["status", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("snapshot: 2026-09-04T12:00:00Z (written 2026-09-04T12:00:00Z, ")
    assert "running: 1" in out
    fake_database.queries_obj.error = StoreError("UndefinedTable: relation does not exist")
    assert main(["status", "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == "[FAIL] database: UndefinedTable: relation does not exist\n"


def test_render_stats() -> None:
    view = StatsView(
        closed_1d=1,
        closed_7d=12,
        runs_1d=3,
        runs_7d=45,
        by_state={"todo": 2, "in_progress": 1, "review": 0, "rework": 0, "complete": 12},
        series=[
            DailyPoint(day=date(2026, 9, 3), closed=11, runs=42),
            DailyPoint(day=date(2026, 9, 4), closed=1, runs=3),
        ],
    )
    assert render_stats(view).splitlines() == [
        "WINDOW  CLOSED  RUNS",
        "1d      1       3",
        "7d      12      45",
        "issues: todo 2, in_progress 1, review 0, rework 0, complete 12",
        "",
        "DAY         CLOSED  RUNS",
        "2026-09-03  11      42",
        "2026-09-04  1       3",
    ]


def test_stats_prints_the_windows_and_the_series(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    queries = fake_database.queries_obj
    queries.closed = {1: 1, 7: 2}
    queries.runs = {1: 3, 7: 4}
    queries.groups["review"] = [object()]
    queries.series = [DailyPoint(day=date(2026, 9, 4), closed=1, runs=3)]
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["stats", "--workflow", str(path), "--days", "3"]) == 0
    out = capsys.readouterr().out
    assert "1d      1       3" in out
    assert "7d      2       4" in out
    assert "issues: todo 0, in_progress 0, review 1, rework 0, complete 0" in out
    assert out.endswith("DAY         CLOSED  RUNS\n2026-09-04  1       3\n")
    assert queries.days_asked == 3
    assert main(["stats", "--workflow", str(path), "--days", "0"]) == 1
    assert capsys.readouterr().out == "[FAIL] stats: --days must be at least 1\n"
    queries.error = StoreUnavailableError("cannot connect: refused")
    assert main(["stats", "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"


def test_refresh_notifies_and_reports_failures(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["refresh", "--workflow", str(path)]) == 0
    assert capsys.readouterr().out == "[ OK ] refresh: notified issuebot_refresh\n"
    assert fake_database.notified == 1
    fake_database.notify_error = StoreUnavailableError("cannot connect: refused")
    assert main(["refresh", "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"


# --- the database in run-once and worker ------------------------------------------------------


def test_run_once_records_the_issue_and_the_claim_in_the_database(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert "issue #42 is now review" in capsys.readouterr().out
    assert fake_database.migrations == 1
    assert fake_database.labels == GitHubLabels()
    store = fake_database.store_obj
    assert [event.kind for event in store.events] == ["state_changed"]  # the stub session is silent
    assert [snapshot.issue.state for snapshot in store.issues[-1]] == [StateLabel.IN_PROGRESS]
    assert store.closed


def test_run_once_fails_before_running_when_migration_fails(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_database.migrate_error = StoreUnavailableError("cannot connect: refused")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"
    assert stub_session.calls == []
    assert fake_github.issue(42).state is StateLabel.TODO


def test_worker_wires_the_database_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    stub_orchestrator.next_event = StateChanged(
        issue_number=7,
        issue_identifier="repo-7",
        from_label="issuebot/in-progress",
        to_label="issuebot/review",
        actor="agent",
    )
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert fake_database.migrations == 1
    instance = stub_orchestrator.instances[0]
    kwargs = instance.kwargs
    assert [sink.name for sink in kwargs["bus"].sinks] == ["log", "postgres"]  # type: ignore[attr-defined]
    (postgres,) = [sink for sink in kwargs["bus"].sinks if sink.name == "postgres"]  # type: ignore[attr-defined]
    assert kwargs["on_snapshot"] == postgres.record_snapshot
    assert kwargs["on_issues"] == postgres.record_issues
    (listener,) = fake_database.listeners
    assert listener.on_notify == instance.request_refresh
    assert listener.started and listener.closed
    store = fake_database.store_obj
    assert [event.kind for event in store.events] == ["state_changed"]
    assert store.closed


def test_worker_without_a_database_passes_no_callbacks(
    tmp_path: Path,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    kwargs = stub_orchestrator.instances[0].kwargs
    assert (kwargs["on_snapshot"], kwargs["on_issues"]) == (None, None)
    assert fake_database.urls == []
    assert fake_database.listeners == []


def test_worker_fails_before_the_orchestrator_when_migration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_database.migrate_error = StoreUnavailableError("cannot connect: refused")
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"
    assert stub_orchestrator.instances == []
    assert fake_database.listeners == []
```


- [ ] **Step 2: Run the tests to verify they fail**

Run: `timeout 120 uv run pytest -q tests/test_cli.py`
Expected: `1 error` during collection: `ImportError: cannot import name 'StatsView' from 'issuebot.cli'`.

- [ ] **Step 3: The CLI**


In `src/issuebot/cli.py` replace

```
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
```

with

```
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
```

In `src/issuebot/cli.py` replace

```
from issuebot.config.resolve import ENV_REF
from issuebot.events import EventBus, EventSink, LogSink, StateChanged
```

with

```
from issuebot.config.resolve import ENV_REF
from issuebot.db import Database, DatabaseError, PostgresSink, RefreshListener, is_postgres_url
from issuebot.db.queries import DailyPoint, SnapshotRow
from issuebot.events import EventBus, EventSink, LogSink, StateChanged
```

In `src/issuebot/cli.py` replace

```
_run_session = run_session
_orchestrator_factory = Orchestrator
_slack_post = urllib_post
```

with

```
_run_session = run_session
_orchestrator_factory = Orchestrator
_slack_post = urllib_post
_database_factory: Callable[[str], Database] = Database
```

In `src/issuebot/cli.py` replace

```
    _add_workflow_option(worker)
    worker.set_defaults(func=cmd_worker)
    return parser
```

with

```
    _add_workflow_option(worker)
    worker.set_defaults(func=cmd_worker)

    migrate = subparsers.add_parser("migrate", help="apply pending database migrations")
    _add_workflow_option(migrate)
    migrate.set_defaults(func=cmd_migrate)

    status = subparsers.add_parser("status", help="print the worker's last runtime snapshot")
    _add_workflow_option(status)
    status.set_defaults(func=cmd_status)

    stats = subparsers.add_parser("stats", help="issues closed and runs started, by window and day")
    _add_workflow_option(stats)
    stats.add_argument(
        "--days", type=int, default=7, help="length of the daily series (default: 7)"
    )
    stats.set_defaults(func=cmd_stats)

    refresh = subparsers.add_parser("refresh", help="ask a running worker to poll now (NOTIFY)")
    _add_workflow_option(refresh)
    refresh.set_defaults(func=cmd_refresh)
    return parser
```

In `src/issuebot/cli.py` replace

```
    checks.extend(_github_checks(adapter))
    checks.append(
        Check(
            "database.url",
            "ok",
            "configured" if cfg.database.url else "not configured (history and dashboard disabled)",
        )
    )
    checks.append(_slack_check(cfg, probe=slack_probe))
```

with

```
    checks.extend(_github_checks(adapter))
    checks.append(_database_check(cfg))
    checks.append(_slack_check(cfg, probe=slack_probe))
```

In `src/issuebot/cli.py` replace

```
def _slack_check(settings: Settings, *, probe: bool) -> Check:
```

with

```
def _database_check(settings: Settings) -> Check:
    """The database.url line: presence, URL scheme, then a connect and the schema version."""
    subject = "database.url"
    if settings.database.url is None:
        return Check(subject, "ok", "not configured (history and dashboard disabled)")
    url = settings.database.url.get_secret_value()
    if not is_postgres_url(url):
        return Check(subject, "fail", "not a postgresql:// URL")
    try:
        probe = asyncio.run(_database_factory(url).probe())
    except DatabaseError as exc:
        return Check(subject, "fail", exc.message)
    detail = f"connected ({probe.server_version}); schema version {probe.schema_version}"
    if probe.ahead:
        detail += f" is newer than this issuebot knows ({probe.latest_version})"
        return Check(subject, "fail", detail)
    if probe.behind:
        detail += f" of {probe.latest_version}; run issuebot migrate"
        return Check(subject, "warn", detail)
    return Check(subject, "ok", detail)


def _slack_check(settings: Settings, *, probe: bool) -> Check:
```

In `src/issuebot/cli.py` replace

```
def _build_bus(settings: Settings) -> tuple[EventBus, SlackSink | None]:
    """The log sink, plus the Slack sink when configured; the caller starts and closes it."""
    slack = _slack_sink(settings)
    sinks: list[EventSink] = [LogSink()]
    if slack is not None:
        sinks.append(slack)
    return EventBus(sinks), slack
```

with

```
@dataclass
class _Sinks:
    """The bus and the sinks whose lifetime the CLI owns (started before, closed after)."""

    bus: EventBus
    slack: SlackSink | None
    postgres: PostgresSink | None
    database: Database | None

    def start(self) -> None:
        if self.slack is not None:
            self.slack.start(self.bus)
        if self.postgres is not None:
            self.postgres.start()

    async def close(self) -> None:
        if self.slack is not None:
            await self.slack.close()
        if self.postgres is not None:
            await self.postgres.close()

    def record_issues(self, issues: Sequence[Issue]) -> None:
        if self.postgres is not None:
            self.postgres.record_issues(issues)


async def _open_database(settings: Settings) -> Database | None:
    """Migrate at start when database.url is set; None when it is not; raises DatabaseError."""
    if settings.database.url is None:
        return None
    database = _database_factory(settings.database.url.get_secret_value())
    result = await database.migrate()
    get_logger(__name__).info(
        "db_migrated",
        database=database.description,
        applied=list(result.applied),
        version=result.version,
    )
    return database


async def _build_sinks(settings: Settings) -> _Sinks:
    """The log sink, plus Slack and PostgreSQL when configured; raises DatabaseError."""
    slack = _slack_sink(settings)
    database = await _open_database(settings)
    postgres = PostgresSink(database.store(settings.github.labels)) if database else None
    sinks: list[EventSink] = [LogSink()]
    if slack is not None:
        sinks.append(slack)
    if postgres is not None:
        sinks.append(postgres)
    return _Sinks(EventBus(sinks), slack, postgres, database)


def _database_or_report(settings: Settings) -> Database | None:
    if settings.database.url is None:
        print("[FAIL] database: not configured; export DATABASE_URL or set database.url: $VAR")
        return None
    return _database_factory(settings.database.url.get_secret_value())
```

In `src/issuebot/cli.py` replace

```
    bus, slack = _build_bus(settings)
    if slack is not None:
        slack.start(bus)
    try:
        return await _claim_and_run(
            workflow, adapter, bus, issue, workspaces=workspaces, attempt=attempt, rework=rework
        )
    finally:
        if slack is not None:
            await slack.close()


async def _claim_and_run(
    workflow: Workflow,
    adapter: GitHubAdapter,
    bus: EventBus,
    issue: Issue,
    *,
    workspaces: WorkspaceManager,
    attempt: int,
    rework: bool,
) -> int:
    settings = workflow.config
    number = issue.number
```

with

```
    try:
        sinks = await _build_sinks(settings)
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    sinks.start()
    sinks.record_issues([issue])
    try:
        return await _claim_and_run(
            workflow,
            adapter,
            sinks.bus,
            issue,
            workspaces=workspaces,
            attempt=attempt,
            rework=rework,
            record=sinks.record_issues,
        )
    finally:
        await sinks.close()


async def _claim_and_run(
    workflow: Workflow,
    adapter: GitHubAdapter,
    bus: EventBus,
    issue: Issue,
    *,
    workspaces: WorkspaceManager,
    attempt: int,
    rework: bool,
    record: Callable[[Sequence[Issue]], None] | None = None,
) -> int:
    settings = workflow.config
    number = issue.number
```

In `src/issuebot/cli.py` replace

```
        if refreshed:
            issue = refreshed[0]
    result = await _run_session(
```

with

```
        if refreshed:
            issue = refreshed[0]
            if record is not None:
                record(refreshed)
    result = await _run_session(
```

In `src/issuebot/cli.py` replace

```
async def _run_worker(workflow: Workflow) -> int:
    """Run the orchestrator until a stop signal; 1 when startup validation fails."""
    bus, slack = _build_bus(workflow.config)
    orchestrator = _orchestrator_factory(
        workflow,
        bus=bus,
        adapter_factory=_adapter_factory,
        run_session=_run_session,
        which=_which,
    )
    if slack is not None:
        slack.start(bus)
    loop = asyncio.get_running_loop()
```

with

```
async def _run_worker(workflow: Workflow) -> int:
    """Run the orchestrator until a stop signal; 1 when startup validation fails."""
    try:
        sinks = await _build_sinks(workflow.config)
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    postgres = sinks.postgres
    orchestrator = _orchestrator_factory(
        workflow,
        bus=sinks.bus,
        adapter_factory=_adapter_factory,
        run_session=_run_session,
        which=_which,
        on_snapshot=postgres.record_snapshot if postgres is not None else None,
        on_issues=postgres.record_issues if postgres is not None else None,
    )
    listener: RefreshListener | None = None
    if sinks.database is not None:
        listener = sinks.database.listener(orchestrator.request_refresh)
    sinks.start()
    if listener is not None:
        listener.start()
    loop = asyncio.get_running_loop()
```

In `src/issuebot/cli.py` replace

```
        for signum in signals:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signum)
        if slack is not None:
            await slack.close()
    return 0
```

with

```
        for signum in signals:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signum)
        if listener is not None:
            await listener.close()
        await sinks.close()
    return 0


# --- migrate, status, stats, refresh ----------------------------------------------------------


def cmd_migrate(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    database = _database_or_report(workflow.config)
    if database is None:
        return 1
    try:
        result = asyncio.run(database.migrate())
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    for label in result.applied:
        print(f"[ OK ] migration {label}: applied")
    if result.applied:
        print(f"[ OK ] database: schema version {result.version}")
    else:
        print(f"[ OK ] database: unchanged at schema version {result.version}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    database = _database_or_report(workflow.config)
    if database is None:
        return 1
    return asyncio.run(_status(database))


async def _status(database: Database) -> int:
    try:
        async with database.queries() as queries:
            row = await queries.snapshot()
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    if row is None:
        print("no runtime snapshot yet (has the worker run against this database?)")
        return 0
    print(render_status(row, now=datetime.now(UTC)), end="")
    return 0


@dataclass(frozen=True)
class StatsView:
    closed_1d: int
    closed_7d: int
    runs_1d: int
    runs_7d: int
    by_state: dict[str, int]
    series: list[DailyPoint]


def cmd_stats(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    if args.days < 1:
        print("[FAIL] stats: --days must be at least 1")
        return 1
    database = _database_or_report(workflow.config)
    if database is None:
        return 1
    return asyncio.run(_stats(database, args.days))


async def _stats(database: Database, days: int) -> int:
    try:
        async with database.queries() as queries:
            groups = await queries.issues_by_state()
            view = StatsView(
                closed_1d=await queries.closed_count(timedelta(days=1)),
                closed_7d=await queries.closed_count(timedelta(days=7)),
                runs_1d=await queries.runs_count(timedelta(days=1)),
                runs_7d=await queries.runs_count(timedelta(days=7)),
                by_state={state: len(rows) for state, rows in groups.items()},
                series=await queries.daily_series(days),
            )
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    print(render_stats(view), end="")
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    database = _database_or_report(workflow.config)
    if database is None:
        return 1
    try:
        asyncio.run(database.notify_refresh())
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    print("[ OK ] refresh: notified issuebot_refresh")
    return 0


def _stamp(value: object) -> str:
    """A second-precision UTC stamp for a datetime or an ISO 8601 string; '-' for None."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "-"


def _table(rows: Sequence[Sequence[str]]) -> list[str]:
    """Left-aligned columns two spaces apart, the last column unpadded."""
    if not rows:
        return []
    last = len(rows[0]) - 1
    widths = [max(len(row[column]) for row in rows) for column in range(last)]
    lines = []
    for row in rows:
        cells = [row[column].ljust(widths[column]) for column in range(last)]
        lines.append("  ".join([*cells, row[last]]).rstrip())
    return lines


def render_status(row: SnapshotRow, *, now: datetime) -> str:
    """The runtime snapshot row as text; tolerant of missing keys (the data is JSON)."""
    data = row.data
    age = max((now - row.written_at).total_seconds(), 0.0)
    if data.get("config_valid"):
        config = "config valid"
    else:
        config = f"config error: {data.get('config_error')}"
    running = list(data.get("running") or [])
    retrying = list(data.get("retrying") or [])
    totals = dict(data.get("totals") or {})
    counters = dict(data.get("counters") or {})
    lines = [
        f"snapshot: {_stamp(row.at)} (written {_stamp(row.written_at)}, {age:.0f} s ago)",
        f"workflow: {data.get('workflow_path')} ({config})",
        f"tick {data.get('tick_count')}, last tick {_stamp(data.get('last_tick_at'))}, "
        f"poll {data.get('poll_interval_ms')} ms, {data.get('max_concurrent_agents')} slots",
        f"running: {len(running)}",
    ]
    if running:
        table = [("  NUMBER", "ATTEMPT", "TURNS", "RUN_ID", "LAST_EVENT", "STARTED", "IDENTIFIER")]
        table.extend(
            (
                f"  {entry.get('issue_number')}",
                str(entry.get("attempt")),
                str(entry.get("turns")),
                str(entry.get("run_id")),
                str(entry.get("last_event") or "-"),
                _stamp(entry.get("started_at")),
                str(entry.get("identifier")),
            )
            for entry in running
        )
        lines.extend(_table(table))
    lines.append(f"retrying: {len(retrying)}")
    if retrying:
        table = [("  NUMBER", "KIND", "ATTEMPT", "DUE", "ERROR")]
        table.extend(
            (
                f"  {entry.get('issue_number')}",
                str(entry.get("kind")),
                str(entry.get("attempt")),
                _stamp(entry.get("due_at")),
                str(entry.get("error") or "-"),
            )
            for entry in retrying
        )
        lines.extend(_table(table))
    lines.append(
        f"totals: {counters.get('runs_started', 0)} runs started, "
        f"{counters.get('runs_ended', 0)} ended, {counters.get('issues_completed', 0)} completed, "
        f"{counters.get('issues_cancelled', 0)} cancelled, {counters.get('blocked', 0)} blocked; "
        f"{totals.get('total_tokens', 0)} tokens, ${float(totals.get('cost_usd', 0.0)):.2f}, "
        f"{float(totals.get('seconds_running', 0.0)):.0f} s running"
    )
    return "\n".join(lines) + "\n"


def render_stats(view: StatsView) -> str:
    lines = _table(
        [
            ("WINDOW", "CLOSED", "RUNS"),
            ("1d", str(view.closed_1d), str(view.runs_1d)),
            ("7d", str(view.closed_7d), str(view.runs_7d)),
        ]
    )
    states = ", ".join(f"{state} {count}" for state, count in view.by_state.items())
    lines.append(f"issues: {states}")
    lines.append("")
    table = [("DAY", "CLOSED", "RUNS")]
    table.extend((point.day.isoformat(), str(point.closed), str(point.runs)) for point in view.series)
    lines.extend(_table(table))
    return "\n".join(lines) + "\n"
```


- [ ] **Step 4: Run the tests to verify they pass, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && timeout 300 uv run pytest -q`
Expected: lint clean; `654 passed, 30 skipped` (every CLI test runs against `FakeDatabase`; no CLI test opens a socket).

Run: `DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q`
Expected: `684 passed`.

- [ ] **Step 5: Smoke-test the real commands against the compose database**

With `GH_TOKEN` exported from `gh auth token` (the validate probes need it; never print it):

```bash
export GH_TOKEN=$(gh auth token) && export DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot && uv run issuebot validate 2>/dev/null | grep -E "database.url|checks:" && uv run issuebot status 2>/dev/null; uv run issuebot migrate 2>/dev/null && uv run issuebot migrate 2>/dev/null && uv run issuebot validate 2>/dev/null | grep -E "database.url|checks:" && uv run issuebot status 2>/dev/null && uv run issuebot stats --days 3 2>/dev/null && uv run issuebot refresh 2>/dev/null && DATABASE_URL=postgresql://issuebot:wrong@127.0.0.1:5440/issuebot uv run issuebot validate 2>/dev/null | grep database.url
```

Expected, in order (the server version is whatever the `postgres:18` image carries, 18.6 at the time of writing): `[WARN] database.url: connected (PostgreSQL 18.6); schema version 0 of 1; run issuebot migrate` and `12 checks: 0 failed, 2 warnings` (the second warning is Slack, unset); `[FAIL] database: UndefinedTable: relation "runtime_snapshot" does not exist` (exit 1); `[ OK ] migration 0001_initial: applied` and `[ OK ] database: schema version 1`; `[ OK ] database: unchanged at schema version 1`; `[ OK ] database.url: connected (PostgreSQL 18.6); schema version 1` and `12 checks: 0 failed, 1 warnings`; `no runtime snapshot yet (has the worker run against this database?)`; the stats block (`WINDOW  CLOSED  RUNS`, two zero rows, `issues: todo 0, in_progress 0, review 0, rework 0, complete 0`, a blank line, `DAY         CLOSED  RUNS` and three zero days ending today); `[ OK ] refresh: notified issuebot_refresh`; `[FAIL] database.url: cannot connect: connection failed: connection to server at "127.0.0.1", port 5440 failed: FATAL:  password authentication failed for user "issuebot"` (one line; the password never appears).

Then put the compose database back the way the live check expects it (an empty `public` schema, so Task 8's `migrate` applies the migration):

```bash
docker compose exec -T db psql -U issuebot -d issuebot -c "DROP TABLE IF EXISTS events, runs, issues, runtime_snapshot, schema_migrations" -c "\dt"
```

Expected: `DROP TABLE` then `Did not find any tables.`

- [ ] **Step 6: Commit**

```bash
git add --all
git commit -m "feat: migrate, status, stats and refresh commands, the database validate check, worker and run-once wiring"
```

---

### Task 7: Documentation

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `compose.yaml`, `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`, `docs/superpowers/specs/2026-09-02-phase-1-foundations-design.md`, `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md`, the dot-env example file at the repository root

No code; no test count changes. Spec: §8 (the amendment notes), §10, §15 (the documentation bullet).

- [ ] **Step 1: `CLAUDE.md`, `README.md`, `compose.yaml` and the specs (Edit tool)**


In `CLAUDE.md` replace

```markdown
uv run pytest                        # tests (hermetic; no network, no Docker)
uv run pytest tests/test_cli.py -k validate   # one file / one pattern
```

with

```markdown
uv run pytest                        # tests (hermetic; no network, no Docker; DB tests skip)
uv run pytest tests/test_cli.py -k validate   # one file / one pattern
ISSUEBOT_DB_PORT=5440 docker compose up -d db   # a local postgres:18 (5432 is taken on this host)
DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot uv run pytest   # + the DB tests
```

In `CLAUDE.md` replace

```markdown
uv run issuebot worker               # the long-running orchestrator; SIGTERM or Ctrl-C stops it
docker compose build                 # image: git, gh, claude, app venv
```

with

```markdown
uv run issuebot worker               # the long-running orchestrator; SIGTERM or Ctrl-C stops it
uv run issuebot migrate              # apply pending .sql migrations (worker and run-once do it too)
uv run issuebot status               # the worker's last runtime snapshot, read from the database
uv run issuebot stats [--days N]     # issues closed and runs started: 1d, 7d and per day
uv run issuebot refresh              # NOTIFY issuebot_refresh: a running worker polls at once
docker compose build                 # image: git, gh, claude, app venv
```

In `CLAUDE.md` replace

```markdown
- `issuebot.events`: frozen dataclass events (`EVENT_KINDS`), `EventBus.publish()`
  (synchronous, sink failures isolated and counted), `LogSink`.
```

with

```markdown
- `issuebot.events`: frozen dataclass events (`EVENT_KINDS`), `EventBus.publish()`
  (synchronous, sink failures isolated and counted), `LogSink`. `RunEnded.log_dir` (Phase 6)
  carries the run's log directory.
```

In `CLAUDE.md` replace

```markdown
  `missing_labels`), then `tick()` (reconcile: stalls, running refresh with a one-tick grace for
  `review`, terminal sweep on the first and every tenth tick; mtime reload; preflight; fetch
  `in_progress`/`rework`/`todo`; dispatch while slots remain; snapshot) and a queue wait that
  fires retries (continuation 1 s; failure backoff; `escape`; `slots`) and handles worker exits
  (the session's final transition is published before any release; `max_turns` while
  `in_progress` or `max_attempts` failures → the blocked escape).
  `request_refresh()`, `request_stop()`, `snapshot()`; SIGTERM shutdown waits for `after_run`.
```

with

```markdown
  `missing_labels`), then `tick()` (reconcile: stalls, running refresh with one poll interval of
  grace for `review` measured on the monotonic clock, terminal sweep on the first and every tenth
  tick; mtime reload; preflight; fetch `in_progress`/`rework`/`todo`, plus `review` when an
  `on_issues` observer is attached; dispatch while slots remain; snapshot) and a queue wait that
  fires retries (continuation 1 s; failure backoff; `escape`; `slots`) and handles worker exits
  (the session's final transition is published before any release; `max_turns` while
  `in_progress` or `max_attempts` failures → the blocked escape).
  `request_refresh()`, `request_stop()`, `snapshot()`; SIGTERM shutdown waits for `after_run`
  and publishes a final snapshot. `on_snapshot` (every tick and at shutdown) and `on_issues`
  (every successful fetch) are how polled data reaches the database sink without the
  orchestrator importing `db`.
```

In `CLAUDE.md` replace

```markdown
- `issuebot.cli`: argparse; `validate` (twelve checks: three network probes through the
  adapter, a `claude --version` floor of 2.1.259, a `notifications.slack` check that warns when
  `SLACK_WEBHOOK_URL` is unset, requires `https`, and with `--slack-probe` posts one test
  message, and a prompt render against a sample issue), `labels ensure`, `issues list`,
  `run-once <number> [--show-prompt]` (claims `in-progress`, runs one session, never sets
  `review`), `worker [--workflow PATH]` (the orchestrator until SIGTERM/SIGINT; `[FAIL]
  startup:` lines and exit 1 when the startup probes fail); `run-once` and `worker` start the
  Slack sink before and close it after (never for a non-`https` webhook); exit codes 0/1/2
  (ok / failed / workflow unloadable).
  Tests substitute `_which`, `_claude_version`, `_adapter_factory`, `_run_session`,
  `_orchestrator_factory` and `_slack_post`.
```

with

```markdown
- `issuebot.db`: the observability store, imported by `cli` only; imports `config`, `events`,
  `github` and `log`. `migrations/NNNN_name.sql` applied by `migrate.py` in one transaction
  under an advisory lock (`schema_migrations` bookkeeping; a recorded version newer than the
  files is an error). `connection.py`: `connect` (autocommit, 5 s connect timeout, UTC session),
  `describe`/`redact` (the URL's password never reaches a log or a line), `reconnect_delay`
  (1, 2, 4, 8, 16, then 30 s). `store.py`: `PostgresStore` (`apply_event` appends to `events`
  and upserts `runs` on `run_started`/`run_ended` or updates `issues` on `state_changed`,
  `issue_completed`, `issue_cancelled`; `upsert_issues`; `write_snapshot`); every `issues`
  write is guarded by `seen_at`, so write order never matters. `sink.py`: `PostgresSink`
  (`handle` enqueues events, cap 1000; `record_issues` merges polled snapshots into one
  pending batch; `record_snapshot` keeps the latest; one drain task writes, reconnects with
  backoff and retries the item in flight; statement failures are dropped and counted;
  `close()` drains for up to 10 s). `listen.py`: `RefreshListener` (`LISTEN issuebot_refresh`
  on its own connection, callback per NOTIFY, reconnects). `queries.py`: `Queries` over one
  connection (`closed_count`, `runs_count`, `daily_series`, `issues_by_state`, `runs_for_issue`,
  `recent_events`, `snapshot`) returning the frozen row types Phase 7 renders. `database.py`:
  the `Database` facade the CLI goes through (`migrate`, `probe`, `queries`, `store`,
  `listener`, `notify_refresh`). Constants, not settings; a `database.url` change needs a
  restart. Tests: `db_url` (conftest) creates a schema per test and skips without
  `DATABASE_URL`; the sink and listener tests use fakes.
- `issuebot.cli`: argparse; `validate` (twelve checks: three network probes through the
  adapter, a `claude --version` floor of 2.1.259, a `database.url` check that connects and
  reports the server and schema versions (behind warns, ahead or unreachable fails), a
  `notifications.slack` check that warns when `SLACK_WEBHOOK_URL` is unset, requires `https`,
  and with `--slack-probe` posts one test message, and a prompt render against a sample issue),
  `labels ensure`, `issues list`, `run-once <number> [--show-prompt]` (claims `in-progress`,
  runs one session, never sets `review`), `worker [--workflow PATH]` (the orchestrator until
  SIGTERM/SIGINT; `[FAIL] startup:` lines and exit 1 when the startup probes fail), `migrate`,
  `status`, `stats [--days N]` and `refresh` (each `[FAIL] database:` and exit 1 without
  `DATABASE_URL`); `run-once` and `worker` migrate first when `database.url` is set (a failure
  is `[FAIL] database:` and exit 1), start the Slack and PostgreSQL sinks before and close them
  after (Slack never for a non-`https` webhook); `worker` also passes `on_snapshot`/`on_issues`
  to the orchestrator and runs the refresh listener; exit codes 0/1/2 (ok / failed / workflow
  unloadable).
  Tests substitute `_which`, `_claude_version`, `_adapter_factory`, `_run_session`,
  `_orchestrator_factory`, `_slack_post` and `_database_factory`.
```

In `README.md` replace

````markdown
uv run issuebot worker            # the long-running orchestrator; Ctrl-C stops it
cp .env.example .env              # then fill in GH_TOKEN and Claude auth
docker compose up --build         # postgres:18 + worker (issuebot worker)
```
````

with

````markdown
uv run issuebot worker            # the long-running orchestrator; Ctrl-C stops it
uv run issuebot migrate           # apply the database migrations (worker does this at start)
uv run issuebot status            # what the worker was doing at its last tick
uv run issuebot stats             # issues closed and agents run: last day, week, per day
uv run issuebot refresh           # make a running worker poll GitHub now
cp .env.example .env              # then fill in GH_TOKEN and Claude auth
docker compose up --build         # postgres:18 + worker (issuebot worker)
```

History is optional: with `DATABASE_URL` set (compose sets it for the worker; on the host
export `postgresql://issuebot:issuebot@127.0.0.1:${ISSUEBOT_DB_PORT:-5432}/issuebot` after
`docker compose up -d db`) the worker records every event, run and issue snapshot in
PostgreSQL and `status`, `stats` and `refresh` work; without it the worker runs exactly as
before. The worker applies pending migrations when it starts and fails fast if the database
is configured but unreachable; `validate` reports the schema version. The tests that need a
database read `DATABASE_URL` and are skipped when it is unset.
````

In `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md` replace

```markdown
**Done when.** After a dogfooding run, `issuebot stats` shows correct 1-day and
7-day counts and a daily series; killing and restarting `worker` preserves history
and the snapshot row recovers within one tick.

### Phase 7: Web dashboard
```

with

```markdown
**Done when.** After a dogfooding run, `issuebot stats` shows correct 1-day and
7-day counts and a daily series; killing and restarting `worker` preserves history
and the snapshot row recovers within one tick.

Decided 2026-09-04 (Phase 6 spec): polled issues and the runtime snapshot reach the
sink through injected callbacks (`on_issues`, `on_snapshot`), not new event kinds, so
the log never carries a bulk poll; the sink keeps one queue with two coalescing
markers and every `issues` write is guarded by its observation time; the tick polls
`review` too when an observer is attached; the `review` grace becomes one poll
interval on the monotonic clock so a `NOTIFY`-driven tick cannot cut it short;
`shutdown()` publishes a final snapshot; `RunEnded` gains `log_dir`; `worker` and
`run-once` migrate at start and fail fast when the configured database is unusable;
`validate` connects and reports the schema version; counts come from
`issues.closed_at` (complete only) and `runs.started_at`; `psycopg[binary]` without a
pool; `issuebot refresh` (`NOTIFY`) joins the CLI. Deferred: a connection pool, event
retention, a reload hook for `database.url`.

### Phase 7: Web dashboard
```

In `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md` replace

```markdown
4. `fetch_issues_by_states([IN_PROGRESS, REWORK, TODO])`; a `GitHubError`
   logs `candidates_fetch_failed` at WARNING and skips steps 5 and 6.
```

with

```markdown
4. `fetch_issues_by_states([IN_PROGRESS, REWORK, TODO])`; a `GitHubError`
   logs `candidates_fetch_failed` at WARNING and skips steps 5 and 6.
   *Amended by Phase 6 (spec §8.1):* with an `on_issues` observer attached the
   fetch covers `REVIEW` as well (`OBSERVED_STATES`), and every successful
   fetch the orchestrator makes (this one, reconcile's refresh, the terminal
   sweep, a fired retry's refresh) is handed to `on_issues`, exceptions logged
   as `issues_consumer_failed` and swallowed.
```

In `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md` replace

```markdown
stops the worker within two intervals. Every other move stops it at the
tick that sees it.
```

with

```markdown
stops the worker within two intervals. Every other move stops it at the
tick that sees it. *Amended by Phase 6 (spec §8.2):* the grace is one poll
interval measured on the monotonic clock (`RunningEntry.review_seen_mono`),
not one tick, so a `NOTIFY`-driven tick cannot cut it short;
`REVIEW_GRACE_TICKS` is gone.
```

In `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md` replace

```markdown
with the counters. Issues stay `in_progress` so the next process resumes
them from `session.json`.
```

with

```markdown
with the counters. Issues stay `in_progress` so the next process resumes
them from `session.json`. *Amended by Phase 6 (spec §8.3):* `shutdown()` ends
with the same `on_snapshot` publish the tick makes, so the stored snapshot of
a stopped worker shows nothing running.
```

In `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md` replace

```markdown
13. **`review` gets a one-tick grace** before reconciliation cancels the
    worker; every other move out of `in_progress` cancels at the tick that
    sees it.
```

with

```markdown
13. **`review` gets a one-tick grace** before reconciliation cancels the
    worker; every other move out of `in_progress` cancels at the tick that
    sees it. (Phase 6 makes it one poll interval in time; §6.5.)
```

In `docs/superpowers/specs/2026-09-02-phase-1-foundations-design.md` replace

```markdown
| `RunEnded` | `run_ended` | `run_id`, `outcome: "succeeded" \| "failed" \| "timed_out" \| "stalled" \| "cancelled"`, `error: str \| None`, `turns: int`, `input_tokens: int`, `output_tokens: int`, `cost_usd: float`, `duration_s: float` |
```

with

```markdown
| `RunEnded` | `run_ended` | `run_id`, `outcome: "succeeded" \| "failed" \| "timed_out" \| "stalled" \| "cancelled"`, `error: str \| None`, `turns: int`, `input_tokens: int`, `output_tokens: int`, `cost_usd: float`, `duration_s: float`, `log_dir: str \| None = None` (added by Phase 6) |
```

In `compose.yaml` replace

```yaml
  worker:
    build: .
    command: ["worker"]
```

with

```yaml
  worker:
    build: .
    # The worker applies pending database migrations when it starts (issuebot migrate is the
    # manual equivalent) and fails fast if DATABASE_URL is set but the server is unusable.
    command: ["worker"]
```


- [ ] **Step 2: The dot-env example (Edit tool only; never name this file in a shell command)**


In `.env.example` replace

```
# Host port for the compose db service (container side stays 5432).
ISSUEBOT_DB_PORT=5432
```

with

```
# Host port for the compose db service (container side stays 5432).
ISSUEBOT_DB_PORT=5432

# Optional: where the worker records events, runs, issue snapshots and its runtime snapshot
# (Phase 6). compose.yaml sets it for the worker container (postgresql://issuebot:issuebot@db:5432/issuebot),
# so set it here only for a worker or the migrate/status/stats/refresh commands run on the
# host against the compose db: postgresql://issuebot:issuebot@127.0.0.1:${ISSUEBOT_DB_PORT}/issuebot.
# The worker runs without history when it is unset; changing it needs a worker restart.
DATABASE_URL=
```


- [ ] **Step 3: Verify and commit**

Run: `uv run pre-commit run --all-files && timeout 300 uv run pytest -q`
Expected: hooks pass (no document in this task carries a new Python fence, so nothing is reflowed); `654 passed, 30 skipped`. `git status --short` lists exactly the seven files.

```bash
git add --all
git commit -m "docs: describe issuebot.db, the four commands and the Phase 6 amendments"
```

---

### Task 8: Live check against `jleavers/issuebot-scratch` and the compose database

**Files:** none in this repository. Everything here happens against GitHub, the compose database on port 5440, a Slack test channel, and directories outside every checkout. This task spends real Claude budget under the operator's subscription login (about $0.60 to $0.80; no `ANTHROPIC_API_KEY` exported) and creates a real issue, branch and pull request; that is intended. Never print `GH_TOKEN`, `SLACK_WEBHOOK_URL` or `DATABASE_URL` (the last carries a password even though it is the compose default). The executor never merges a pull request; Step 6 asks the operator to. The executor cannot see the Slack channel; Slack is incidental here (it was proven in Phase 5), the evidence is the worker log, the CLI output and the database.

Starting state (from the Phase 5 live check): issues #1 and #5 closed `issuebot/complete`; issue #3 (`Add a multiply function`) in `issuebot/review` with PR #4 open, rebased onto `main` and mergeable; `~/issuebot-workspaces/issuebot-scratch-3` holds a pre-rebase commit (harmless: nobody dispatches #3); `~/issuebot-scratch/WORKFLOW.md` already has the repo, the workspace root, `stall_timeout_ms: 1800000` and `run_ended` in the Slack allow-list; `~/issuebot-scratch/slack-webhook` (mode 600) exists; `~/issuebot-scratch/worker.log` is the Phase 5 log.

- [ ] **Step 1: Rename the old log, bring the database up, migrate, validate**

```bash
mv ~/issuebot-scratch/worker.log ~/issuebot-scratch/worker-phase5.log && cd /home/jleavers/_dev/issuebot && ISSUEBOT_DB_PORT=5440 docker compose up -d db && sleep 6 && docker compose ps db && export GH_TOKEN=$(gh auth token) && export SLACK_WEBHOOK_URL=$(cat ~/issuebot-scratch/slack-webhook) && export DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot && unset ANTHROPIC_API_KEY && uv run issuebot validate --workflow ~/issuebot-scratch/WORKFLOW.md | grep -E "database.url|notifications.slack|checks:" && uv run issuebot migrate --workflow ~/issuebot-scratch/WORKFLOW.md && uv run issuebot validate --workflow ~/issuebot-scratch/WORKFLOW.md | grep -E "database.url|checks:" && uv run issuebot status --workflow ~/issuebot-scratch/WORKFLOW.md
```

Expected: the container is `healthy`; `[WARN] database.url: connected (PostgreSQL 18.x); schema version 0 of 1; run issuebot migrate`, `[ OK ] notifications.slack: configured (blocked, run_ended, state_changed)` and `12 checks: 0 failed, 1 warnings`; `[ OK ] migration 0001_initial: applied` and `[ OK ] database: schema version 1` (if the schema was already at version 1 from an earlier run the line reads `unchanged at schema version 1`; record it and carry on); then `[ OK ] database.url: connected (PostgreSQL 18.x); schema version 1` and `12 checks: 0 failed, 0 warnings`; `no runtime snapshot yet (has the worker run against this database?)`.

- [ ] **Step 2: Start the worker detached and read the first tick from the database**

```bash
export GH_TOKEN=$(gh auth token) && export SLACK_WEBHOOK_URL=$(cat ~/issuebot-scratch/slack-webhook) && export DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot && unset ANTHROPIC_API_KEY && cd /home/jleavers/_dev/issuebot && setsid nohup uv run issuebot --log-format console worker --workflow ~/issuebot-scratch/WORKFLOW.md >> ~/issuebot-scratch/worker.log 2>&1 < /dev/null & sleep 8 && pgrep -f '\.venv/bin/issuebot --log-format console worker' > ~/issuebot-scratch/worker.pid && cat ~/issuebot-scratch/worker.pid && grep -E "db_migrated|db_sink_started|db_listen_started|db_connected|orchestrator_started|issue_finished|dispatched" ~/issuebot-scratch/worker.log | tail -8 && uv run issuebot status --workflow ~/issuebot-scratch/WORKFLOW.md && uv run issuebot stats --workflow ~/issuebot-scratch/WORKFLOW.md
```

Expected: one pid; the log shows `db_migrated applied=[] version=1 database=postgresql://issuebot@127.0.0.1:5440/issuebot` (no password), `db_sink_started`, `orchestrator_started repo=jleavers/issuebot-scratch`, `db_listen_started channel=issuebot_refresh`, `db_connected attempt=1`, no `dispatched` (nothing is `todo`) and no `issue_finished` (#1 and #5 are already `complete`); `status` prints `snapshot: <now> (written <now>, N s ago)`, `tick 1, ... poll 30000 ms, 2 slots`, `running: 0`, `retrying: 0`, `totals: 0 runs started, ...`; `stats` prints `issues: todo 0, in_progress 0, review 1, rework 0, complete 2` (the first tick's sweep recorded #1 and #5, the observed fetch recorded #3) and `closed 7d` counts those of #1 and #5 closed within the last seven days (both, when the check runs within a week of 2026-09-04; `1d` is 0 unless one closed today). Paste the three outputs.

- [ ] **Step 3: `issuebot refresh` reaches the worker**

```bash
export DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot && cd /home/jleavers/_dev/issuebot && uv run issuebot status --workflow ~/issuebot-scratch/WORKFLOW.md | grep "^tick" && uv run issuebot refresh --workflow ~/issuebot-scratch/WORKFLOW.md && sleep 3 && grep -c db_refresh_received ~/issuebot-scratch/worker.log && uv run issuebot status --workflow ~/issuebot-scratch/WORKFLOW.md | grep "^tick"
```

Expected: `[ OK ] refresh: notified issuebot_refresh`; the log count is `1` (`db_refresh_received channel=issuebot_refresh`); the tick number in the second `status` line is one higher than in the first, three seconds later rather than thirty (the NOTIFY made the worker tick at once and the tick wrote a new snapshot).

- [ ] **Step 4: File a trivial issue and watch it reach `review` and the database**

Write `~/issuebot-scratch/issue-power.md` with the Write tool:

```markdown
Add a `power(base: int, exponent: int) -> int` function to `src/scratch/__init__.py` next to the existing arithmetic functions, returning `base ** exponent`.

## Acceptance criteria

- `power(2, 3) == 8` and `power(5, 0) == 1`.
- A test in `tests/test_scratch.py` covers both cases.
- `uv run pytest -q` passes.
```

```bash
gh issue create -R jleavers/issuebot-scratch --title "Add a power function" --body-file ~/issuebot-scratch/issue-power.md --label issuebot/todo
```

Note the number (`N` below). Then wait with a bounded loop under `run_in_background` (up to fifteen minutes):

```bash
timeout 900 bash -c 'until grep -q "to_label=issuebot/review" ~/issuebot-scratch/worker.log; do sleep 10; done'; sleep 5; grep -E "state_changed|dispatched|run_started|run_ended|retry_|db_write|db_queue|db_connect" ~/issuebot-scratch/worker.log | tail -16
```

Expected for issue N: `state_changed actor=issuebot to_label=issuebot/in-progress`, `dispatched`, `run_started`, ..., `state_changed actor=agent to_label=issuebot/review pr_url=...`, `run_ended outcome=succeeded`, `retry_scheduled kind=continuation` and `retry_released`; no `db_write_retry`, `db_write_failed`, `db_write_crashed`, `db_queue_full` or `db_connect_failed`. Then read the database:

```bash
export DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot && cd /home/jleavers/_dev/issuebot && uv run issuebot stats --workflow ~/issuebot-scratch/WORKFLOW.md && docker compose exec -T db psql -U issuebot -d issuebot -c "select run_id, issue_number, attempt, outcome, turns, input_tokens, output_tokens, round(cost_usd::numeric, 2) as cost_usd, log_dir from runs" -c "select kind, issue_number, run_id is not null as has_run from events where issue_number = N order by id" -c "select number, state, state_label, github_state, pr_number, pr_state from issues order by number"
```

(Replace `N`.) Expected: `stats` shows `runs 1d = 1` and `7d = 1`, `issues: ... in_progress 0, review 2, ...` (#3 and N); `runs` has one row for N with `outcome = succeeded`, non-zero tokens, a cost and `log_dir` under `/home/jleavers/issuebot-workspaces/issuebot-scratch-N/.issuebot/runs/`; `events` for N lists, in order, `state_changed`, `run_started`, `pr_opened` and/or `state_changed` (the agent's move; `pr_opened` may come before or after depending on which refresh saw the pull request first), `run_ended`, and the `notification_sent` rows for the Slack deliveries; `issues` shows N as `review` / `issuebot/review` / `open` with its PR number and `open`, #3 as `review`, #1 and #5 as `complete` / `closed`. Also confirm on GitHub:

```bash
gh issue view N -R jleavers/issuebot-scratch --json labels,state --jq '{labels: [.labels[].name], state}'
```

Expected: `{"labels":["issuebot/review"],"state":"OPEN"}`. If the run ends `max_turns` instead, the worker applies the blocked escape; record it, read `turn-*.jsonl` under the workspace's `.issuebot/runs/`, and note that `runs.outcome` is still `succeeded` with `events` carrying a `blocked` row.

- [ ] **Step 5: Merged pull request → `complete` in the database (operator action)**

Ask the operator to merge PR #4 on GitHub (the executor never merges). Then wait for the next terminal sweep (every tenth tick, five minutes at the 30 s interval; `issuebot refresh` does not shorten this, the sweep is tick-counted):

```bash
timeout 420 bash -c 'until grep -q "issue_finished.*outcome=complete" ~/issuebot-scratch/worker.log; do sleep 10; done'; sleep 5; export DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot && cd /home/jleavers/_dev/issuebot && grep -E "issue_finished|to_label=issuebot/complete|workspace_removed" ~/issuebot-scratch/worker.log | tail -3 && uv run issuebot stats --workflow ~/issuebot-scratch/WORKFLOW.md && docker compose exec -T db psql -U issuebot -d issuebot -c "select number, state, github_state, closed_at is not null as has_closed_at from issues where number = 3"
```

Expected: `issue_finished outcome=complete` for issue 3, `state_changed actor=issuebot to_label=issuebot/complete`, `workspace_removed` for `issuebot-scratch-3`; `stats` shows `closed 1d = 1` (and `7d` one more than before) and `issues: ... review 1 ... complete 3`; the row for #3 reads `complete`, `closed`, `t` (the sweep's poll wrote `closed_at`, the `state_changed` event wrote the state).

- [ ] **Step 6: SIGTERM, restart, history preserved, snapshot recovers within one tick**

```bash
export DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot && cd /home/jleavers/_dev/issuebot && kill -TERM $(cat ~/issuebot-scratch/worker.pid) && sleep 3 && grep -E "orchestrator_stopped|slack_sink_closed|db_sink_closed|db_listen_closed" ~/issuebot-scratch/worker.log | tail -4 && (pgrep -f '\.venv/bin/issuebot --log-format console worker' || echo "no worker left") && uv run issuebot status --workflow ~/issuebot-scratch/WORKFLOW.md | head -4
```

Expected: `orchestrator_stopped` within a second, `slack_sink_closed`, `db_sink_closed written=W failed=0 dropped=0 reconnects=0` (W is the number of events plus issue batches plus snapshots written), `db_listen_closed notified=1 reconnects=0`; `no worker left`; `status` still prints the last snapshot, now with `running: 0` and its `written` stamp at the shutdown (the final snapshot). Then restart exactly as in Step 2 (same command), wait one poll interval, and:

```bash
sleep 35 && export DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot && cd /home/jleavers/_dev/issuebot && uv run issuebot status --workflow ~/issuebot-scratch/WORKFLOW.md | head -3 && uv run issuebot stats --workflow ~/issuebot-scratch/WORKFLOW.md && docker compose exec -T db psql -U issuebot -d issuebot -c "select count(*) as runs from runs" -c "select count(*) as events from events"
```

Expected: `status` shows a fresh `snapshot:` stamp (after the restart) with `tick 1` or `tick 2`; `stats` prints the same numbers as after Step 5; the `runs` and `events` counts are unchanged from Step 4/5 plus the restart's own snapshot writes (events grow only by the new process's events, none so far). History survived the restart and the snapshot row recovered within one tick.

- [ ] **Step 7: Stop the worker and report**

```bash
kill -TERM $(cat ~/issuebot-scratch/worker.pid) && sleep 3 && tail -3 ~/issuebot-scratch/worker.log && (pgrep -f '\.venv/bin/issuebot --log-format console worker' || echo "no worker left")
```

Paste the relevant log lines and CLI outputs from Steps 1 to 6, the `psql` rows, the `gh` outputs and the operator's confirmation of the merge into the report for the PR body. Do not merge N's pull request. Leave `~/issuebot-scratch/slack-webhook` and the compose database (its `pgdata` volume now holds the history) to the operator; `docker compose stop db` is fine if the operator asks.

---

### Task 9: Push the branch, whole-branch review, fix wave, and the pull request

**Files:** none (plus whatever the review's fix wave touches).

- [ ] **Step 1: Final full check**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files && timeout 300 uv run pytest -q && DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q && docker compose build`
Expected: everything passes; `654 passed, 30 skipped` then `684 passed`; the image builds (its `uv sync --frozen --no-dev` installs psycopg-binary 3.3.5; `docker run --rm --entrypoint python issuebot-worker -c "import psycopg; print(psycopg.pq.__impl__)"` prints `binary`); `git status --short` is empty; `git diff main -- pyproject.toml` shows only the `psycopg[binary]>=3.3` line.

- [ ] **Step 2: Push the branch**

Run: `git push -u origin phase-6-postgres`

- [ ] **Step 3: Whole-branch review (fable) and the fix wave**

Dispatch one reviewer subagent on model `fable` with the spec, this plan's Global Constraints, and `git diff main...phase-6-postgres`, asking for a whole-branch review against the spec: correctness of the SQL and the `seen_at` guards, the sink's reconnect and close paths, the listener's reconnect, the orchestrator amendments (nothing else changed there), secret hygiene (no URL in any log line or CLI line), the test counts, and the documentation. Classify findings Critical / Important / Minor. Fix every Critical and Important finding in one wave (TDD: a failing test first where the finding is testable), re-run Step 1, commit as `fix: <what the review found>` with the trailer, push. Record the Minors as parked follow-ups for the report. Repeat once if the fix wave was non-trivial.

- [ ] **Step 4: Write the PR body to a file under the session scratchpad directory (a separate call from Step 5; use the Write tool)**

`<scratchpad>/issuebot-phase-6-pr.md`:

```markdown
## Phase 6: Persistence

Implements `docs/superpowers/specs/2026-09-04-phase-6-persistence-design.md`.

- `issuebot.db`: numbered `.sql` migrations applied in one transaction under an advisory lock (`schema_migrations`; a newer recorded version is refused); `PostgresStore` appends every event, upserts `runs` on `run_started`/`run_ended`, upserts `issues` from polls and updates them on `state_changed`/`issue_completed`/`issue_cancelled` (every `issues` write guarded by its observation time), and rewrites the single `runtime_snapshot` row
- `PostgresSink`: `handle`, `record_issues` and `record_snapshot` only enqueue (events up to 1000, issue snapshots merged into one pending batch, the latest runtime snapshot in a slot); one drain task writes, reconnects with backoff (1, 2, 4, 8, 16, then 30 s), retries the item in flight, drops and counts statement failures; `close()` drains for up to 10 s
- `RefreshListener`: `LISTEN issuebot_refresh` on its own connection wired to `request_refresh()`, reconnecting; `issuebot refresh` sends the NOTIFY
- `Queries` with the view models Phase 7 renders: `closed_count`, `runs_count`, `daily_series`, `issues_by_state`, `runs_for_issue`, `recent_events`, `snapshot`
- CLI: `migrate`, `status`, `stats [--days N]`, `refresh`; `validate` connects and reports the server and schema versions (behind warns, ahead or unreachable fails); `worker` and `run-once` migrate at start and fail fast when the configured database is unusable; the Slack and PostgreSQL sinks are started before and closed after
- Polled issues and the runtime snapshot reach the sink through orchestrator callbacks (`on_issues`, `on_snapshot`), not new event kinds; the tick also polls `review` when an observer is attached; the `review` grace is one poll interval on the monotonic clock (a NOTIFY-driven tick cannot cut it short); `shutdown()` publishes a final snapshot; `RunEnded` carries `log_dir`
- One dependency, `psycopg[binary]`; no setting changes; DB tests read `DATABASE_URL` and are skipped, and reported as skipped, without it (CI's service container runs them)
- Live check against `jleavers/issuebot-scratch` with the compose database: `migrate` then `validate` OK, a `todo` issue ran to `review` with its run, events and issue rows recorded, `issuebot refresh` made the worker tick at once, merging PR #4 turned #3 `complete` in `stats`, and a SIGTERM plus restart preserved the history with the snapshot row recovering within one tick (output below)

The dashboard is Phase 7.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Append the live-check output from Task 8 under a `## Live check` heading before the generated-with line, and the session link the executing harness requires after it.

- [ ] **Step 5: Open the PR via the REST API (the CLI's `pr create` is blocked in this repo)**

```bash
gh api repos/jleavers/issuebot/pulls -X POST \
  -f title='Phase 6: Persistence' \
  -f head='phase-6-postgres' -f base='main' \
  -F body=@<scratchpad>/issuebot-phase-6-pr.md
```

Then confirm with `gh pr view --json title,body --jq '.title'` and watch CI with `gh pr checks --watch`. CI must be green (lint, tests with the service container and no skips, the Docker build) before handing over for human review. Do not merge.

---

## Acceptance criteria

Spec §15, restated for the executor:

- `uv run pytest -q` passes with no network and no database (`654 passed, 30 skipped` after Task 6) and `DATABASE_URL=... uv run pytest -q` passes with the compose database (`684 passed`, no skips); ruff and pre-commit clean; CI green; `docker compose build` succeeds; `pyproject.toml` and `uv.lock` carry `psycopg[binary]` and nothing else new.
- `uv run issuebot validate` on the committed `WORKFLOW.md` prints twelve checks; with `DATABASE_URL` pointing at the compose database the line warns before `issuebot migrate` and reads `connected (PostgreSQL 18.x); schema version 1` after it.
- The live check (Task 8) shows: `migrate` applied `0001_initial`; the worker's first tick wrote the snapshot and the sweep's issues; `issuebot refresh` produced `db_refresh_received` and an early tick; a `todo` issue reached `review` with its `runs`, `events` and `issues` rows; merging PR #4 made `stats` count #3 as closed; SIGTERM closed the sink with `failed=0 dropped=0` and the restart preserved the history with a fresh snapshot within one tick.
- `CLAUDE.md`, `README.md`, `compose.yaml`, the roadmap, the Phase 1 and Phase 4 specs and the dot-env example carry the Task 7 edits.
