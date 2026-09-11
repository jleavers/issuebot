# One Dashboard For Every Repository Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One shared PostgreSQL and one dashboard for every repository issuebot works on, with a dropdown to switch between them and the old per-repository databases imported.

**Architecture:** Migration `0003` stamps every table with a `repo` column and adds a `repos` registry that workers write at startup. `PostgresStore` and `RefreshListener` take the repository; `Queries` splits into global reads and a `RepoQueries` bound to one repository. The web is configured by `DATABASE_URL` alone, scopes every page under `/r/{owner}/{name}/`, and draws the registry as a `<select>` in the header. Compose profiles split the hub (`db`, `web`) from the per-checkout `worker`, which meet on an external network. `issuebot import` copies a version-2 database into the hub.

**Tech Stack:** Python 3.14, `uv`, psycopg 3 (async), FastAPI + Jinja2, htmx 2 (vendored), pytest (async tests via the project's existing config), Docker Compose profiles.

**Spec:** `docs/superpowers/specs/2026-09-10-single-dashboard-design.md`

## Global Constraints

- Run tests with `uv run pytest`; database tests need `DATABASE_URL` pointing at a throwaway `test-db` (see CLAUDE.md: `docker compose --profile test up -d --wait test-db`, then `DATABASE_URL=postgresql://issuebot:issuebot@$(docker compose port test-db 5432)/issuebot uv run pytest`). Never point them at the long-lived `db`.
- Lint before every commit: `uv run ruff check . && uv run ruff format --check .`. Pre-commit runs on commit (whitespace, yaml, ruff).
- Python style as the codebase has it: frozen `kw_only` `slots` dataclasses for rows, module docstrings, keyword-only constructor options, structlog event names in `snake_case`, error messages never carrying the database URL (pass through `redact`).
- The repository name is `owner/name` exactly as `github.repo` (`RepoName` in `settings.py`: `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`).
- Migration `0003_repos` is the only new migration; schema version becomes 3. Every new `repo` column is `text NOT NULL` with no default.
- The page prefix is `/r/{owner}/{name}`; the API prefix is `/api/v1/repos/{owner}/{name}`; the list is `/api/v1/repos`; the cookie is `issuebot-repo`.
- The web takes `--bind` (default `0.0.0.0`) and `--port` (default `8080`) and loads no workflow. `ServerSettings` is removed from `Settings`.
- Commit messages end with the attribution lines from the session (`Co-Authored-By` and `Claude-Session`). Never push to `main`; the work is on branch `issuebot/single-dashboard`.
- `docs/superpowers/specs/2026-09-10-single-dashboard-design.md` is the authority; when this plan and the spec disagree, follow the spec and note the difference in the commit message.

---

## File structure

**Created**

- `src/issuebot/db/migrations/0003_repos.sql` — the guard, the `repos` table, the `repo` columns, the new `runtime_snapshot`.
- `src/issuebot/db/importer.py` — `import_repo(source_url, target_url, *, repo, labels, workflow_path, connect)` and `ImportResult`; the version-2 read SQL lives here and nowhere else.
- `src/issuebot/web/templates/no-repos.html` — the root page when the registry is empty.
- `tests/test_db_importer.py` — the import round trip and its refusals.

**Modified**

- `src/issuebot/db/errors.py` — `ImportRefused(DatabaseError)`.
- `src/issuebot/db/store.py` — `PostgresStore(url, *, repo, labels, connect)`; every statement carries `repo`; `REGISTER_REPO`.
- `src/issuebot/db/queries.py` — `RepoRow`; explicit column lists; `Queries` (global: `repos`, `repo`, `snapshots`, `scoped`) and `RepoQueries` (the fourteen scoped reads).
- `src/issuebot/db/listen.py` — `RefreshListener(..., repo=)`, payload filtering, `REPO_PAYLOAD`.
- `src/issuebot/db/database.py` — `register_repo`, `store(labels, repo)`, `listener(on_notify, *, repo)`, `notify_refresh(repo)`, `import_from`.
- `src/issuebot/db/__init__.py` — new exports.
- `src/issuebot/config/settings.py`, `src/issuebot/config/__init__.py` — `ServerSettings` removed.
- `src/issuebot/cli.py` — registration at startup, scoped `status`/`stats`, `refresh` payload, `import`, `web` without a workflow.
- `src/issuebot/web/views.py` — `RepoContext`, `repo_base`, `switch_target`, `worst_status`, `repo_labels`; `issue_filters` and `turn_url` take the base.
- `src/issuebot/web/app.py` — the prefixed routes, the root redirect, `/api/v1/repos`, the health map; `create_app(database, *, clock, now)`.
- `src/issuebot/web/templates/*.html` — links through `repo.base`; the header `<select>`.
- `src/issuebot/web/static/app.js`, `app.css` — the switch handler, the stats URL from a data attribute, the select's style.
- `compose.yaml`, `.env.example`, `.github/workflows/ci.yml` — profiles, the external network, the compose config check.
- `README.md`, `CLAUDE.md`, `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md` — the hub story.
- Tests: `tests/conftest.py` (a second schema fixture), `tests/fakes/database.py`, `tests/fakes/web.py`, `tests/test_db_migrate.py`, `tests/test_db_store.py`, `tests/test_db_queries.py`, `tests/test_db_listen.py`, `tests/test_db_database.py`, `tests/test_web_app.py`, `tests/test_web_pages.py`, `tests/test_web_app_db.py`, `tests/test_cli.py`.

---

### Task 1: The persistence layer at schema version 3

Migration, store and queries change together: once `0003` runs, `SELECT *` on `issues` returns a `repo` column the row types do not have, and the old `runtime_snapshot` write no longer matches the table, so the three cannot be green separately. One task, one commit.

**Files:**
- Create: `src/issuebot/db/migrations/0003_repos.sql`
- Modify: `src/issuebot/db/store.py`, `src/issuebot/db/queries.py`, `src/issuebot/db/database.py`, `src/issuebot/db/__init__.py`
- Test: `tests/test_db_migrate.py`, `tests/test_db_store.py`, `tests/test_db_queries.py`, `tests/test_db_database.py`, `tests/test_web_app_db.py` (constructor calls only), `tests/fakes/database.py`

**Interfaces:**
- Produces: `PostgresStore(url, *, repo: str, labels: GitHubLabels, connect=connect)`; `Database.store(labels, repo)`; `Database.register_repo(repo, labels, workflow_path) -> None`; `Queries.repos() -> list[RepoRow]`, `Queries.repo(name) -> RepoRow | None`, `Queries.snapshots() -> dict[str, SnapshotRow]`, `Queries.scoped(repo) -> RepoQueries`; `RepoQueries` with the fourteen existing read methods, same signatures and return types; `RepoRow(repo, labels: dict[str, Any], workflow_path: str | None, registered_at, seen_at)`; `REGISTER_REPO` SQL in `store.py`.

- [ ] **Step 1: Write the migration**

`src/issuebot/db/migrations/0003_repos.sql`:

```sql
-- One dashboard for every repository (spec 2026-09-10 §3): a repos registry and a repo
-- column on every table. The guard first: these columns are NOT NULL with no default, and a
-- migration cannot know which repository existing rows belong to, so a database that holds
-- any refuses the upgrade and points at the import command. runtime_snapshot is not part of
-- the guard: its one row is replaced by the new table, not stamped.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM issues)
       OR EXISTS (SELECT 1 FROM runs)
       OR EXISTS (SELECT 1 FROM events) THEN
        RAISE EXCEPTION 'issues, runs or events already hold rows and 0003 cannot tell which repository they belong to; give the hub a fresh database and copy these in with `issuebot import`';
    END IF;
END $$;

CREATE TABLE repos (
    repo          text PRIMARY KEY,      -- owner/name, as github.repo
    labels        jsonb NOT NULL,        -- GitHubLabels.model_dump(): five roles + no_fault
    workflow_path text,                  -- the worker's, for the operator's orientation
    registered_at timestamptz NOT NULL,  -- first registration
    seen_at       timestamptz NOT NULL   -- latest registration
);

ALTER TABLE issues ADD COLUMN repo text NOT NULL;
ALTER TABLE issues DROP CONSTRAINT issues_pkey;
ALTER TABLE issues ADD PRIMARY KEY (repo, number);
DROP INDEX issues_state_idx;
DROP INDEX issues_closed_at_idx;
CREATE INDEX issues_state_idx ON issues (repo, state, updated_at DESC);
CREATE INDEX issues_closed_at_idx ON issues (repo, closed_at) WHERE closed_at IS NOT NULL;

-- run_id stays the primary key: a second-resolution stamp plus six hex digits, unique enough
-- across repositories, and run_turns references it.
ALTER TABLE runs ADD COLUMN repo text NOT NULL;
DROP INDEX runs_started_at_idx;
DROP INDEX runs_issue_idx;
CREATE INDEX runs_started_at_idx ON runs (repo, started_at DESC);
CREATE INDEX runs_issue_idx ON runs (repo, issue_number, started_at DESC);

ALTER TABLE events ADD COLUMN repo text NOT NULL;
DROP INDEX events_at_idx;
DROP INDEX events_issue_idx;
CREATE INDEX events_at_idx ON events (repo, at DESC);
CREATE INDEX events_issue_idx ON events (repo, issue_number, at DESC);

-- One row per worker, keyed by its repository, in place of the one-row table.
DROP TABLE runtime_snapshot;
CREATE TABLE runtime_snapshot (
    repo       text PRIMARY KEY,
    at         timestamptz NOT NULL,    -- RuntimeSnapshot.at
    written_at timestamptz NOT NULL,    -- now() at the write; the dashboard's snapshot age
    data       jsonb NOT NULL           -- RuntimeSnapshot.to_dict()
);
```

- [ ] **Step 2: Update the migration tests**

In `tests/test_db_migrate.py`, change `TABLES` and the discovery test, and add the guard tests at the end of the file:

```python
TABLES = {"issues", "runs", "events", "runtime_snapshot", "run_turns", "repos", "schema_migrations"}


def test_the_package_ships_the_three_migrations() -> None:
    migrations = discover_migrations()
    assert [m.label for m in migrations] == ["0001_initial", "0002_run_turns", "0003_repos"]
    assert [m.version for m in migrations] == [1, 2, 3]
    assert "CREATE TABLE issues" in migrations[0].sql
    assert "CREATE TABLE runtime_snapshot" in migrations[0].sql
    assert "CREATE TABLE run_turns" in migrations[1].sql
    assert "CREATE TABLE repos" in migrations[2].sql
```

```python
async def _at_version_two(db_url: str) -> psycopg.AsyncConnection:
    """A connection to a schema migrated to version 2 only (the pre-hub shape)."""
    conn = await connect(db_url)
    await apply_migrations(conn, discover_migrations()[:2])
    return conn


ISSUE_V2 = """
INSERT INTO issues (number, identifier, title, github_state, url, created_at, updated_at, seen_at)
VALUES (7, 'repo-7', 'Old', 'open', 'https://github.com/x/y/issues/7', now(), now(), now())
"""


async def test_0003_refuses_a_database_that_holds_rows(db_url: str) -> None:
    conn = await _at_version_two(db_url)
    try:
        await conn.execute(ISSUE_V2)
    finally:
        await conn.close()
    with pytest.raises(MigrationError, match="copy these in with `issuebot import`"):
        await migrate(db_url)
    conn = await connect(db_url)
    try:
        assert await schema_version(conn) == 2  # the transaction rolled back
    finally:
        await conn.close()


async def test_0003_ignores_a_stored_snapshot(db_url: str) -> None:
    conn = await _at_version_two(db_url)
    try:
        await conn.execute(
            "INSERT INTO runtime_snapshot (id, at, written_at, data) VALUES (true, now(), now(), '{}')"
        )
    finally:
        await conn.close()
    result = await migrate(db_url)
    assert result.version == 3 and result.applied == ("0003_repos",)
    conn = await connect(db_url)
    try:
        columns = await (
            await conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'runtime_snapshot' ORDER BY ordinal_position"
            )
        ).fetchall()
        assert [c[0] for c in columns] == ["repo", "at", "written_at", "data"]
        assert (await (await conn.execute("SELECT count(*) FROM runtime_snapshot")).fetchone())[
            0
        ] == 0
    finally:
        await conn.close()
```

Also fix any existing test in the file that asserts `version == 2` or `["0001_initial", "0002_run_turns"]` (`test_migrate_applies_every_migration_once` lists the applied labels: add `"0003_repos"` and version 3).

- [ ] **Step 3: Run the migration tests**

Run: `DATABASE_URL=... uv run pytest tests/test_db_migrate.py -v`
Expected: the new tests PASS; `tests/test_db_store.py`, `tests/test_db_queries.py` and `tests/test_web_app_db.py` are red until steps 4 to 8 land (the store writes no `repo`).

- [ ] **Step 4: Give the store its repository**

In `src/issuebot/db/store.py`:

Add `repo` to every statement. The parameter is `%(repo)s` everywhere:

```python
INSERT_EVENT = """
INSERT INTO events (repo, at, kind, issue_number, run_id, payload)
VALUES (%(repo)s, %(at)s, %(kind)s, %(issue_number)s, %(run_id)s, %(payload)s)
"""

RUN_STARTED = """
INSERT INTO runs (repo, run_id, issue_number, issue_identifier, attempt, session_id, started_at,
                  workspace_path)
VALUES (%(repo)s, %(run_id)s, %(issue_number)s, %(issue_identifier)s, %(attempt)s, %(session_id)s,
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
INSERT INTO runs (repo, run_id, issue_number, issue_identifier, started_at, ended_at, outcome,
                  error, turns, input_tokens, output_tokens, cost_usd, duration_s, log_dir)
VALUES (%(repo)s, %(run_id)s, %(issue_number)s, %(issue_identifier)s, %(started_at)s, %(ended_at)s,
        %(outcome)s, %(error)s, %(turns)s, %(input_tokens)s, %(output_tokens)s, %(cost_usd)s,
        %(duration_s)s, %(log_dir)s)
ON CONFLICT (run_id) DO UPDATE SET
    ... (unchanged)
"""

STATE_CHANGED = """
UPDATE issues SET state = %(state)s, state_label = %(state_label)s, seen_at = %(at)s
WHERE repo = %(repo)s AND number = %(number)s AND seen_at <= %(at)s
"""

ISSUE_CLOSED = """
UPDATE issues
SET github_state = 'closed', closed_at = coalesce(closed_at, %(at)s), seen_at = %(at)s
WHERE repo = %(repo)s AND number = %(number)s AND seen_at <= %(at)s
"""

UPSERT_ISSUE = """
INSERT INTO issues (repo, number, identifier, title, state, state_label, github_state, url, labels,
                    pr_number, pr_url, pr_state, pr_merged_at, created_at, updated_at, closed_at,
                    seen_at)
VALUES (%(repo)s, %(number)s, %(identifier)s, %(title)s, %(state)s, %(state_label)s,
        %(github_state)s, %(url)s, %(labels)s, %(pr_number)s, %(pr_url)s, %(pr_state)s,
        %(pr_merged_at)s, %(created_at)s, %(updated_at)s, %(closed_at)s, %(seen_at)s)
ON CONFLICT (repo, number) DO UPDATE SET
    ... (unchanged)
WHERE issues.seen_at <= EXCLUDED.seen_at
"""

WRITE_SNAPSHOT = """
INSERT INTO runtime_snapshot (repo, at, written_at, data)
VALUES (%(repo)s, %(at)s, now(), %(data)s)
ON CONFLICT (repo) DO UPDATE SET at = EXCLUDED.at, written_at = now(), data = EXCLUDED.data
"""

REGISTER_REPO = """
INSERT INTO repos (repo, labels, workflow_path, registered_at, seen_at)
VALUES (%(repo)s, %(labels)s, %(workflow_path)s, now(), now())
ON CONFLICT (repo) DO UPDATE SET
    labels = EXCLUDED.labels,
    workflow_path = EXCLUDED.workflow_path,
    seen_at = now()
"""
```

`INSERT_TURN` is unchanged (`run_turns` has no `repo`).

The class:

```python
class PostgresStore:
    """Writes events, runs, issues and the runtime snapshot over one autocommit connection,
    every row stamped with the worker's repository."""

    def __init__(
        self, url: str, *, repo: str, labels: GitHubLabels, connect: Connector = connect
    ) -> None:
        self._url = url
        self._repo = repo
        self._labels = labels
        self._connect = connect
        self._conn: AsyncConnection | None = None

    @property
    def repo(self) -> str:
        return self._repo
```

Every `execute`/`executemany` gets the repo merged into its parameters. The cleanest way is one helper:

```
    def _stamp(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"repo": self._repo, **row}
```

and then in `apply_event`: `self._stamp(event_row(event))`, `self._stamp(run_started_row(event))`, `self._stamp(run_ended_row(event))`, `self._stamp(self._state_changed_row(event))`, `self._stamp({"number": event.issue_number, "at": event.at})`; in `upsert_issues`: `[self._stamp(issue_row(s)) for s in issues]`; in `write_snapshot`: `self._stamp({"at": at, "data": Jsonb(dict(data))})`. The turn rows are not stamped.

- [ ] **Step 5: Update the store tests**

In `tests/test_db_store.py`, the fixture:

```python
REPO = "example/repo"


@pytest.fixture
async def store(db_url: str) -> AsyncIterator[PostgresStore]:
    await migrate(db_url)
    store = PostgresStore(db_url, repo=REPO, labels=GitHubLabels())
    await store.connect()
    try:
        yield store
    finally:
        await store.close()
```

Every other `PostgresStore(...)` construction in the file (there are some in `test_writes_need_a_connection`, `test_a_missing_table_is_a_store_error`, `test_connect_failure_is_unavailable_and_redacted`) gains `repo=REPO`. Replace `test_write_snapshot_keeps_exactly_one_row` with:

```python
async def test_write_snapshot_keeps_one_row_per_repository(
    store: PostgresStore, db_url: str
) -> None:
    await store.write_snapshot(at(0), {"tick_count": 1})
    await store.write_snapshot(at(5), {"tick_count": 2})
    other = PostgresStore(db_url, repo="example/other", labels=GitHubLabels())
    await other.connect()
    try:
        await other.write_snapshot(at(1), {"tick_count": 9})
    finally:
        await other.close()
    found = await rows(db_url, "SELECT repo, at, data FROM runtime_snapshot ORDER BY repo")
    assert [(r["repo"], r["at"], r["data"]["tick_count"]) for r in found] == [
        ("example/other", at(1), 9),
        (REPO, at(5), 2),
    ]
```

Add:

```python
async def test_every_write_carries_the_repository(
    store: PostgresStore, db_url: str, make_issue: Callable[..., Issue]
) -> None:
    await store.apply_event(started())
    await store.apply_event(ended())
    await store.upsert_issues([snapshot(make_issue(), 0)])  # the file's IssueSnapshot helper
    assert {r["repo"] for r in await rows(db_url, "SELECT repo FROM events")} == {REPO}
    assert {r["repo"] for r in await rows(db_url, "SELECT repo FROM runs")} == {REPO}
    assert {r["repo"] for r in await rows(db_url, "SELECT repo FROM issues")} == {REPO}


async def test_two_repositories_share_an_issue_number_without_colliding(
    store: PostgresStore, db_url: str, make_issue: Callable[..., Issue]
) -> None:
    other = PostgresStore(db_url, repo="example/other", labels=GitHubLabels())
    await other.connect()
    try:
        await store.upsert_issues([IssueSnapshot(issue=make_issue(title="Ours"), seen_at=at(0))])
        await other.upsert_issues([IssueSnapshot(issue=make_issue(title="Theirs"), seen_at=at(0))])
        await other.apply_event(
            StateChanged(
                issue_number=42,
                issue_identifier="repo-42",
                from_label="issuebot/todo",
                to_label="issuebot/review",
                actor="agent",
                pr_url=None,
                at=at(1),
            )
        )
    finally:
        await other.close()
    found = await rows(db_url, "SELECT repo, title, state FROM issues ORDER BY repo")
    assert [(r["repo"], r["title"], r["state"]) for r in found] == [
        ("example/other", "Theirs", "review"),
        (REPO, "Ours", "todo"),
    ]
```

(`make_issue` is the conftest fixture and `snapshot(issue, seconds)` is the file's own `IssueSnapshot` helper. The point of the second test is that the other repository's `StateChanged` does not touch ours.)

- [ ] **Step 6: Split the queries**

In `src/issuebot/db/queries.py`:

Add the row type and the column lists after `TurnRow`:

```python
@dataclass(frozen=True, kw_only=True, slots=True)
class RepoRow:
    """One registered worker: what the dropdown lists and where the board's labels come from.
    ``labels`` is the stored mapping; the web validates it per request (spec §7, §8)."""

    repo: str
    labels: dict[str, Any]
    workflow_path: str | None
    registered_at: datetime
    seen_at: datetime


# Explicit column lists rather than SELECT *: every table now carries a repo column the
# row types do not, and the repository is in the request, not the row.
ISSUE_COLUMNS = ", ".join(f.name for f in fields(IssueRow))
RUN_COLUMNS = ", ".join(f.name for f in fields(RunRow))
EVENT_COLUMNS = ", ".join(f.name for f in fields(EventRow))
SUMMARY_COLUMNS = ", ".join(f"t.{f.name}" for f in fields(TurnSummaryRow))
```

Rewrite the SQL so every per-repository statement carries `repo = %(repo)s`:

```python
CLOSED_COUNT = """
SELECT count(*) AS n FROM issues
WHERE repo = %(repo)s AND state = 'complete' AND closed_at >= now() - %(window)s
"""

RUNS_COUNT = """
SELECT count(*) AS n FROM runs WHERE repo = %(repo)s AND started_at >= now() - %(window)s
"""

RUN_TOTALS = """
SELECT coalesce(sum(input_tokens), 0) AS input_tokens,
       coalesce(sum(output_tokens), 0) AS output_tokens,
       coalesce(sum(cost_usd), 0) AS cost_usd
FROM runs WHERE repo = %(repo)s AND started_at >= now() - %(window)s
"""

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
        WHERE repo = %(repo)s AND state = 'complete'
          AND closed_at >= day AND closed_at < day + interval '1 day') AS closed,
       (SELECT count(*) FROM runs
        WHERE repo = %(repo)s AND started_at >= day AND started_at < day + interval '1 day')
           AS runs
FROM days ORDER BY day
"""

OPEN_ISSUES = f"""
SELECT {ISSUE_COLUMNS} FROM issues
WHERE repo = %(repo)s AND github_state = 'open' AND state IS NOT NULL
ORDER BY updated_at DESC, number DESC
"""

COMPLETE_ISSUES = f"""
SELECT {ISSUE_COLUMNS} FROM issues
WHERE repo = %(repo)s AND github_state = 'closed' AND state = 'complete'
ORDER BY closed_at DESC NULLS LAST, number DESC LIMIT %(limit)s
"""

ISSUE_LIST = f"""
SELECT {ISSUE_COLUMNS} FROM issues
WHERE repo = %(repo)s AND state = ANY(%(roles)s) AND (github_state = 'open' OR state = 'complete')
ORDER BY coalesce(closed_at, updated_at) DESC, number DESC LIMIT %(limit)s
"""

RUNS_FOR_ISSUE = f"""
SELECT {RUN_COLUMNS} FROM runs WHERE repo = %(repo)s AND issue_number = %(number)s
ORDER BY started_at DESC, run_id DESC
"""

RECENT_EVENTS = f"""
SELECT {EVENT_COLUMNS} FROM events WHERE repo = %(repo)s ORDER BY id DESC LIMIT %(limit)s
"""

SNAPSHOT = "SELECT at, written_at, data FROM runtime_snapshot WHERE repo = %(repo)s"

SNAPSHOTS = "SELECT repo, at, written_at, data FROM runtime_snapshot"

REPOS = "SELECT repo, labels, workflow_path, registered_at, seen_at FROM repos ORDER BY repo"

REPO = "SELECT repo, labels, workflow_path, registered_at, seen_at FROM repos WHERE repo = %(repo)s"

ISSUE = f"SELECT {ISSUE_COLUMNS} FROM issues WHERE repo = %(repo)s AND number = %(number)s"

EVENTS_FOR_ISSUE = f"""
SELECT {EVENT_COLUMNS} FROM events WHERE repo = %(repo)s AND issue_number = %(number)s
ORDER BY id DESC LIMIT %(limit)s
"""

TURN_SUMMARIES_FOR_ISSUE = f"""
SELECT {SUMMARY_COLUMNS} FROM run_turns t JOIN runs r ON r.run_id = t.run_id
WHERE r.repo = %(repo)s AND r.issue_number = %(number)s
ORDER BY r.started_at DESC, r.run_id DESC, t.turn_number
"""

# run_turns has no repo column of its own, so the join to runs is what scopes it.
TURN = """
SELECT t.* FROM run_turns t JOIN runs r ON r.run_id = t.run_id
WHERE r.repo = %(repo)s AND t.run_id = %(run_id)s AND t.turn_number = %(turn_number)s
"""

STATE_COUNTS = """
SELECT state, count(*) AS n FROM issues
WHERE repo = %(repo)s AND state IS NOT NULL AND (github_state = 'open' OR state = 'complete')
GROUP BY state
"""
```

The classes:

```python
class _Reader:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def _count(self, query: str, params: dict[str, Any]) -> int:
        rows = await self._rows(query, params)
        return int(rows[0]["n"])

    async def _rows(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchall()


class Queries(_Reader):
    """The repository-free reads over one connection, and the way to a scoped view."""

    async def repos(self) -> list[RepoRow]:
        """Every registered worker, by name."""
        return [RepoRow(**row) for row in await self._rows(REPOS)]

    async def repo(self, name: str) -> RepoRow | None:
        rows = await self._rows(REPO, {"repo": name})
        return RepoRow(**rows[0]) if rows else None

    async def snapshots(self) -> dict[str, SnapshotRow]:
        """Every worker's latest snapshot, keyed by repository (for /healthz)."""
        return {
            row["repo"]: SnapshotRow(at=row["at"], written_at=row["written_at"], data=row["data"])
            for row in await self._rows(SNAPSHOTS)
        }

    def scoped(self, repo: str) -> RepoQueries:
        """The same connection with ``repo`` bound into every predicate."""
        return RepoQueries(self._conn, repo)


class RepoQueries(_Reader):
    """Read-only queries for one repository; every method is one round trip or two.

    Binding the repository into the object rather than adding a parameter to every method
    means a handler cannot forget the predicate.
    """

    def __init__(self, conn: AsyncConnection, repo: str) -> None:
        super().__init__(conn)
        self.repo = repo

    def _params(self, **params: Any) -> dict[str, Any]:
        return {"repo": self.repo, **params}
```

Then move the fourteen existing methods (`closed_count` ... `snapshot`) onto `RepoQueries` unchanged except that every `self._rows(X, {...})` / `self._count(X, {...})` call passes `self._params(...)` instead of a bare dict, and the calls with no parameters (`OPEN_ISSUES`, `STATE_COUNTS`, `SNAPSHOT`) pass `self._params()`.

- [ ] **Step 7: The facade**

In `src/issuebot/db/database.py`:

```python
from psycopg.types.json import Jsonb

from issuebot.db.store import REGISTER_REPO, PostgresStore
```

```
    async def register_repo(
        self, repo: str, labels: GitHubLabels, workflow_path: str | None
    ) -> None:
        """Upsert the worker's row in ``repos``: what the dashboard lists and lays out by.

        A one-shot write before the sinks start, so a failure is a startup failure, not a
        queued item that could be dropped (spec §5).
        """
        async with self._open() as conn:
            await conn.execute(
                REGISTER_REPO,
                {"repo": repo, "labels": Jsonb(labels.model_dump()), "workflow_path": workflow_path},
            )

    def store(self, labels: GitHubLabels, repo: str) -> PostgresStore:
        return PostgresStore(self._url, repo=repo, labels=labels, connect=self._connect)
```

Leave `listener` and `notify_refresh` for Task 2.

In `src/issuebot/db/__init__.py` export `RepoQueries` and `RepoRow` (add to the import from `issuebot.db.queries` and to `__all__`, alphabetically).

- [ ] **Step 8: Update the query, database and web-db tests and the fake**

`tests/test_db_queries.py`: the `seeded` fixture builds `PostgresStore(db_url, repo=REPO, labels=GitHubLabels())` with `REPO = "example/repo"` at module level, and every test that did `async with seeded.queries() as queries:` then called a scoped method now does `queries = base.scoped(REPO)` first. The simplest edit is a helper at the top of the file:

```python
@asynccontextmanager
async def scoped(database: Database, repo: str = REPO) -> AsyncIterator[RepoQueries]:
    async with database.queries() as queries:
        yield queries.scoped(repo)
```

and `async with scoped(seeded) as queries:` in place of `async with seeded.queries() as queries:` throughout. Add at the end:

```python
async def test_scoped_reads_see_only_their_own_repository(
    seeded: Database, db_url: str, make_issue: Callable[..., Issue]
) -> None:
    other = PostgresStore(db_url, repo="example/other", labels=GitHubLabels())
    await other.connect()
    try:
        await other.upsert_issues(
            [IssueSnapshot(issue=make_issue(number=1, title="Elsewhere"), seen_at=NOW)]
        )
        await other.apply_event(
            RunStarted(
                issue_number=1,
                issue_identifier="repo-1",
                run_id="other-run",
                attempt=1,
                session_id="s",
                workspace_path="/w",
                at=NOW,
            )
        )
        await other.write_snapshot(NOW, {"tick_count": 99})
    finally:
        await other.close()
    async with scoped(seeded, "example/other") as theirs, scoped(seeded) as ours:
        assert [row.title for row in await theirs.issues_for_state(None)] == ["Elsewhere"]
        assert "Elsewhere" not in [row.title for row in await ours.issues_for_state(None)]
        assert [run.run_id for run in await theirs.runs_for_issue(1)] == ["other-run"]
        assert await theirs.runs_count(timedelta(days=1)) == 1
        assert "other-run" not in [run.run_id for run in await ours.runs_for_issue(1)]
        assert (await theirs.snapshot()).data == {"tick_count": 99}  # type: ignore[union-attr]
        assert await ours.turn("other-run", 1) is None


async def test_repos_lists_registrations_by_name_and_snapshots_by_repo(
    seeded: Database, db_url: str
) -> None:
    await seeded.register_repo("zeta/last", GitHubLabels(), "/configs/z.md")
    await seeded.register_repo("alpha/first", GitHubLabels(review="issuebot/check"), None)
    await seeded.register_repo("zeta/last", GitHubLabels(), "/configs/z2.md")  # re-register
    async with seeded.queries() as queries:
        rows = await queries.repos()
        assert [(r.repo, r.workflow_path) for r in rows] == [
            ("alpha/first", None),
            ("zeta/last", "/configs/z2.md"),
        ]
        assert rows[0].labels["review"] == "issuebot/check"
        assert rows[1].registered_at <= rows[1].seen_at
        assert await queries.repo("nobody/here") is None
        assert (await queries.repo("alpha/first")).labels["todo"] == "issuebot/todo"  # type: ignore[union-attr]
        snapshots = await queries.snapshots()
        assert set(snapshots) == {REPO}  # the seeded fixture wrote one for REPO only
```

(Where the `seeded` fixture writes no snapshot, drop the last two lines or write one first; keep the assertion truthful to what the fixture does.)

`tests/test_web_app_db.py`: the `seeded` fixture's store gains `repo="example/repo"`; leave everything else for Task 5, which reshapes the routes (that file is red until then; say so in the commit message).

`tests/test_db_database.py::test_store_and_listener_are_built_with_the_url`: `database.store(GitHubLabels(), "example/repo")` and assert `store.repo == "example/repo"`.

`tests/fakes/database.py`: `FakeStore` unchanged. `FakeDatabase.store(self, labels, repo)` records `self.labels = labels` and `self.repo = repo`. Add:

```
        self.registrations: list[tuple[str, GitHubLabels, str | None]] = []
        self.register_error: DatabaseError | None = None

    async def register_repo(self, repo: str, labels: GitHubLabels, workflow_path: str | None) -> None:
        if self.register_error is not None:
            raise self.register_error
        self.registrations.append((repo, labels, workflow_path))
```

`FakeQueries` gains the global reads and a `scoped` that records and returns itself, so the fourteen canned methods keep working for the web tests:

```
        self.repo_rows: dict[str, RepoRow] = {}
        self.snapshot_rows: dict[str, SnapshotRow] = {}  # per repo; snapshot_row is the default
        self.scoped_repos: list[str] = []
        self._repo: str | None = None

    async def repos(self) -> list[RepoRow]:
        self._check("repos")
        return [self.repo_rows[name] for name in sorted(self.repo_rows)]

    async def repo(self, name: str) -> RepoRow | None:
        self._check("repo")
        return self.repo_rows.get(name)

    async def snapshots(self) -> dict[str, SnapshotRow]:
        self._check("snapshots")
        rows = {name: self.snapshot_rows.get(name, self.snapshot_row) for name in self.repo_rows}
        return {name: row for name, row in rows.items() if row is not None}

    def scoped(self, repo: str) -> FakeQueries:
        self.scoped_repos.append(repo)
        self._repo = repo
        return self

    async def snapshot(self) -> SnapshotRow | None:
        self._check("snapshot")
        if self._repo is not None and self._repo in self.snapshot_rows:
            return self.snapshot_rows[self._repo]
        return self.snapshot_row
```

- [ ] **Step 9: Run the database suite and lint**

Run: `DATABASE_URL=... uv run pytest tests/test_db_migrate.py tests/test_db_store.py tests/test_db_queries.py tests/test_db_database.py tests/test_db_sink.py -v`
Expected: PASS. Then `uv run pytest` (the whole suite): `tests/test_web_app_db.py`, `tests/test_web_*.py` and the CLI's database tests are expected red (they still build `PostgresStore`/`create_app`/`store(labels)` the old way); everything else green. `uv run ruff check . && uv run ruff format --check .` clean.

- [ ] **Step 10: Commit**

```bash
git add src/issuebot/db tests/test_db_migrate.py tests/test_db_store.py tests/test_db_queries.py tests/test_db_database.py tests/test_web_app_db.py tests/fakes/database.py
git commit -m "feat(db): schema version 3 with a repo column on every table and a repos registry

Migration 0003 refuses a database that already holds rows and points at
the import command. PostgresStore stamps every row with its repository,
Queries splits into the global reads and a RepoQueries bound to one
repository, and Database.register_repo is the worker's startup write.

The web and the CLI still speak version 2 and are red until the next
tasks land."
```

---

### Task 2: The refresh channel carries the repository

**Files:**
- Modify: `src/issuebot/db/listen.py`, `src/issuebot/db/database.py`
- Test: `tests/test_db_listen.py`, `tests/test_db_database.py`, `tests/fakes/database.py`

**Interfaces:**
- Consumes: `Database._open`, `REFRESH_CHANNEL`.
- Produces: `RefreshListener(url, on_notify, *, repo: str | None = None, connect=, sleep=)`; `Database.listener(on_notify, *, repo: str | None = None)`; `Database.notify_refresh(repo: str | None = None)`; `REPO_PAYLOAD` regex in `listen.py`; log events `db_refresh_other_repo` (debug) and `refresh_payload_ignored` (warning).

- [ ] **Step 1: Write the failing listener tests**

Append to `tests/test_db_listen.py`:

```python
def notify(payload: str) -> FakeNotify:
    item = FakeNotify(REFRESH_CHANNEL)
    item.payload = payload
    return item


async def test_a_scoped_listener_fires_on_its_repo_and_on_an_empty_payload() -> None:
    h = Harness()
    h.listener = RefreshListener(
        URL, h.on_notify, repo="example/repo", connect=h.connect, sleep=h.sleep
    )
    h.listener.start()
    await h.settle()
    (conn,) = h.connections
    conn.feed.put_nowait(notify("example/repo"))
    conn.feed.put_nowait(notify(""))
    conn.feed.put_nowait(notify("example/other"))
    conn.feed.put_nowait(notify("not a repo!"))
    await h.settle()
    assert h.calls == 2
    assert h.listener.notified == 4  # every notification is counted, two were filtered
    assert len(h.logged("db_refresh_other_repo")) == 1
    (ignored,) = h.logged("refresh_payload_ignored")
    assert ignored["payload"] == "not a repo!"
    await h.listener.close()


async def test_an_unscoped_listener_fires_on_every_payload(h: Harness) -> None:
    h.listener.start()
    await h.settle()
    (conn,) = h.connections
    conn.feed.put_nowait(notify("example/other"))
    conn.feed.put_nowait(notify("anything"))
    await h.settle()
    assert h.calls == 2
    await h.listener.close()
```

And change `test_a_real_notify_reaches_the_callback` so it sends the payload through the facade: `await Database(db_url).notify_refresh("example/repo")` with a listener built by `Database(db_url).listener(cb, repo="example/repo")`, and a second `notify_refresh("example/other")` that must not fire it.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_db_listen.py -v`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'repo'`.

- [ ] **Step 3: Implement the filter and the payload**

`src/issuebot/db/listen.py`:

```python
import re

REFRESH_CHANNEL = "issuebot_refresh"
REPO_PAYLOAD = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")  # RepoName, settings.py
```

Constructor gains `repo: str | None = None` (keyword-only, after `on_notify`) stored as `self._repo`. In `_run`, replace the body of the `async for` loop:

```
                async for notification in conn.notifies():
                    self.notified += 1
                    payload = notification.payload or ""
                    if not self._accepts(payload):
                        continue
                    self._log.info("db_refresh_received", channel=notification.channel, payload=payload)
                    try:
                        self._on_notify()
                    except Exception:
                        self._log.exception("db_refresh_callback_failed")
```

```
    def _accepts(self, payload: str) -> bool:
        """An empty payload wakes every worker; a repository name wakes that one; anything
        else is dropped with a warning (spec §5)."""
        if self._repo is None or not payload or payload == self._repo:
            return True
        if REPO_PAYLOAD.match(payload):
            self._log.debug("db_refresh_other_repo", payload=payload, repo=self._repo)
        else:
            self._log.warning("refresh_payload_ignored", payload=payload, repo=self._repo)
        return False
```

`src/issuebot/db/database.py`:

```
    def listener(self, on_notify: Callable[[], None], *, repo: str | None = None) -> RefreshListener:
        return RefreshListener(self._url, on_notify, repo=repo, connect=self._connect)

    async def notify_refresh(self, repo: str | None = None) -> None:
        """NOTIFY the refresh channel: with a repository, that worker ticks at once; without
        one, every listening worker does."""
        async with self._open() as conn:
            await conn.execute("SELECT pg_notify(%s, %s)", (REFRESH_CHANNEL, repo or ""))
```

`tests/fakes/database.py`: `FakeListener.__init__(self, on_notify, repo)` stores `self.repo`; `FakeDatabase.listener(self, on_notify, *, repo=None)` passes it; `notify_refresh(self, repo=None)` appends to `self.notified_repos: list[str | None]` as well as incrementing `notified`.

`tests/test_db_database.py::test_store_and_listener_are_built_with_the_url`: build the listener with `repo="example/repo"` and assert `listener._repo == "example/repo"` (or expose a `repo` property; either is fine, be consistent).

- [ ] **Step 4: Run the tests**

Run: `DATABASE_URL=... uv run pytest tests/test_db_listen.py tests/test_db_database.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/db/listen.py src/issuebot/db/database.py tests/test_db_listen.py tests/test_db_database.py tests/fakes/database.py
git commit -m "feat(db): the refresh NOTIFY names the repository it is for

A scoped listener fires on its own repository's payload and on an empty
one, so a hand-typed NOTIFY still wakes every worker; another repository's
is ignored and anything else is dropped with a warning."
```

---

### Task 3: `issuebot import`

**Files:**
- Create: `src/issuebot/db/importer.py`, `tests/test_db_importer.py`
- Modify: `src/issuebot/db/errors.py`, `src/issuebot/db/database.py`, `src/issuebot/db/__init__.py`, `src/issuebot/cli.py`, `tests/conftest.py`, `tests/fakes/database.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `schema_version`, `apply_migrations`, `connect`, `classify`, `redact`, `REGISTER_REPO`, `INSERT_TURN` (the target's turn insert, reused).
- Produces: `ImportRefused(DatabaseError)`; `ImportResult(counts: dict[str, int])`; `import_repo(source_url, target_url, *, repo, labels, workflow_path, connect=connect) -> ImportResult`; `Database.import_from(source_url, *, repo, labels, workflow_path) -> ImportResult`; CLI `issuebot import --from URL [--workflow PATH]`.

- [ ] **Step 1: A second schema fixture**

In `tests/conftest.py`, factor the schema creation out so two fixtures share it:

```python
@contextmanager
def _fresh_schema() -> Iterator[str]:
    if not _DATABASE_URL:
        pytest.skip("DATABASE_URL is not set; database tests need a PostgreSQL server")
    schema = f"issuebot_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(_DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield with_search_path(_DATABASE_URL, schema)
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def db_url() -> Iterator[str]:
    """A DATABASE_URL scoped to a fresh schema; skipped when no database is configured."""
    with _fresh_schema() as url:
        yield url


@pytest.fixture
def source_db_url() -> Iterator[str]:
    """A second fresh schema on the same server: the old, single-repository database."""
    with _fresh_schema() as url:
        yield url
```

(`from contextlib import contextmanager`.)

- [ ] **Step 2: Write the failing import tests**

`tests/test_db_importer.py`:

```python
"""The one-off import of a version-2 database into the hub (needs DATABASE_URL)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from issuebot.config import GitHubLabels
from issuebot.db import Database, apply_migrations, connect, discover_migrations, migrate
from issuebot.db.errors import ImportRefused
from issuebot.db.importer import import_repo

REPO = "example/repo"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
RUN_ID = "20260904T120000Z-abc123"


async def seed_version_two(url: str) -> None:
    """A source at schema version 2 with one row in every table."""
    conn = await connect(url)
    try:
        await apply_migrations(conn, discover_migrations()[:2])
        await conn.execute(
            "INSERT INTO issues (number, identifier, title, state, state_label, github_state, url,"
            " labels, created_at, updated_at, closed_at, seen_at) VALUES (7, 'repo-7', 'Old', "
            "'complete', 'issuebot/complete', 'closed', 'https://github.com/x/y/issues/7', "
            "%s, %s, %s, %s, %s)",
            (["issuebot/complete"], NOW, NOW, NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO runs (run_id, issue_number, issue_identifier, attempt, started_at, ended_at,"
            " outcome, turns, input_tokens, output_tokens, cost_usd, duration_s, log_dir) VALUES "
            "(%s, 7, 'repo-7', 1, %s, %s, 'succeeded', 1, 100, 10, 0.5, 60.0, '/w/runs/x')",
            (RUN_ID, NOW - timedelta(minutes=1), NOW),
        )
        await conn.execute(
            "INSERT INTO run_turns (run_id, turn_number, captured_at, model, prompt, prompt_bytes,"
            " stream, stream_bytes, stream_lines, omitted_lines, stderr, stderr_bytes, truncated)"
            " VALUES (%s, 1, %s, 'opus', 'p', 1, 's', 1, 1, 0, '', 0, false)",
            (RUN_ID, NOW),
        )
        await conn.execute(
            "INSERT INTO events (at, kind, issue_number, run_id, payload) VALUES (%s, 'run_ended',"
            " 7, %s, %s)",
            (NOW, RUN_ID, Jsonb({"kind": "run_ended", "run_id": RUN_ID})),
        )
        await conn.execute(
            "INSERT INTO runtime_snapshot (id, at, written_at, data) VALUES (true, %s, %s, %s)",
            (NOW, NOW, Jsonb({"tick_count": 3})),
        )
    finally:
        await conn.close()


async def count(url: str, table: str, repo: str | None = REPO) -> int:
    conn = await connect(url)
    try:
        where = "" if repo is None else f" WHERE repo = '{repo}'"
        row = await (await conn.execute(f"SELECT count(*) FROM {table}{where}")).fetchone()
        return int(row[0]) if row else 0
    finally:
        await conn.close()


async def test_import_copies_every_table_stamped_with_the_repository(
    db_url: str, source_db_url: str
) -> None:
    await seed_version_two(source_db_url)
    result = await import_repo(
        source_db_url, db_url, repo=REPO, labels=GitHubLabels(), workflow_path="/configs/W.md"
    )
    assert result.counts == {
        "issues": 1,
        "runs": 1,
        "run_turns": 1,
        "events": 1,
        "runtime_snapshot": 1,
    }
    for table in ("issues", "runs", "events", "runtime_snapshot"):
        assert await count(db_url, table) == 1
    assert await count(db_url, "run_turns", repo=None) == 1
    database = Database(db_url)
    async with database.queries() as queries:
        (row,) = await queries.repos()
        assert (row.repo, row.workflow_path) == (REPO, "/configs/W.md")
        scoped = queries.scoped(REPO)
        turn = await scoped.turn(RUN_ID, 1)
        assert turn is not None and turn.model == "opus"
        (event,) = await scoped.recent_events(10)
        assert event.payload["run_id"] == RUN_ID
        assert (await scoped.snapshot()).data == {"tick_count": 3}  # type: ignore[union-attr]
        assert await scoped.closed_count(timedelta(days=3650)) == 1


async def test_import_refuses_a_repository_that_is_already_registered(
    db_url: str, source_db_url: str
) -> None:
    await seed_version_two(source_db_url)
    await import_repo(source_db_url, db_url, repo=REPO, labels=GitHubLabels(), workflow_path=None)
    with pytest.raises(ImportRefused, match="example/repo is already registered in the target"):
        await import_repo(
            source_db_url, db_url, repo=REPO, labels=GitHubLabels(), workflow_path=None
        )
    assert await count(db_url, "events") == 1  # nothing doubled


async def test_import_refuses_a_source_at_the_wrong_version(
    db_url: str, source_db_url: str
) -> None:
    await migrate(source_db_url)  # version 3: not what the import reads
    with pytest.raises(ImportRefused, match="source is at schema version 3, expected 2"):
        await import_repo(
            source_db_url, db_url, repo=REPO, labels=GitHubLabels(), workflow_path=None
        )


async def test_import_migrates_an_empty_target_first(db_url: str, source_db_url: str) -> None:
    await seed_version_two(source_db_url)
    conn = await connect(source_db_url)
    try:
        await conn.execute("DELETE FROM runtime_snapshot")
    finally:
        await conn.close()
    result = await import_repo(
        source_db_url, db_url, repo=REPO, labels=GitHubLabels(), workflow_path=None
    )
    assert result.counts["runtime_snapshot"] == 0
    assert await count(db_url, "issues") == 1
```

- [ ] **Step 3: Run them to see them fail**

Run: `DATABASE_URL=... uv run pytest tests/test_db_importer.py -v`
Expected: FAIL with `ModuleNotFoundError: issuebot.db.importer`.

- [ ] **Step 4: Implement the importer**

`src/issuebot/db/errors.py`:

```python
class ImportRefused(DatabaseError):
    """``issuebot import`` will not run: the source or the target is not what it expects."""
```

`src/issuebot/db/importer.py`:

```python
"""The one-off import of a version-2, single-repository database into the hub (spec §4).

The source is read with the version-2 SQL below, written here and nowhere else, because
``Queries`` speaks version 3. Rows are streamed in batches through a server-side cursor so a
large ``run_turns`` never has to fit in memory. Everything lands in one target transaction.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from issuebot.config import GitHubLabels
from issuebot.db.connection import Connector, connect, error_text, redact
from issuebot.db.errors import ImportRefused
from issuebot.db.migrate import apply_migrations, schema_version
from issuebot.db.store import INSERT_TURN, REGISTER_REPO

SOURCE_VERSION = 2
BATCH = 500

READ_ISSUES = """
SELECT number, identifier, title, state, state_label, github_state, url, labels, pr_number,
       pr_url, pr_state, pr_merged_at, created_at, updated_at, closed_at, seen_at
FROM issues ORDER BY number
"""
WRITE_ISSUE = """
INSERT INTO issues (repo, number, identifier, title, state, state_label, github_state, url, labels,
                    pr_number, pr_url, pr_state, pr_merged_at, created_at, updated_at, closed_at,
                    seen_at)
VALUES (%(repo)s, %(number)s, %(identifier)s, %(title)s, %(state)s, %(state_label)s,
        %(github_state)s, %(url)s, %(labels)s, %(pr_number)s, %(pr_url)s, %(pr_state)s,
        %(pr_merged_at)s, %(created_at)s, %(updated_at)s, %(closed_at)s, %(seen_at)s)
"""

READ_RUNS = """
SELECT run_id, issue_number, issue_identifier, attempt, session_id, started_at, ended_at, outcome,
       error, turns, input_tokens, output_tokens, cost_usd, duration_s, workspace_path, log_dir
FROM runs ORDER BY started_at, run_id
"""
WRITE_RUN = """
INSERT INTO runs (repo, run_id, issue_number, issue_identifier, attempt, session_id, started_at,
                  ended_at, outcome, error, turns, input_tokens, output_tokens, cost_usd,
                  duration_s, workspace_path, log_dir)
VALUES (%(repo)s, %(run_id)s, %(issue_number)s, %(issue_identifier)s, %(attempt)s, %(session_id)s,
        %(started_at)s, %(ended_at)s, %(outcome)s, %(error)s, %(turns)s, %(input_tokens)s,
        %(output_tokens)s, %(cost_usd)s, %(duration_s)s, %(workspace_path)s, %(log_dir)s)
"""

READ_TURNS = """
SELECT run_id, turn_number, model, subtype, is_error, num_turns, input_tokens,
       cache_creation_input_tokens, cache_read_input_tokens, output_tokens, cost_usd, duration_ms,
       result_text, prompt, prompt_bytes, stream, stream_bytes, stream_lines, omitted_lines,
       stderr, stderr_bytes, truncated
FROM run_turns ORDER BY run_id, turn_number
"""
# INSERT_TURN (store.py) sets captured_at = now(); the original capture time is not carried,
# which the transcript page does not show for an imported run anyway.

READ_EVENTS = "SELECT at, kind, issue_number, run_id, payload FROM events ORDER BY id"
WRITE_EVENT = """
INSERT INTO events (repo, at, kind, issue_number, run_id, payload)
VALUES (%(repo)s, %(at)s, %(kind)s, %(issue_number)s, %(run_id)s, %(payload)s)
"""

READ_SNAPSHOT = "SELECT at, written_at, data FROM runtime_snapshot"
WRITE_SNAPSHOT = """
INSERT INTO runtime_snapshot (repo, at, written_at, data)
VALUES (%(repo)s, %(at)s, %(written_at)s, %(data)s)
"""

IS_REGISTERED = "SELECT 1 FROM repos WHERE repo = %(repo)s"


@dataclass(frozen=True, slots=True)
class ImportResult:
    counts: dict[str, int]  # table -> rows copied, in copy order


def _as_is(row: dict[str, Any]) -> dict[str, Any]:
    return row


def _json_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "payload": Jsonb(row["payload"])}


