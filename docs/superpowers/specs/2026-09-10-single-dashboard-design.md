# One dashboard for every repository

Date: 2026-09-10
Status: approved; implemented 2026-09-10 on branch `issuebot/single-dashboard`

## Problem

Today each repository issuebot works on is its own stack: one checkout, one `WORKFLOW.md`,
one PostgreSQL, one worker and one dashboard on its own port. The README's "More than one
repository" section says why: the schema has no repository column, the runtime snapshot
is a single row, and a NOTIFY on the refresh channel wakes every worker on the database.
Nothing in the store knows which repository a row belongs to, so nothing can share it.

The cost is one browser tab per repository and one database per repository on a host
that is already running a database per project. The dashboard's header names the
repository at the top, which is exactly where an operator would expect to switch.

The requirement for this phase: **one dashboard, one database, a dropdown at the top that
switches between repositories, and the existing per-repository history carried over.**
Everything still runs on one VM under Docker Compose; the dashboard is not hosted
elsewhere and gains no authentication.

## Decision

One shared PostgreSQL with one schema, a `repo` column on every table, and a `repos`
registry table that workers write at startup and the web reads for its dropdown and its
label names. The web is configured by `DATABASE_URL` alone and reads no `WORKFLOW.md`.
Workers stay one per checkout, exactly as deployed today, and reach the shared database
over a named Docker network. A one-off `issuebot import` copies each old database into
the new one, stamped with its repository.

### Why not the alternatives

**One schema per repository, the web switching `search_path`** was rejected. It needs no
column changes and the test suite already works that way, but every migration would run
once per schema under a per-schema advisory lock, the repository-to-schema naming would
be a convention to maintain, NOTIFY would still need a payload, and any later cross-repo
page would be a UNION over N schemas. Less code now, more awkwardness for good.

**Keeping every per-repository database and giving the web a list of them** was rejected.
The hub would have to reach N databases across compose projects, the web's configuration
would need editing for every new repository, and the "single dashboard" would be a facade
over N silos.

**One compose project running N worker services** was considered for the deployment and
rejected in favour of keeping one checkout per repository. It avoids the shared network
but means editing the compose file for every new repository and finding per-service homes
for the configs directory, the workspaces volume and the Claude login, all of which the
per-checkout layout already has.

## Design

### 1. Deployment

The same `compose.yaml` serves two roles through Compose profiles:

- `db` and `web` sit under a `hub` profile.
- `worker` sits under a `worker` profile.
- `test-db` keeps its `test` profile and is unchanged.

Each checkout's git-ignored `.env` chooses its role with `COMPOSE_PROFILES`. The checkout
that already runs today becomes the hub and keeps its worker, so its `.env` says
`COMPOSE_PROFILES=hub,worker`. Every other repository's checkout says
`COMPOSE_PROFILES=worker`, drops its own database and dashboard, and keeps its own
`configs` directory, `workspaces` volume, `claude-home` volume and `.env` exactly as they
are. `.env.example` gains the line with `hub,worker` and a comment on the two roles.

The containers meet on one named Docker network, created once on the host with
`docker network create issuebot` and declared by every project as:

```yaml
networks:
  issuebot:
    external: true
```

Every service lists `issuebot` as its only network, so no project creates a default
network of its own. Docker's network-scoped aliases are visible to every container on the
network, so a worker in another compose project resolves the hub's database as `db` and
keeps the `DATABASE_URL` it has now. The network is `external` in every project rather
than owned by the hub because a single compose file cannot declare it both ways, and a
project that finds a network it did not create, but which carries no `external` flag,
refuses to start.

The worker's `depends_on: db` goes, since the database may live in another project. A
worker that starts before the hub answers fails its startup migration, prints
`[FAIL] database:` and exits 1, and `restart: unless-stopped` brings it back until the
database is there. In the hub checkout this can cost one restart at `docker compose up`;
that is the existing fail-fast behaviour, not a new one.