def _json_data(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "data": Jsonb(row["data"])}


# (table, read, write, adapt) in foreign-key order.
TABLES: tuple[tuple[str, str, str, Callable[[dict[str, Any]], dict[str, Any]]], ...] = (
    ("issues", READ_ISSUES, WRITE_ISSUE, _as_is),
    ("runs", READ_RUNS, WRITE_RUN, _as_is),
    ("run_turns", READ_TURNS, INSERT_TURN, _as_is),
    ("events", READ_EVENTS, WRITE_EVENT, _json_payload),
    ("runtime_snapshot", READ_SNAPSHOT, WRITE_SNAPSHOT, _json_data),
)


async def import_repo(
    source_url: str,
    target_url: str,
    *,
    repo: str,
    labels: GitHubLabels,
    workflow_path: str | None,
    connect: Connector = connect,
) -> ImportResult:
    """Copy a version-2 database into the hub, every row stamped with ``repo``.

    Refuses (``ImportRefused``) a source that is not at version 2 and a repository already in
    the target's registry; connection and statement failures are ``ImportRefused`` too, with
    both URLs redacted. Migrates the target first, so an empty hub needs no separate step.
    """
    try:
        source = await connect(source_url)
    except psycopg.Error as exc:
        raise ImportRefused(
            redact(f"cannot connect to the source: {error_text(exc)}", source_url)
        ) from exc
    try:
        try:
            target = await connect(target_url)
        except psycopg.Error as exc:
            raise ImportRefused(
                redact(f"cannot connect to the target: {error_text(exc)}", target_url)
            ) from exc
        try:
            return await _import(
                source, target, repo=repo, labels=labels, workflow_path=workflow_path
            )
        except psycopg.Error as exc:
            message = redact(
                redact(f"{type(exc).__name__}: {error_text(exc)}", source_url), target_url
            )
            raise ImportRefused(message) from exc
        finally:
            await target.close()
    finally:
        await source.close()