The `web` service loses its `ISSUEBOT_WORKFLOW` environment and its `configs` mount. Its
command becomes `web --bind 0.0.0.0 --port 8080`, and the host port stays
`ISSUEBOT_WEB_PORT` on loopback.

### 2. Configuration

The web is configured by `DATABASE_URL` and its two flags. The repository name, the label
names it needs to lay the board out, and the list of repositories all come from the
database (§3).

`ServerSettings` leaves `Settings`: nothing reads `server.bind` or `server.port` once the
web takes them from flags. A workflow file that still carries a `server` block fails to
load with the loader's usual unknown-field error naming the key, and the README's upgrade
notes say to delete the block. `issuebot web` takes `--bind` (default `0.0.0.0`) and
`--port` (default `8080`), the values the settings defaulted to.

Nothing in the worker's configuration changes. `github.repo` is what the worker registers
and stamps on every row.

### 3. Schema

Migration `0003_repos` takes the schema to version 3.

```sql
CREATE TABLE repos (
    repo          text PRIMARY KEY,      -- owner/name, as github.repo
    labels        jsonb NOT NULL,        -- GitHubLabels.model_dump(): five roles + no_fault
    workflow_path text,                  -- the worker's, for the operator's orientation
    registered_at timestamptz NOT NULL,  -- first registration
    seen_at       timestamptz NOT NULL   -- latest registration
);
```

- `issues` gains `repo text NOT NULL`; its primary key becomes `(repo, number)`; both
  indexes get `repo` as their leading column.