async def _import(
    source: AsyncConnection,
    target: AsyncConnection,
    *,
    repo: str,
    labels: GitHubLabels,
    workflow_path: str | None,
) -> ImportResult:
    version = await schema_version(source)
    if version != SOURCE_VERSION:
        raise ImportRefused(f"source is at schema version {version}, expected {SOURCE_VERSION}")
    await apply_migrations(target)
    found = await (await target.execute(IS_REGISTERED, {"repo": repo})).fetchone()
    if found is not None:
        raise ImportRefused(
            f"{repo} is already registered in the target; delete its rows first if you mean to "
            "import again"
        )
    counts: dict[str, int] = {}
    async with target.transaction(), source.transaction():
        await target.execute(
            REGISTER_REPO,
            {"repo": repo, "labels": Jsonb(labels.model_dump()), "workflow_path": workflow_path},
        )
        for table, read, write, adapt in TABLES:
            counts[table] = await _copy(source, target, read, write, adapt, repo)
    return ImportResult(counts=counts)


async def _copy(
    source: AsyncConnection,
    target: AsyncConnection,
    read: str,
    write: str,
    adapt: Callable[[dict[str, Any]], dict[str, Any]],
    repo: str,
) -> int:
    copied = 0
    async with (
        source.cursor(name="issuebot_import", row_factory=dict_row) as reader,
        target.cursor() as writer,
    ):
        reader.itersize = BATCH
        await reader.execute(read)
        while batch := await reader.fetchmany(BATCH):
            await writer.executemany(write, [{"repo": repo, **adapt(row)} for row in batch])
            copied += len(batch)
    return copied