- `runs` gains `repo text NOT NULL`. `run_id` stays the primary key: it is a
  second-resolution timestamp plus six hex digits, unique enough across repositories, and
  `run_turns` references it. *Amended 2026-09-14 (#111): "unique enough" is the stand-in this
  design should not have accepted -- a 24-bit suffix inside one UTC second is a bound, not a
  key, and `RUN_STARTED`'s `ON CONFLICT (run_id)` would have had one repository's row
  overwrite another's. `0004_run_turns_repo` re-keys this table `(repo, run_id)`.* The
  per-issue index becomes `(repo, issue_number, started_at
  DESC)`; `runs_started_at_idx` gains `repo` as its leading column.
- `events` gains `repo text NOT NULL`; both indexes get `repo` as their leading column.
- `runtime_snapshot` drops its one-row `id` column and check and is keyed by `repo`, one
  row per worker.
- `run_turns` is unchanged. *Amended 2026-09-14 (#111): `0004_run_turns_repo` gives it a
  `repo` column, keyed `(repo, run_id, turn_number)` with a foreign key to `runs (repo,
  run_id)`, and re-keys `runs` the same way; the join is no longer what scopes it.*

Model labels are not stored: the web never needs them.

**The migration refuses a database that already holds rows.** Every new column is
`NOT NULL` with no default, and a migration cannot know which repository the existing
rows belong to. So `0003` opens with a `DO` block that raises when `issues`, `runs` or
`events` has any row:

```
issues, runs or events already hold rows and 0003 cannot tell which repository they
belong to; give the hub a fresh database and copy these in with `issuebot import`
```

The raise aborts the migration transaction, so the database stays at version 2 and the
message surfaces through the existing migrate error path in `worker`, `web` and
`migrate`. `runtime_snapshot` is not part of the guard: its one row is replaced, not
stamped, and is dropped by the migration.

The upgrade path is therefore explicit: the hub gets a fresh database, and old data
enters it by import (§4). For the current checkout that means either a new database on
the same server (`docker compose exec db createdb -U issuebot issuebot_hub`, then point
`DATABASE_URL` at it) or a new `pgdata` volume, before the first `hub` start.

### 4. Import

`issuebot import --from <old DATABASE_URL>` is the one-off tool. It runs on the host,
where every old database is reachable on its published loopback port (the hub's through
`ISSUEBOT_DB_PORT`, the others through the ports their checkouts published). The target is
the hub's `DATABASE_URL`, resolved the way every other command resolves `database.url`.
The repository and its labels come from the workflow file, `./configs/WORKFLOW.md` and its
overlay by default like every other command, so running the command from each
repository's checkout stamps that repository.

The command:

1. Opens the source and checks `schema_migrations` is at version 2 exactly; another
   version is `[FAIL] import: source is at schema version N, expected 2` and exit 1.
2. Opens the target, migrates it if needed (the guard in §3 only bites when the target has
   rows, and a fresh hub has none), and checks the repository is not already in `repos`;
   if it is, `[FAIL] import: <repo> is already registered in the target; delete its rows
   first if you mean to import again` and exit 1. This is what makes a rerun safe: events
   have no natural key, so a second pass would double them.
3. Copies, in one target transaction and in foreign-key order: the `repos` row (labels
   from the workflow, `workflow_path` the workflow's path, both timestamps now), then
   `issues`, `runs`, `run_turns`, `events` and `runtime_snapshot`, every row stamped with
   the repository. Events get fresh identity ids in the target; every reader orders them
   by `at`, so the ids do not matter. Turn captures cross as they are, so the dashboard's
   transcript pages keep working for old runs.
4. Prints one line per table with the count copied, and exits 0.

It reads the source with version-2 SQL written into the command, not through `Queries`,
which by then speaks version 3. Source rows are streamed in batches so a large `run_turns`
does not have to fit in memory. Exit codes follow the CLI's 0, 1 and 2.

### 5. Worker side

`Database.register_repo(repo, labels, workflow_path)` is a new facade call, one
`INSERT ... ON CONFLICT (repo) DO UPDATE` that refreshes `labels`, `workflow_path` and
`seen_at`. `worker` and `run-once` call it after migrating and before building the sinks,
so a failure is `[FAIL] database:` and exit 1 like the other startup database checks, not
a queued sink item that could be dropped. A restart re-registers, which is how a label
rename in `WORKFLOW.md` reaches the dashboard.

`PostgresStore` takes the repository beside the labels
(`Database.store(labels, repo)`), and every row it writes carries it: the `issues` upsert
conflicts on `(repo, number)`, `runs` and `events` insert it, and the snapshot write
becomes an upsert on `repo`. `PostgresSink` passes the store through unchanged.

`RefreshListener` takes the repository and reads the notification payload: an empty
payload or one equal to its repository fires the callback, another repository's is
ignored, and anything else (a payload that is not a repository name) is dropped with a
`refresh_payload_ignored` warning. So a bare `NOTIFY issuebot_refresh` still wakes every
worker, which keeps a hand-typed `psql` refresh working. `Database.notify_refresh(repo)`
sends the payload, and the CLI's `refresh` passes its workflow's repository.

`status` and `stats` scope their reads by the workflow's repository through `RepoQueries`
(§6).

### 6. Queries

`Queries` keeps the repository-free reads and gains one factory:

- `repos()` → `list[RepoRow]` (`repo`, `labels` as the stored mapping, `workflow_path`,
  `registered_at`, `seen_at`), ordered by name.
- `repo(name)` → `RepoRow | None`.
- `snapshots()` → `dict[str, SnapshotRow]`, every worker's latest snapshot keyed by
  repository, for `/healthz`.
- `scoped(repo)` → `RepoQueries`, the same connection with the repository bound in.

`RepoQueries` carries the fourteen methods that exist today (`closed_count`, `runs_count`,
`run_totals`, `daily_series`, `issues_by_state`, `state_counts`, `issues_for_state`,
`issue`, `runs_for_issue`, `events_for_issue`, `turn_summaries_for_issue`, `turn`,
`recent_events`, `snapshot`), each with `repo = %(repo)s` in its predicate. `turn` joins
`runs` to check the repository, since `run_turns` has no column of its own. Binding the
repository into the object, rather than adding a parameter to every method, means a
handler cannot forget the predicate. The row types are unchanged; the repository is in
the request, not the row.

### 7. The web

**Routes.** Every page and API route is scoped by a prefix naming the repository:

| Today | After |
|---|---|
| `/` | `/` (redirect, below) and `/r/{owner}/{name}/` |
| `/issues[?state=]` | `/r/{owner}/{name}/issues[?state=]` |
| `/issues/{n}` | `/r/{owner}/{name}/issues/{n}` |
| `/issues/{n}/runs/{run_id}/turns/{t}[/{part}]` | the same under the prefix |
| `/partials/dashboard` | `/r/{owner}/{name}/partials/dashboard` |
| `/api/v1/state` | `/api/v1/repos` (the list) and `/api/v1/repos/{owner}/{name}/state` |
| `/api/v1/issues/{n}` | `/api/v1/repos/{owner}/{name}/issues/{n}` |
| `/api/v1/stats?window=` | `/api/v1/repos/{owner}/{name}/stats?window=` |
| `POST /api/v1/refresh` | `POST /api/v1/repos/{owner}/{name}/refresh` |
| `/healthz`, `/static` | unchanged |

The handlers look the repository up in `repos` first; an unregistered one is a 404 in the
usual envelope or error page, never a 500, because no scoped query runs before the
lookup. The refresh throttle is per repository, one `_Refresh` per name.

`/api/v1/repos` returns `{"repos": [{"repo", "url", "worker", "snapshot_at"}]}`, the
`url` being the repository's dashboard path and `worker` its status from §7's health
rule, so a script can find its way in the same way a browser does.

**The root redirects.** `/` sends the browser to the dashboard of the repository named in
an `issuebot-repo` cookie when that repository is registered, else to the first
registered repository by name. With no rows in `repos` it renders a plain page, from the
error template's family, saying no worker has registered yet and how one does. That page
is a 200, so a fresh hub does not read as broken.

**The dropdown.** The header's repository name becomes a native `<select>` listing every
registered repository, the current one selected, so the header reads as it does now. Each
option's value is the URL of the equivalent page under that repository: the dashboard
stays a dashboard and the issue list stays an issue list (its `state` filter kept), while
anything deeper, an issue or a turn, goes to the other repository's dashboard because the
number means nothing there. The rule lives in a pure `switch_target(page_kind, repo,
query)` in `views.py`. A few lines in the existing `app.js` handle the `change` event,
set the cookie (`SameSite=Lax`, one year, path `/`) and navigate; the CSP forbids inline
handlers and is unchanged. Without JavaScript the select does nothing, which is
acceptable for a loopback-only operator page whose charts already need it.

**Labels per request.** Handlers rebuild `GitHubLabels.model_validate(row.labels)` from
the registry row and pass it to the same view builders that take labels today
(`state_document`, `issue_filters`, the board). The template context's `repo` becomes a
small frozen `RepoContext(name, base)` and a `repo_path(repo, suffix)` helper builds every
internal link, so no template concatenates paths by hand. `_WORKER_KEYS` and the worker
line are unchanged.

**Health.** `/healthz` still answers 503 only when the database does not. Its body gains
`workers`, a map from repository to `{"status", "snapshot_at", "snapshot_age_s",
"dispatch_hold"}` computed per snapshot row with the existing rule (`ok`, `held`, `stale`,
`none`), and the single `worker` field becomes the worst status across them in the order
`none` > `stale` > `held` > `ok`, so a probe that reads one field still sees trouble. With
no registered repository `worker` is `none` and `workers` is empty. The compose
healthcheck is unchanged.

**The `web` command** requires `DATABASE_URL`, loads no workflow, migrates at start as it
does now, and serves on its flags' bind and port.

### 8. Error handling

- A worker whose registration fails reports `[FAIL] database:` and exits 1; compose
  restarts it.
- The migration guard is a SQL `RAISE`; its text names the import command and it
  surfaces through the existing migrate error path.
- `import` prints one `[FAIL] import:` line naming which check refused it and exits 1;
  a workflow it cannot load is exit 2 like every other command.
- A request naming a repository that was registered and later deleted by hand is a 404.
- A NOTIFY payload that is neither empty nor a repository name is dropped with a warning.
- The web with a `repos` row whose `labels` no longer validate (a hand edit) answers 503
  under that prefix with the validation message, since the board cannot be laid out.

### 9. Testing

- Migration: `0003` applies to a fresh schema on `test-db`; the guard refuses a schema
  migrated to version 2 and seeded with rows, and the message names the import command;
  a seeded `runtime_snapshot` alone does not trip it.
- Import: a round trip on `test-db` between two schemas, one at version 2 and seeded with
  every table, one fresh, checking each table's count and that a turn still resolves
  through the scoped `turn` query; then the refusals, an already-registered repository and
  a source at the wrong version.
- Store: every written row carries the repository; the snapshot upsert keeps one row per
  repository; two stores for two repositories on one schema do not see each other's rows.
- Listener: a payload matching the repository fires, an empty one fires, another
  repository's does not, a malformed one warns.
- Queries: two repositories seeded side by side, and every `RepoQueries` method returns
  only its own rows; `repos()` orders by name; `snapshots()` keys by repository.
- Web: `FakeDatabase` gains a registry; tests cover the root redirect with and without the
  cookie and with an unregistered cookie, the empty-registry page, the dropdown's options
  and `switch_target` from each page kind, a 404 for an unknown prefix, per-repository
  labels reaching the board, per-repository refresh throttling, `/api/v1/repos`, and the
  health body's `workers` map with its worst-status rule.
- CLI: `web` refuses to start without `DATABASE_URL` and loads no workflow; `refresh`
  sends its repository; `status` and `stats` scope by it; the worker registers at startup
  and a registration failure is `[FAIL] database:` and exit 1.
- Compose: CI's build job runs `docker compose config` under `COMPOSE_PROFILES=hub`,
  `worker` and `hub,worker`, so a profile typo fails a PR.

### 10. Documentation

- README: "More than one repository" becomes the hub story — create the network, choose
  profiles per checkout, point workers at the hub, import each old database — plus an
  upgrade note covering the fresh database, the `server` block and the moved URLs.
- `compose.yaml` comments explain the two profiles and the external network.
- `.env.example` gains `COMPOSE_PROFILES`.
- CLAUDE.md's package notes for `db`, `web` and `cli` change accordingly.
- The phased design's "Later" list marks multiple repositories as done by this spec.

## Out of scope

Named so nobody expects them in this phase: any cross-repository page or an "all" entry
in the dropdown; authentication; hosting the dashboard off the VM; workers sharing one
Claude login; a `since` filter or any other change to what a worker fetches.

## Implementation notes

Three places where the built thing differs from the plan above, and one clarification.

- **§3's guard text** ends "copy these in with the import command" rather than naming
  `issuebot import` in backticks. `redact()` replaces the database password anywhere in a
  message it passes, and the compose default password is the word `issuebot`, so a message
  that spells the command out reaches the operator with the command name replaced.
- **§3's upgrade route** became a rename rather than a second database: `ALTER DATABASE
  issuebot RENAME TO issuebot_old`, `createdb issuebot`, then the import before the new
  worker starts. `DATABASE_URL` is hard-coded in every checkout's compose file, so pointing
  it at a differently named database would be an edit in every checkout; renaming the old
  data out from under the same name is one command and no configuration change.
- **§7's `repo_path(repo, suffix)` helper** became `RepoContext(name, base, api)`: the
  templates build links from `repo.base` and `repo.api` directly, which keeps the page
  prefix and the API prefix in one object rather than in a filter every template has to
  call correctly.

§1's "every service" means the `hub` and `worker` services. `test-db` deliberately stays
off the `issuebot` network: it is a throwaway for the test suite, reached on a published
loopback port, and joining a shared network would let it collide with the hub's `db` by
name.