```

Note on `INSERT_TURN`: it takes every `TurnCapture` field plus `run_id`; `READ_TURNS` selects exactly those columns (compare with `turn_row` in `store.py`; `captured_at` is set by the statement). The `{"repo": repo, ...}` extra key is harmless to psycopg's named placeholders.

`src/issuebot/db/database.py`:

```
    async def import_from(
        self, source_url: str, *, repo: str, labels: GitHubLabels, workflow_path: str | None
    ) -> ImportResult:
        return await import_repo(
            source_url, self._url, repo=repo, labels=labels, workflow_path=workflow_path,
            connect=self._connect,
        )
```

`src/issuebot/db/__init__.py`: export `ImportRefused`, `ImportResult`, `import_repo`.

- [ ] **Step 5: Run the importer tests**

Run: `DATABASE_URL=... uv run pytest tests/test_db_importer.py -v`
Expected: PASS. If the server-side cursor complains about autocommit, the `source.transaction()` in `_import` is what it needs; keep it.

- [ ] **Step 6: The CLI command, test first**

Append to `tests/test_cli.py` beside the other database-command tests:

```python
def test_import_copies_a_source_and_reports_the_counts(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    path = _db_workflow(tmp_path, monkeypatch)
    source = "postgresql://issuebot:old@127.0.0.1:5433/issuebot"
    assert main(["import", "--from", source, "--workflow", str(path)]) == 0
    assert capsys.readouterr().out == (
        "[ OK ] import: issues 3\n"
        "[ OK ] import: runs 2\n"
        "[ OK ] import: run_turns 5\n"
        "[ OK ] import: events 9\n"
        "[ OK ] import: runtime_snapshot 1\n"
        "[ OK ] import: example/repo imported from postgresql://issuebot@127.0.0.1:5433/issuebot\n"
    )
    ((src, repo, labels, workflow_path),) = fake_database.imports
    assert (src, repo, workflow_path) == (source, "example/repo", str(path))
    assert labels == GitHubLabels()


def test_import_reports_a_refusal(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    path = _db_workflow(tmp_path, monkeypatch)
    fake_database.import_error = ImportRefused("source is at schema version 3, expected 2")
    assert main(["import", "--from", "postgresql://x@y/z", "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == "[FAIL] import: source is at schema version 3, expected 2\n"


def test_import_needs_a_source(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["import", "--workflow", str(GOOD)])
    assert exc.value.code == 2
```

Add `["import", "--from", "postgresql://x@y/z"]` to the two parametrised lists (`test_database_commands_need_a_configured_url`, `test_database_commands_exit_two_on_an_unloadable_workflow`).

`tests/fakes/database.py`:

```
        self.imports: list[tuple[str, str, GitHubLabels, str | None]] = []
        self.import_error: DatabaseError | None = None
        self.import_result = ImportResult(
            counts={"issues": 3, "runs": 2, "run_turns": 5, "events": 9, "runtime_snapshot": 1}
        )

    async def import_from(
        self, source_url: str, *, repo: str, labels: GitHubLabels, workflow_path: str | None
    ) -> ImportResult:
        if self.import_error is not None:
            raise self.import_error
        self.imports.append((source_url, repo, labels, workflow_path))
        return self.import_result
```

`src/issuebot/cli.py`, the parser (after `refresh`):

```
    importer = subparsers.add_parser(
        "import", help="copy an old single-repository database into this one, stamped with github.repo"
    )
    _add_workflow_option(importer)
    importer.add_argument(
        "--from", dest="source", required=True, metavar="URL", help="the old database's URL"
    )
    importer.set_defaults(func=cmd_import)
```

The command:

```python
def cmd_import(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    database = _database_or_report(workflow.config)
    if database is None:
        return 1
    settings = workflow.config
    try:
        result = asyncio.run(
            database.import_from(
                args.source,
                repo=settings.github.repo,
                labels=settings.github.labels,
                workflow_path=str(workflow.path),
            )
        )
    except DatabaseError as exc:
        print(f"[FAIL] import: {exc.message}")
        return 1
    for table, copied in result.counts.items():
        print(f"[ OK ] import: {table} {copied}")
    print(f"[ OK ] import: {settings.github.repo} imported from {describe(args.source)}")
    return 0
```

(`describe` from `issuebot.db`; it hides the password.)

- [ ] **Step 7: Run the CLI tests and the whole suite**

Run: `uv run pytest tests/test_cli.py -k "import or database_commands" -v`, then `uv run ruff check . && uv run ruff format --check .`
Expected: PASS; lint clean.

- [ ] **Step 8: Commit**

```bash
git add src/issuebot/db tests/conftest.py tests/test_db_importer.py tests/fakes/database.py tests/test_cli.py src/issuebot/cli.py
git commit -m "feat: issuebot import copies a version-2 database into the hub

Reads the source with version-2 SQL kept in the importer alone, streams
each table through a server-side cursor, and writes everything in one
target transaction stamped with the workflow's repository. Refuses a
source at any other version and a repository already registered, so a
rerun cannot double the events."
```

---

### Task 4: The worker registers itself and scopes its reads

**Files:**
- Modify: `src/issuebot/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `Database.register_repo`, `Database.store(labels, repo)`, `Database.listener(on_notify, repo=)`, `Database.notify_refresh(repo)`, `Queries.scoped(repo)`.
- Produces: `_build_sinks(settings, *, workflow_path: str) -> _Sinks` (registers first); `_status(database, repo)`; `_stats(database, repo, days)`; `_last_rate_limits(database, repo)`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py`, beside the existing worker and run-once database tests:

```python
def test_worker_registers_its_repository_before_the_sinks_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    path = _workflow_with_root(tmp_path)
    assert main(["worker", "--workflow", str(path)]) == 0
    ((repo, labels, workflow_path),) = fake_database.registrations
    assert (repo, workflow_path) == ("example/repo", str(path))
    assert labels == GitHubLabels()
    assert fake_database.repo == "example/repo"  # the store was built for this repository
    (listener,) = fake_database.listeners
    assert listener.repo == "example/repo"


def test_worker_fails_fast_when_registration_fails(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_database.register_error = StoreUnavailableError("cannot connect: refused")
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"
    assert stub_orchestrator.instances == []


def test_run_once_registers_its_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubSession,
    fake_github: FakeGitHub,
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    path = _workflow_with_root(tmp_path)
    assert main(["run-once", "42", "--workflow", str(path)]) == 0
    ((repo, labels, workflow_path),) = fake_database.registrations
    assert (repo, labels, workflow_path) == ("example/repo", GitHubLabels(), str(path))
    assert fake_database.repo == "example/repo"


def test_refresh_names_its_repository(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["refresh", "--workflow", str(path)]) == 0
    assert capsys.readouterr().out == "[ OK ] refresh: notified issuebot_refresh for example/repo\n"
    assert fake_database.notified_repos == ["example/repo"]


def test_status_and_stats_read_their_own_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_database: FakeDatabase
) -> None:
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["status", "--workflow", str(path)]) == 0
    assert main(["stats", "--workflow", str(path)]) == 0
    assert fake_database.queries_obj.scoped_repos == ["example/repo", "example/repo"]
```

Update `test_refresh_notifies_and_reports_failures` to the new output line.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_cli.py -k "registers or refresh_names or read_their_own" -v`
Expected: FAIL (`registrations` empty, wrong output line, `store()` signature).

- [ ] **Step 3: Implement**

`src/issuebot/cli.py`:

```python
async def _build_sinks(settings: Settings, *, workflow_path: str) -> _Sinks:
    """The log sink, plus Slack and PostgreSQL when configured; raises DatabaseError.

    With a database the worker registers its repository first (spec §5): the row the
    dashboard lists and lays the board out by, refreshed on every start so a label rename
    reaches it.
    """
    slack = _slack_sink(settings)
    database = await _open_database(settings)
    if database is not None:
        await database.register_repo(settings.github.repo, settings.github.labels, workflow_path)
    postgres = (
        PostgresSink(
            database.store(settings.github.labels, settings.github.repo),
            description=database.description,
        )
        if database
        else None
    )
    ...
```

Both callers pass `workflow_path=str(workflow.path)`. In `_run_worker`:

```
        initial_rate_limits=await _last_rate_limits(sinks.database, workflow.config.github.repo),
    ...
        listener = sinks.database.listener(orchestrator.request_refresh, repo=workflow.config.github.repo)
```

```python
async def _last_rate_limits(database: Database | None, repo: str) -> RateLimits | None:
    ...
        async with database.queries() as queries:
            row = await queries.scoped(repo).snapshot()
```

`cmd_status` → `_status(database, workflow.config.github.repo)` and inside `queries.scoped(repo).snapshot()`; `cmd_stats` → `_stats(database, workflow.config.github.repo, args.days)` with `queries = base.scoped(repo)` before the six reads. `cmd_refresh`:

```
    repo = workflow.config.github.repo
    try:
        asyncio.run(database.notify_refresh(repo))
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    print(f"[ OK ] refresh: notified issuebot_refresh for {repo}")
    return 0
```

- [ ] **Step 4: Run the CLI suite**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS except the `web` tests, which Task 6 rewrites (they still pass `--workflow` and read `server:`; if they fail here because `create_app` still takes settings, that is expected and named in the commit).

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/cli.py tests/test_cli.py
git commit -m "feat(cli): the worker registers its repository and scopes its reads

register_repo runs after the migration and before the sinks, so a failure
is a startup failure; the store and the listener are built for the
workflow's repository; refresh sends it as the NOTIFY payload; status and
stats read through RepoQueries."
```

---

### Task 5: The web, scoped by repository

**Files:**
- Create: `src/issuebot/web/templates/no-repos.html`
- Modify: `src/issuebot/web/views.py`, `src/issuebot/web/app.py`, `src/issuebot/web/templates/base.html`, `index.html`, `issues.html`, `issue.html`, `turn.html`, `error.html`, `partials/dashboard.html`, `src/issuebot/web/static/app.js`, `src/issuebot/web/static/app.css`
- Test: `tests/fakes/web.py`, `tests/test_web_app.py`, `tests/test_web_pages.py`, `tests/test_web_app_db.py`

**Interfaces:**
- Consumes: `Queries.repos/repo/snapshots/scoped`, `RepoRow`, `Database.notify_refresh(repo)`.
- Produces: `create_app(database, *, clock=, now=)` (no settings); `RepoContext(name, base, api)`; `repo_base(name) -> str` (`/r/{name}`); `api_base(name) -> str` (`/api/v1/repos/{name}`); `switch_target(kind, name, query) -> str`; `worst_status(statuses) -> WorkerStatus`; `repo_labels(row) -> GitHubLabels`; `REPO_COOKIE = "issuebot-repo"`; `issue_filters(state, counts, labels, base)`; `turn_url(base, number, run_id, turn_number)`; template context keys `repo` (`RepoContext | None`) and `repo_options` (list of `{name, url, current}`).

- [ ] **Step 1: The pure helpers, test first**

Append to `tests/test_web_app.py` (the pure-builder section):

```python
from issuebot.web.views import repo_base, api_base, switch_target, worst_status, repo_labels


def test_repo_paths() -> None:
    assert repo_base("acme/frontend") == "/r/acme/frontend"
    assert api_base("acme/frontend") == "/api/v1/repos/acme/frontend"


@pytest.mark.parametrize(
    ("kind", "query", "target"),
    [
        ("dashboard", "", "/r/acme/frontend/"),
        ("issues", "", "/r/acme/frontend/issues"),
        ("issues", "state=review", "/r/acme/frontend/issues?state=review"),
        ("issue", "", "/r/acme/frontend/"),  # an issue number means nothing elsewhere
        ("turn", "", "/r/acme/frontend/"),
    ],
)
def test_switch_target(kind: str, query: str, target: str) -> None:
    assert switch_target(kind, "acme/frontend", query) == target


def test_worst_status_orders_none_over_stale_over_held_over_ok() -> None:
    assert worst_status([]) == "none"
    assert worst_status(["ok", "ok"]) == "ok"
    assert worst_status(["ok", "held"]) == "held"
    assert worst_status(["held", "stale"]) == "stale"
    assert worst_status(["stale", "none", "ok"]) == "none"


def test_repo_labels_validates_the_stored_mapping() -> None:
    row = repo_row(labels={**GitHubLabels().model_dump(), "review": "issuebot/check"})
    assert repo_labels(row).review == "issuebot/check"
    with pytest.raises(ValueError):
        repo_labels(repo_row(labels={"todo": ""}))
```

Add `repo_row` to `tests/fakes/web.py`:

```python
from issuebot.db.queries import RepoRow

REPO = "example/repo"


def repo_row(**overrides: Any) -> RepoRow:
    fields: dict[str, Any] = {
        "repo": REPO,
        "labels": GitHubLabels().model_dump(),
        "workflow_path": "/configs/WORKFLOW.md",
        "registered_at": NOW - timedelta(days=2),
        "seen_at": NOW - timedelta(hours=1),
    }
    fields.update(overrides)
    return RepoRow(**fields)
```

Implement in `src/issuebot/web/views.py`:

```python
from collections.abc import Iterable

from pydantic import ValidationError

from issuebot.db.queries import RepoRow

REPO_COOKIE = "issuebot-repo"
_STATUS_RANK: dict[WorkerStatus, int] = {"ok": 0, "held": 1, "stale": 2, "none": 3}


@dataclass(frozen=True, slots=True)
class RepoContext:
    """What a page knows about the repository it is scoped to: its name and its two prefixes."""

    name: str
    base: str  # /r/{owner}/{name}: the pages
    api: str  # /api/v1/repos/{owner}/{name}: the JSON routes the page calls


def repo_base(name: str) -> str:
    return f"/r/{name}"


def api_base(name: str) -> str:
    return f"/api/v1/repos/{name}"


def repo_context(name: str) -> RepoContext:
    return RepoContext(name=name, base=repo_base(name), api=api_base(name))


def switch_target(kind: str, name: str, query: str = "") -> str:
    """Where the dropdown sends the browser when it picks ``name`` from a page of ``kind``.

    A dashboard stays a dashboard and an issue list stays an issue list (filter kept); an
    issue or a turn page goes to the other repository's dashboard, because the number
    means nothing there (spec §7).
    """
    base = repo_base(name)
    if kind == "issues":
        return f"{base}/issues?{query}" if query else f"{base}/issues"
    return f"{base}/"


def repo_options(
    repos: list[RepoRow], current: str, kind: str, query: str = ""
) -> list[dict[str, Any]]:
    return [
        {
            "name": row.repo,
            "url": switch_target(kind, row.repo, query),
            "current": row.repo == current,
        }
        for row in repos
    ]


def worst_status(statuses: Iterable[WorkerStatus]) -> WorkerStatus:
    """The one status a single-field probe should see: none > stale > held > ok."""
    worst: WorkerStatus = "ok"
    seen = False
    for status in statuses:
        seen = True
        if _STATUS_RANK[status] > _STATUS_RANK[worst]:
            worst = status
    return worst if seen else "none"


def repo_labels(row: RepoRow) -> GitHubLabels:
    """The registry row's labels, validated; a hand-edited row raises ValueError."""
    try:
        return GitHubLabels.model_validate(row.labels)
    except ValidationError as exc:
        raise ValueError(f"labels of {row.repo} do not validate: {exc}") from exc
```

`issue_filters(state, counts, labels, base)` builds `href` as `f"{base}/issues"` and `f"{base}/issues?state={role.value}"`. `turn_url(base, number, run_id, turn_number)` returns `f"{base}/issues/{number}/runs/{run_id}/turns/{turn_number}"`; `issue_document(issue, runs, turns, events, snapshot, base)` passes it through. Update the two existing tests that call `issue_filters`/`turn_url` directly (`tests/test_web_pages.py`, `tests/test_web_app.py`) with `base="/r/example/repo"`.

Run: `uv run pytest tests/test_web_app.py -k "repo_paths or switch_target or worst_status or repo_labels" -v` → PASS. Commit:

```bash
git add src/issuebot/web/views.py tests/fakes/web.py tests/test_web_app.py tests/test_web_pages.py
git commit -m "feat(web): the pure helpers for a repository-scoped page"
```

- [ ] **Step 2: Rewire the fakes and the harness**

`tests/fakes/web.py`: `Harness.__init__` registers one repository and builds the app without settings:

```python
class Harness:
    def __init__(self) -> None:
        self.database = FakeDatabase()
        self.queries = self.database.queries_obj
        self.queries.repo_rows[REPO] = repo_row()
        self.clock = Clock()
        self.client = TestClient(
            create_app(self.database, clock=self.clock, now=self.clock.utcnow),
            raise_server_exceptions=False,
        )

    def register(self, name: str, **overrides: Any) -> None:
        self.queries.repo_rows[name] = repo_row(repo=name, **overrides)
```

Add path helpers the tests use everywhere:

```python
BASE = "/r/example/repo"
API = "/api/v1/repos/example/repo"
```

Drop `SETTINGS`. Then in `tests/test_web_app.py`, `tests/test_web_pages.py` and `tests/test_web_app_db.py`, prefix every request path: `"/"` (the dashboard) becomes `f"{BASE}/"`, `"/issues..."` becomes `f"{BASE}/issues..."`, `"/partials/dashboard"` becomes `f"{BASE}/partials/dashboard"`, `"/api/v1/state"` becomes `f"{API}/state"`, `"/api/v1/issues/7"` becomes `f"{API}/issues/7"`, `"/api/v1/stats..."` becomes `f"{API}/stats..."`, `"/api/v1/refresh"` becomes `f"{API}/refresh"`. Every `href="/issues/7"` assertion becomes `href="/r/example/repo/issues/7"` (and so on). `/healthz` and `/static` stay. `test_web_app_db.py`: `client` fixture is `TestClient(create_app(seeded))`, and the `seeded` fixture calls `await Database(db_url).register_repo("example/repo", GitHubLabels(), "/configs/WORKFLOW.md")` after writing the snapshot.

This is mechanical; do it with care and run the three files after the app is rewired in Step 4.

- [ ] **Step 3: Write the failing route tests**

Append to `tests/test_web_app.py`:

```python
# --- repositories -----------------------------------------------------------------------------


def test_root_redirects_to_the_cookie_then_the_first_repository(h: Harness) -> None:
    h.register("acme/frontend")
    response = h.client.get("/", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (302, "/r/acme/frontend/")
    h.client.cookies.set("issuebot-repo", "example/repo")
    response = h.client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/r/example/repo/"
    h.client.cookies.set("issuebot-repo", "nobody/here")
    response = h.client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/r/acme/frontend/"


def test_root_without_a_registry_is_a_plain_page(h: Harness) -> None:
    h.queries.repo_rows.clear()
    response = h.client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "no worker has registered yet" in response.text
    assert "<select" not in response.text


def test_an_unregistered_prefix_is_a_404_before_any_scoped_read(h: Harness) -> None:
    response = h.client.get("/api/v1/repos/nobody/here/state")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert h.queries.scoped_repos == []
    page = h.client.get("/r/nobody/here/")
    assert page.status_code == 404 and "<h1>404</h1>" in page.text


def test_the_repos_api_lists_every_registration_with_its_worker(h: Harness) -> None:
    h.register("acme/frontend")
    h.queries.snapshot_rows["acme/frontend"] = snapshot(age_s=5.0)
    body = h.client.get("/api/v1/repos").json()
    assert body == {
        "repos": [
            {
                "repo": "acme/frontend",
                "url": "/r/acme/frontend/",
                "worker": "ok",
                "snapshot_at": (NOW - timedelta(seconds=6)).isoformat(),
            },
            {
                "repo": "example/repo",
                "url": "/r/example/repo/",
                "worker": "none",
                "snapshot_at": None,
            },
        ]
    }


def test_healthz_maps_every_worker_and_reports_the_worst(h: Harness) -> None:
    h.register("acme/frontend")
    h.queries.snapshot_rows["example/repo"] = snapshot(age_s=5.0)
    h.queries.snapshot_rows["acme/frontend"] = snapshot(age_s=5.0, dispatch_hold=HOLD)
    body = h.client.get("/healthz").json()
    assert body["status"] == "ok" and body["database"] == "ok"
    assert body["worker"] == "held"
    assert body["workers"]["example/repo"] == {
        "status": "ok",
        "snapshot_at": (NOW - timedelta(seconds=6)).isoformat(),
        "snapshot_age_s": 5.0,
        "dispatch_hold": None,
    }
    assert body["workers"]["acme/frontend"]["status"] == "held"
    assert body["workers"]["acme/frontend"]["dispatch_hold"]["kind"] == "auth"
    h.queries.snapshot_rows["acme/frontend"] = snapshot(age_s=91.0)
    assert h.client.get("/healthz").json()["worker"] == "stale"
    h.queries.repo_rows.clear()
    assert h.client.get("/healthz").json() == {
        "status": "ok",
        "database": "ok",
        "worker": "none",
        "workers": {},
    }


def test_refresh_is_throttled_per_repository(h: Harness) -> None:
    h.register("acme/frontend")
    assert h.client.post(f"{API}/refresh").json()["queued"] is True
    assert h.client.post(f"{API}/refresh").json()["coalesced"] is True
    assert h.client.post("/api/v1/repos/acme/frontend/refresh").json()["queued"] is True
    assert h.database.notified_repos == ["example/repo", "acme/frontend"]


def test_invalid_stored_labels_are_a_503_under_that_prefix_only(h: Harness) -> None:
    h.register("acme/frontend", labels={"todo": ""})
    response = h.client.get("/r/acme/frontend/")
    assert response.status_code == 503 and "do not validate" in response.text
    assert h.client.get(f"{BASE}/").status_code == 200
```

Rewrite `test_healthz_ok_stale_and_none` and `test_healthz_reports_a_held_worker_and_why` in terms of the `workers` map (the top-level `snapshot_at`, `snapshot_age_s` and `dispatch_hold` fields are gone; they are per repository now).

Append to `tests/test_web_pages.py`:

```python
def test_the_header_offers_a_dropdown_of_repositories(h: Harness) -> None:
    h.register("acme/frontend")
    text = html(h.client.get(f"{BASE}/"))
    assert '<select class="repo-switch"' in text
    assert (
        '<option value="/r/acme/frontend/" data-repo="acme/frontend">acme/frontend</option>' in text
    )
    assert (
        '<option value="/r/example/repo/" data-repo="example/repo" selected>example/repo</option>'
        in text
    )
    text = html(h.client.get(f"{BASE}/issues?state=review"))
    assert '<option value="/r/acme/frontend/issues?state=review"' in text
    h.seed_issue()
    text = html(h.client.get(f"{BASE}/issues/7"))
    assert '<option value="/r/acme/frontend/"' in text  # an issue number means nothing there


def test_the_page_carries_its_api_prefix_for_the_scripts(h: Harness) -> None:
    text = html(h.client.get(f"{BASE}/"))
    assert f'hx-post="{API}/refresh"' in text
    assert f'data-stats-url="{API}/stats"' in text
    assert f'hx-get="{BASE}/partials/dashboard"' in text


def test_the_board_uses_the_repositorys_own_labels(h: Harness) -> None:
    h.register("acme/frontend", labels={**GitHubLabels().model_dump(), "review": "team/check"})
    text = html(h.client.get("/r/acme/frontend/"))
    assert "team/check" in text and "issuebot/review" not in text
```

- [ ] **Step 4: Rewire the app**

`src/issuebot/web/app.py`. Signature and context:

```python
def create_app(
    database: Database,
    *,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    app = FastAPI(title="issuebot", docs_url=None, redoc_url=None, openapi_url=None)
    log = get_logger(__name__)
    refreshes: dict[str, _Refresh] = {}
    env = template_environment()
    app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")

    def render(
        name: str,
        *,
        status_code: int = 200,
        repo: RepoContext | None = None,
        repos: list[RepoRow] = (),
        kind: str = "dashboard",
        query: str = "",
        **context: Any,
    ) -> HTMLResponse:
        options = repo_options(list(repos), repo.name, kind, query) if repo is not None else []
        text = env.get_template(name).render(
            repo=repo,
            repo_options=options,
            live_poll_s=LIVE_POLL_S,
            chart_poll_s=CHART_POLL_S,
            chart_days=CHART_DAYS,
            now=now(),
            **context,
        )
        return HTMLResponse(text, status_code=status_code)
```

The scope loader, used by every prefixed handler:

```
    @dataclass(frozen=True, slots=True)  # from dataclasses; RepoQueries from issuebot.db
    class Scope:
        repo: RepoContext
        labels: GitHubLabels
        repos: list[RepoRow]
        queries: RepoQueries

    async def scope(queries: Queries, owner: str, name: str) -> Scope:
        """The registry row for the prefix, its labels and the dropdown's list; 404 when the
        repository is not registered, 503 when its stored labels do not validate. Runs before
        any scoped read (spec §7, §8)."""
        full = f"{owner}/{name}"
        repos = await queries.repos()
        row = next((r for r in repos if r.repo == full), None)
        if row is None:
            raise HTTPException(404, f"repository {full} is not registered")
        try:
            labels = repo_labels(row)
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from exc
        return Scope(repo=repo_context(full), labels=labels, repos=repos, queries=queries.scoped(full))
```

Add `503: "unavailable"` to `_HTTP_CODES`. Move the JSON-or-HTML decision so `/api/` paths keep the envelope: `_wants_json` is unchanged.

Routes (every page handler takes `owner: str, name: str` first):

```
    @app.get("/")
    async def index(request: Request) -> Response:
        async with database.queries() as queries:
            repos = await queries.repos()
        if not repos:
            return render("no-repos.html")
        names = {row.repo for row in repos}
        chosen = request.cookies.get(REPO_COOKIE)
        target = chosen if chosen in names else repos[0].repo
        return RedirectResponse(repo_base(target) + "/", status_code=302)

    @app.get("/r/{owner}/{name}/")
    async def dashboard(owner: str, name: str) -> HTMLResponse:
        async with database.queries() as queries:
            s = await scope(queries, owner, name)
            live = await live_context(s)
        return render("index.html", repo=s.repo, repos=s.repos, live=live)

    @app.get("/r/{owner}/{name}/partials/dashboard")
    async def partial_dashboard(owner: str, name: str) -> HTMLResponse:
        try:
            async with database.queries() as queries:
                s = await scope(queries, owner, name)
                live = await live_context(s)
        except DatabaseError as exc:
            log.warning("web_database_error", path=f"/r/{owner}/{name}/partials/dashboard", error=exc.message)
            return render("partials/dashboard.html", repo=repo_context(f"{owner}/{name}"), live={"unavailable": exc.message}, status_code=503)
        return render("partials/dashboard.html", repo=s.repo, repos=s.repos, live=live)

    @app.get("/r/{owner}/{name}/issues")
    async def issues_page(owner: str, name: str, state: str | None = None) -> HTMLResponse:
        if state is not None and not is_board_state(state):
            raise HTTPException(404, f"there is no {state} column")
        async with database.queries() as queries:
            s = await scope(queries, owner, name)
            counts = await s.queries.state_counts()
            rows = await s.queries.issues_for_state(state)
        return render(
            "issues.html",
            repo=s.repo, repos=s.repos, kind="issues", query=f"state={state}" if state else "",
            rows=rows,
            filters=issue_filters(state, counts, s.labels, s.repo.base),
            truncated=len(rows) >= ISSUE_LIST_LIMIT,
            limit=ISSUE_LIST_LIMIT,
        )
```

`issue_page`, `turn_page` and `turn_raw` follow the same shape under `/r/{owner}/{name}/issues/{number}...`, with `kind="issue"` / `kind="turn"`, `load_turn(s, number, run_id, turn_number)` reading through `s.queries`, and `raw_url=turn_url(s.repo.base, number, run_id, turn_number)`. `live_context(s)` takes the `Scope` and reads through `s.queries`, passing `labels=s.labels`.

The API:

```
    @app.get("/api/v1/repos")
    async def api_repos() -> JSONResponse:
        async with database.queries() as queries:
            repos = await queries.repos()
            snapshots = await queries.snapshots()
        current = now()
        return JSONResponse(
            {
                "repos": [
                    {
                        "repo": row.repo,
                        "url": repo_base(row.repo) + "/",
                        "worker": worker_status(snapshots.get(row.repo), current),
                        "snapshot_at": iso(snapshots[row.repo].at) if row.repo in snapshots else None,
                    }
                    for row in repos
                ]
            }
        )

    @app.get("/api/v1/repos/{owner}/{name}/state")
    async def api_state(owner: str, name: str) -> JSONResponse:
        async with database.queries() as queries:
            s = await scope(queries, owner, name)
            row = await s.queries.snapshot()
        return JSONResponse(state_document(row, now()))
```

`api_issue` (`issue_document(..., s.repo.base)`), `api_stats` and `api_refresh` likewise; `api_refresh` keys the throttle: `refresh = refreshes.setdefault(s.repo.name, _Refresh(clock))` and calls `await database.notify_refresh(s.repo.name)`, logging `web_refresh_requested` with `repo=`. Note `scope()` raising `HTTPException(404)` under `/api/` produces the JSON envelope through the existing handler (`_wants_json`), and the 404 for an unknown issue keeps its `unknown_issue` envelope.

Health:

```
    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        try:
            async with database.queries() as queries:
                repos = await queries.repos()
                snapshots = await queries.snapshots()
        except DatabaseError as exc:
            body = {"status": "unavailable", "database": "unavailable", "error": exc.message}
            return JSONResponse(body, status_code=503)
        current = now()
        workers: dict[str, Any] = {}
        for row in repos:
            snap = snapshots.get(row.repo)
            workers[row.repo] = {
                "status": worker_status(snap, current),
                "snapshot_at": iso(snap.at) if snap is not None else None,
                "snapshot_age_s": snapshot_age_s(snap, current) if snap is not None else None,
                "dispatch_hold": dispatch_hold(snap),
            }
        return JSONResponse(
            {
                "status": "ok",
                "database": "ok",
                "worker": worst_status(w["status"] for w in workers.values()),
                "workers": workers,
            }
        )
```

Remove the `from issuebot.config import Settings` import and `app.state.settings`. `error_response` renders `error.html` with `repo=None`.

- [ ] **Step 5: The templates, the script and the style**

`base.html` header:

```html
<header class="top">
  <a class="brand" href="/">issuebot</a>
  {% if repo %}
  <select class="repo-switch" aria-label="Repository">
    {% for option in repo_options %}
    <option value="{{ option.url }}" data-repo="{{ option.name }}"{% if option.current %} selected{% endif %}>{{ option.name }}</option>
    {% endfor %}
  </select>
  <nav>
    <a href="{{ repo.base }}/">dashboard</a>
    <a href="{{ repo.base }}/issues">issues</a>
    <a href="{{ repo.api }}/state">api</a>
    <a href="/healthz">health</a>
  </nav>
  {% else %}
  <nav>
    <a href="/api/v1/repos">api</a>
    <a href="/healthz">health</a>
  </nav>
  {% endif %}
  ...theme toggle unchanged...
</header>
```

Load `app.js` from `base.html` (after htmx, before `{% block scripts %}`) so the switch handler runs on every page; `index.html` then loads only `chart.umd.js` in its `scripts` block and sets the data attributes on a `<span id="chart-config" hidden data-stats-url="{{ repo.api }}/stats" data-chart-window="{{ chart_days }}d" data-chart-poll-s="{{ chart_poll_s }}">` inside its content. `index.html`'s title is `issuebot: {{ repo.name }}`, its `<h1>` is `{{ repo.name }}`, and the button is `hx-post="{{ repo.api }}/refresh"`. Every `href="/issues...` in `issues.html`, `issue.html`, `turn.html` and `partials/dashboard.html` becomes `href="{{ repo.base }}/issues...`; `hx-get="{{ repo.base }}/partials/dashboard"`. `error.html` keeps `<a href="/">back to the dashboard</a>`.

`no-repos.html`:

```html
{% extends "base.html" %}
{% block title %}issuebot{% endblock %}
{% block content %}
<div class="error-page">
  <h1>no worker has registered yet</h1>
  <p>A worker registers its repository in this database when it starts. Start one with
  <code>docker compose up -d worker</code> in that repository's checkout, or run
  <code>issuebot import --from &lt;old DATABASE_URL&gt;</code> to bring an old database in,
  then reload.</p>
</div>
{% endblock %}
```

`app.js`: before the chart section's early return, the switch:

```js
  // --- the repository dropdown: navigate, and remember the choice for "/" ------------------
  var switcher = document.querySelector("select.repo-switch");
  if (switcher) {
    switcher.addEventListener("change", function () {
      var option = switcher.options[switcher.selectedIndex];
      if (!option) { return; }
      var name = option.dataset.repo || "";
      document.cookie = "issuebot-repo=" + encodeURIComponent(name) + "; Path=/; Max-Age=31536000; SameSite=Lax";
      window.location.assign(option.value);
    });
  }
```

The Poll-now handler matches `info.requestPath` ending in `/refresh` under `/api/v1/repos/` instead of the fixed path. The chart section reads `var config = document.getElementById("chart-config")` and uses `config.dataset.statsUrl`, `config.dataset.chartWindow`, `config.dataset.chartPollS`; it returns early without the element or the canvases.

`app.css`: style the select like the old `.repo` text:

```css
header.top .repo-switch {
  color: var(--muted); background: transparent; border: 1px solid var(--line); border-radius: 6px;
  padding: 3px 6px; font: inherit; max-width: 40vw;
}
```

(The theme test in `tests/test_web_theme.py` checks contrast of marks and text; a transparent select with `--muted` on the panel is what `.repo` already was.)

- [ ] **Step 6: Run the web suites**

Run: `uv run pytest tests/test_web_app.py tests/test_web_pages.py tests/test_web_theme.py tests/test_web_transcript.py -v`, then `DATABASE_URL=... uv run pytest tests/test_web_app_db.py -v`
Expected: PASS. Then lint.

- [ ] **Step 7: Commit**

```bash
git add src/issuebot/web tests/fakes/web.py tests/test_web_app.py tests/test_web_pages.py tests/test_web_app_db.py
git commit -m "feat(web): one dashboard for every repository

Every page and API route is scoped under /r/{owner}/{name}; the header's
repository name is a select over the registry; / redirects to the cookie's
repository or the first registered one; /healthz maps every worker and
reports the worst; the labels come from the registry row, per request."
```

---

### Task 6: `issuebot web` without a workflow, and `server` leaves the settings

**Files:**
- Modify: `src/issuebot/cli.py`, `src/issuebot/config/settings.py`, `src/issuebot/config/__init__.py`
- Test: `tests/test_cli.py`, `tests/test_settings.py`

**Interfaces:**
- Consumes: `create_app(database, *, clock, now)`.
- Produces: `issuebot web [--bind HOST] [--port N]` reading `DATABASE_URL` only; `_run_web(url, *, port, bind)`; `_migrate_database(url) -> Database`.

- [ ] **Step 1: Rewrite the web CLI tests**

Replace the five `test_web_*` tests in `tests/test_cli.py`:

```python
def test_web_migrates_then_serves_on_the_defaults(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    assert main(["web"]) == 0
    assert fake_database.migrations == 1 and fake_database.urls == [DB_URL]
    ((app, host, port),) = fake_serve.calls
    assert (host, port) == ("0.0.0.0", 8080)
    assert getattr(app, "title", None) == "issuebot"
    err = capsys.readouterr().err
    assert "web_started" in err and "s3cret" not in err


def test_web_takes_the_bind_and_port_from_the_command_line(
    monkeypatch: pytest.MonkeyPatch, fake_database: FakeDatabase, fake_serve: FakeServe
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    assert main(["web", "--bind", "127.0.0.1", "--port", "0"]) == 0
    ((_app, host, port),) = fake_serve.calls
    assert (host, port) == ("127.0.0.1", 0)


def test_web_reads_no_workflow(
    monkeypatch: pytest.MonkeyPatch, fake_database: FakeDatabase, fake_serve: FakeServe
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("ISSUEBOT_WORKFLOW", "/nowhere/WORKFLOW.md")
    assert main(["web"]) == 0
    with pytest.raises(SystemExit) as exc:
        main(["web", "--workflow", "x"])
    assert exc.value.code == 2


def test_web_needs_database_url(
    capsys: pytest.CaptureFixture[str], fake_database: FakeDatabase, fake_serve: FakeServe
) -> None:
    assert main(["web"]) == 1
    assert capsys.readouterr().out == "[FAIL] database: not configured; export DATABASE_URL\n"
    assert fake_database.urls == [] and fake_serve.calls == []
```

Keep `test_web_fails_fast_when_the_migration_fails`, `test_web_rejects_a_port_out_of_range` and `test_web_exits_one_when_uvicorn_cannot_bind` but drop their `--workflow` argument and set `DATABASE_URL` via `monkeypatch.setenv`. Remove `["web"]` from the two parametrised database-command lists. In `tests/test_settings.py`, if a test builds a `server:` block, delete it; add:

```python
def test_a_server_block_is_no_longer_accepted() -> None:
    with pytest.raises(ValidationError, match="server"):
        Settings.model_validate({"github": {"repo": "a/b"}, "server": {"port": 1}})
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_cli.py -k web tests/test_settings.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`settings.py`: delete `ServerSettings` and the `server` field on `Settings`. `config/__init__.py`: drop it from the import and `__all__`.

`cli.py` parser:

```
    web = subparsers.add_parser(
        "web", help="serve the dashboard and the JSON API until SIGTERM or SIGINT (needs DATABASE_URL)"
    )
    web.add_argument("--port", type=int, default=8080, help="listen port (default: 8080)")
    web.add_argument("--bind", default="0.0.0.0", help="listen address (default: 0.0.0.0)")
    web.set_defaults(func=cmd_web)
```

```python
_WEB_NOT_CONFIGURED = "[FAIL] database: not configured; export DATABASE_URL"


def cmd_web(args: argparse.Namespace) -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print(_WEB_NOT_CONFIGURED)
        return 1
    return asyncio.run(_run_web(url, port=args.port, bind=args.bind))


async def _migrate_database(url: str) -> Database:
    """Migrate at start; raises DatabaseError."""
    database = _database_factory(url)
    result = await database.migrate()
    get_logger(__name__).info(
        "db_migrated",
        database=database.description,
        applied=list(result.applied),
        version=result.version,
    )
    return database


async def _open_database(settings: Settings) -> Database | None:
    if settings.database.url is None:
        return None
    return await _migrate_database(settings.database.url.get_secret_value())


async def _run_web(url: str, *, port: int, bind: str) -> int:
    """Migrate, build the app and serve it until a stop signal; a failed bind is uvicorn's
    error line and exit 1. The web reads no workflow: everything it shows is in the database."""
    if not 0 <= port <= 65535:
        print("[FAIL] web: --port must be between 0 and 65535")
        return 1
    try:
        database = await _migrate_database(url)
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    get_logger(__name__).info("web_started", bind=bind, port=port, database=database.description)
    try:
        await _serve(create_app(database), host=bind, port=port)
    except SystemExit as exc:
        return 1 if exc.code else 0
    return 0
```

Grep for `server.` and `Settings` mentions of `server` in `src/` and `tests/` and remove what is left (the `validate` sample config renderer, if it prints `server`).

- [ ] **Step 4: Run the whole suite**

Run: `DATABASE_URL=... uv run pytest` and lint.
Expected: everything PASS; this is the first fully green point since Task 1.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/cli.py src/issuebot/config tests/test_cli.py tests/test_settings.py
git commit -m "feat(cli): web is configured by DATABASE_URL and its flags alone

It loads no workflow, so the server block leaves Settings; --bind and
--port carry the defaults the block used to."
```

---

### Task 7: Compose profiles, the external network and the CI check

**Files:**
- Modify: `compose.yaml`, `.env.example`, `.github/workflows/ci.yml`

- [ ] **Step 1: The compose file**

Edit `compose.yaml`:

- `db`: add `profiles: ["hub"]` and `networks: [issuebot]`.
- `test-db`: unchanged.
- `worker`: add `profiles: ["worker"]`, `networks: [issuebot]`; delete the `depends_on` block; above it a comment:

```yaml
    # No depends_on: the database may be in another checkout's project (the hub). A worker
    # that starts first fails its migration, prints [FAIL] database: and exits 1, and
    # restart: unless-stopped brings it back until the hub answers.
```

- `web`: add `profiles: ["hub"]`, `networks: [issuebot]`; delete `ISSUEBOT_WORKFLOW` from `environment` and the `./configs:/configs:ro` volume (and the comment about it); `command: ["web", "--bind", "0.0.0.0", "--port", "8080"]`; the comment becomes: "The dashboard and the JSON API for every repository on this database. Reads nothing but DATABASE_URL; no workflow, no GitHub, Claude or Slack credential."
- At the bottom:

```yaml
# Every project on the host joins one network, created once with `docker network create
# issuebot`, so a worker in another checkout resolves the hub's database as `db`. External
# in every project rather than owned by the hub: a single compose file cannot declare it
# both ways, and a project that finds a network it did not create refuses to start.
networks:
  issuebot:
    external: true
```

- A header comment at the top of the file:

```yaml
# Two roles in one file, chosen by COMPOSE_PROFILES in this checkout's .env:
#   hub     -> db + web        (one checkout on the host)
#   worker  -> worker          (every checkout, this one included)
# So the hub checkout says COMPOSE_PROFILES=hub,worker and every other says worker.
```

- [ ] **Step 2: `.env.example`**

Add at the top, after the first comment:

```bash
# This checkout's role(s): the one hub on the host runs `hub,worker`; every other
# repository's checkout runs `worker` and points at the hub's database (see README,
# "More than one repository"). A plain `docker compose up` starts nothing without it.
COMPOSE_PROFILES=hub,worker
```

- [ ] **Step 3: The CI check**

In `.github/workflows/ci.yml`, `docker` job, before the build step:

```yaml
      - name: compose config under each profile
        run: |
          docker network create issuebot
          for profiles in hub worker hub,worker; do
            COMPOSE_PROFILES=$profiles docker compose config --quiet
          done
```

- [ ] **Step 4: Check locally**

Run: `docker network create issuebot 2>/dev/null; for p in hub worker hub,worker; do COMPOSE_PROFILES=$p docker compose config --quiet && echo "$p ok"; done` and `uv run pre-commit run --all-files`.
Expected: three `ok` lines; pre-commit clean.

- [ ] **Step 5: Commit**

```bash
git add compose.yaml .env.example .github/workflows/ci.yml
git commit -m "build: hub and worker profiles on one external network

db and web are the hub, worker is every checkout; COMPOSE_PROFILES in
.env picks the role. The worker no longer depends on db, since the
database may be in another project; CI checks the file under each
profile combination."
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`

- [ ] **Step 1: README**

- "What is configured where", first bullet: replace "To work on several repositories, run one self-contained stack per repository (see "More than one repository" below); there is no shared dashboard." with "To work on several repositories, run one worker checkout per repository against one shared database and one dashboard (see "More than one repository" below)."
- "Step 1: clone and configure": add `docker network create issuebot` and `COMPOSE_PROFILES=hub,worker` in `.env` to the steps.
- "Step 3: start it": unchanged commands; the host variant drops `uv run issuebot web` needing a workflow: `DATABASE_URL=... uv run issuebot web` "in a second terminal; it reads nothing else".
- Rewrite "More than one repository":

```markdown
### More than one repository

One database and one dashboard serve every repository; each repository still gets its own
worker, in its own checkout, with its own `configs/WORKFLOW.local.md`, workspaces volume
and Claude login. The checkouts meet on one Docker network.

1. Once per host: `docker network create issuebot`.
2. The checkout you already run is the **hub**: its `.env` says `COMPOSE_PROFILES=hub,worker`,
   so `docker compose up -d` starts the database, the dashboard and this repository's worker.
3. Every other repository: clone issuebot again, set `github.repo` in its
   `configs/WORKFLOW.local.md`, copy `.env.example` to `.env` with `COMPOSE_PROFILES=worker`,
   and `docker compose up -d`. The worker reaches the hub's database as `db` over the shared
   network and registers itself; it appears in the dashboard's dropdown on its first start.
4. The dashboard is at http://127.0.0.1:8080 (the hub's `ISSUEBOT_WEB_PORT`). `/` opens the
   repository you last chose; the header's dropdown switches.

`issuebot status`, `stats` and `refresh` act on the repository their workflow names, so run
them from that repository's checkout.

#### Upgrading from one stack per repository

Schema version 3 adds a repository column to every table, and the migration refuses a
database that already holds rows because it cannot tell which repository they belong to.
So the hub starts with a **fresh** database and each old one is imported:

1. In the hub checkout, before starting it with the `hub` profile, give it an empty database:
   either a new `pgdata` volume, or `docker compose exec db createdb -U issuebot issuebot_hub`
   and `DATABASE_URL` pointing at it in the worker's and the web's environment.
2. Start the hub. From each repository's checkout, with the old database still running on
   its published port, run on the host:

   ```bash
   DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5432/issuebot_hub \
     uv run issuebot import --from postgresql://issuebot:issuebot@127.0.0.1:5433/issuebot
   ```

   The repository and its labels come from that checkout's workflow file; the command
   refuses to run twice for the same repository.
3. Switch that checkout to `COMPOSE_PROFILES=worker`, remove its `db` and `web` containers
   (`docker compose rm -sf db web`) and `docker compose up -d`.

Two smaller changes: the dashboard's URLs moved under `/r/<owner>/<name>/` (the API under
`/api/v1/repos/<owner>/<name>/`), and the `server` block in `WORKFLOW.md` is no longer
accepted, since the web takes `--bind` and `--port` instead; delete it.
```

- "When things go wrong": the line about `--force-recreate worker web` becomes `--force-recreate worker` (the web no longer mounts `configs`).

- [ ] **Step 2: CLAUDE.md**

- Commands: `uv run issuebot web [--port N] [--bind HOST]   # the dashboard and the JSON API (needs DATABASE_URL, reads no workflow)`; add `uv run issuebot import --from URL   # copy a version-2 database into this one, stamped with github.repo`; the compose line: `db + web (profile hub) + worker (profile worker), COMPOSE_PROFILES in .env`.
- `issuebot.db` paragraph: migrations `0001_initial`, `0002_run_turns`, `0003_repos` (schema version 3; refuses a database with rows); `PostgresStore(url, *, repo, labels)`; `Queries` (`repos`, `repo`, `snapshots`, `scoped`) and `RepoQueries`; `Database.register_repo`, `store(labels, repo)`, `listener(on_notify, repo=)`, `notify_refresh(repo)`, `import_from`; `importer.py`; the listener's payload rule.
- `issuebot.web` paragraph: `create_app(database, *, clock=, now=)`; the prefixed routes table in one line; `/api/v1/repos`; the root redirect and cookie; `/healthz`'s `workers` map and worst-status rule; `RepoContext`, `switch_target`, `repo_labels`; the web reads no `WORKFLOW.md`.
- `issuebot.cli` paragraph: `web` needs `DATABASE_URL` and takes no `--workflow`; `import`; `refresh` sends the repository; the worker's startup registration.
- Remove `server.*` mentions.

- [ ] **Step 3: The phased design's Later list**

In `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`, under "Later (not scheduled)", change "multiple target repositories per worker" to "multiple target repositories per worker (one dashboard and one database for many workers is done: `2026-09-10-single-dashboard-design.md`)". In "Decisions to confirm" item 6, append "One dashboard across workers landed 2026-09-10."

- [ ] **Step 4: Check and commit**

Run: `uv run pre-commit run --all-files`
Expected: clean.

```bash
git add README.md CLAUDE.md docs/superpowers/specs/2026-09-02-issuebot-phased-design.md
git commit -m "docs: the hub, the worker profile, the import and the moved URLs"
```

---

## Finishing

After Task 8, run the full suite once more against `test-db` (`DATABASE_URL=... uv run pytest`), lint, and then follow `superpowers:finishing-a-development-branch`. The PR goes through the REST API per the user's global instructions (write the body to a temp `.md` file in one Bash call, `gh api repos/{owner}/{repo}/pulls -X POST` with `-F body=@file` in another). Do not merge.
