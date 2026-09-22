# Toolchains for the target repository

The worker's image carries Python 3.14, `git`, `gh` and `claude`, and nothing else. Whatever
the target repository's own suite needs is the deployment's to add, and `hooks.after_create` is
where most of it goes: an `npm ci`, a `bundle install`, a `go mod download`.

Three things a hook cannot install, because the session runs as a session account (by default
the pool the image built, `agent-1` .. `agent-N` at uids 1011 upwards) with no Docker and no way
to invoke `sudo`:

| What | Build variable | Recipe |
|---|---|---|
| a PostgreSQL server, for a suite whose fixtures fail rather than skip without one | `ISSUEBOT_POSTGRES_VERSION` | [below](#a-postgresql-server-for-the-target-repositorys-tests) |
| `node` and `npm`, to execute the repository's own client-side JavaScript | `ISSUEBOT_NODE_VERSION` | [below](#node-for-the-target-repositorys-tests) |
| `uv`, to run a Python repository's suite, linter and formatter | `ISSUEBOT_UV_VERSION` | [below](#uv-for-the-target-repositorys-tests) |

Each key is empty in `.env.example`, so a deployment that does not need one keeps the image it
has; each is read at build time, so changing one needs `docker compose build worker` rather than
a restart; and only the `worker` service takes the argument. All three end at the same seam --
[`.issuebot/env`](#issuebotenv-what-a-hook-hands-the-agent), the file a hook writes and issuebot
merges into the environment of every turn -- which is the last section here.

Everything else an operator needs is in the [README](../README.md).

## A PostgreSQL server for the target repository's tests

Some repositories cannot run their suite without a real PostgreSQL: the fixtures fail rather
than skip, and most of the tests never get to run. No hook can install a server — the session
runs unprivileged, with no `sudo` and no Docker — and no compose sidecar helps either: a service
on the compose network is reachable by name, not on loopback, and one server shared by every
concurrent session is one session's `DROP DATABASE` away from wrecking another's run.

So the server binaries go into the image, off by default, and each session runs its own
throwaway cluster inside its own workspace.

**1. Build the worker image with a server.** Set `ISSUEBOT_POSTGRES_VERSION=18` in this
checkout's `.env` — `.env.example` carries the key, empty — and rebuild:

```bash
docker compose build worker
docker compose up -d worker
```

Empty — the default — installs nothing, so every checkout that does not need a server keeps
the image it has. The version comes from the PostgreSQL project's own apt repository, so it is
not limited to the one Debian ships; `docker compose run --rm --entrypoint initdb worker
--version` says which one you got. Only the `worker` service takes the argument: the dashboard
needs no server. Changing the variable needs `docker compose build worker`, not just a restart.

**2. Give the target repository's workflow the hooks.** `initdb`, `pg_ctl`, `postgres` and
`psql` are all on the `PATH` of the image built above — in the hooks' login shell too, which
`/etc/profile` would otherwise reset. Put this in the `hooks` block of that checkout's
`configs/WORKFLOW.local.md`; the git-ignored overlay is the right place, since it is a property
of the deployment rather than of issuebot:

```yaml
hooks:
  before_run: |
    set -e
    PG="$PWD/.issuebot/pg"
    mkdir -p "$PG/sock"
    [ -d "$PG/data" ] || initdb -D "$PG/data" -U issuebot --auth=trust \
      --encoding=UTF8 --locale=C.UTF-8 >/dev/null
    pg_ctl -D "$PG/data" status >/dev/null 2>&1 \
      || pg_ctl -D "$PG/data" -w -l "$PG/log" \
           -o "-c listen_addresses='' -k '$PG/sock' -c fsync=off" start
    psql -h "$PG/sock" -d postgres -tAc \
      "select 1 from pg_database where datname='acme_test'" | grep -q 1 \
      || createdb -h "$PG/sock" acme_test
    printf 'export ACME_DATABASE_URL=postgresql://issuebot@/acme_test?host=%s\n' \
      "$PG/sock" > .issuebot/env
  after_run: |
    pg_ctl -D "$PWD/.issuebot/pg/data" -m fast stop || true
  before_remove: |
    pg_ctl -D "$PWD/.issuebot/pg/data" -m fast stop || true
```

Rename `ACME_DATABASE_URL` to whatever the target repository reads, and `acme_test` to
whatever database it expects — in both the `createdb` line and the DSN. `initdb` makes only
`postgres` and the two templates, so without that line the very first connection dies with
`FATAL: database "acme_test" does not exist`, and `.issuebot/pg/log` shows a perfectly
healthy server. Drop the line only if the suite creates its own database.

That is the whole recipe: there is no prompt to change and nothing for the agent to remember
to source, because `.issuebot/env` is the seam described below.

Why it is shaped this way:

- **One cluster per workspace**, under `.issuebot/`, which is the scratch directory issuebot
  already adds to the clone's `.git/info/exclude`. Concurrent sessions never share a server, so
  one session's teardown cannot touch another's data, and `finish_terminal` takes the cluster
  with the workspace when the issue leaves.
- **A Unix socket, `listen_addresses=''`.** No port to allocate, so no collisions between
  concurrent sessions, and nothing outside the container can reach it. It also satisfies a
  target repository that refuses a non-loopback host, because there is no host to refuse:
  `urlsplit` on `postgresql://issuebot@/db?host=/path/sock` reports no hostname at all, and the
  query string survives the DSN rewriting such suites tend to do. Keep the socket
  directory inside the workspace root — the kernel caps a socket path at about 107 bytes, which
  `/workspaces/<repo>-<number>/.issuebot/pg/sock` is comfortably inside.
- **`--auth=trust`** is fine here: the only way to the server is a socket inside a container
  nobody else is in.
- **`initdb` refuses to run as root**, and the session runs as an unprivileged session account
  (uid 1011 upwards for a pool member, 1001 for `agent`), so that is one problem the image does
  not have.
- **`--encoding=UTF8 --locale=C.UTF-8`, even though the image already sets `LANG=C.UTF-8`.**
  Told neither, `initdb` takes the cluster's encoding from the locale, and on a `C` locale that
  is `SQL_ASCII` -- which psycopg then reads back as bytes rather than `str`, so a suite fails
  in teardown rather than anywhere near the cause. The image sets the locale so that a
  cluster a session starts on its own lands right too; the flags are here as well because a
  cluster's encoding is fixed at `initdb` and cannot be corrected afterwards, so the recipe
  should not depend on the environment being what it ought to be.
- **Three hooks, not two.** `before_run` runs once per session and starts the cluster
  idempotently (`pg_ctl status || pg_ctl start`), so a retry or a rework session on the same
  workspace reuses it rather than paying for `initdb` again; `after_run` stops it at the end of
  the session; and `before_remove` stops it again, because `finish_terminal` deletes the
  workspace and a postmaster whose data directory has vanished would otherwise sit there until
  the container restarts.
- **`hooks.timeout_ms` (60 s by default) is ample**: `initdb` takes a couple of seconds and the
  start after it is immediate.

If a session still reports no server, the postmaster's own log says why:
`docker compose exec worker bash -lc 'cat /workspaces/<repo>-<number>/.issuebot/pg/log'`. Drop
the `-lc` and the hooks' `PATH` goes with it, which is a quick way to reproduce a
`command not found`.

## Node for the target repository's tests

The same problem in a different shape: a repository whose tests *execute* its client-side
JavaScript — in jsdom, over the markup the server actually rendered — has nothing to execute it
with. Those tests usually skip rather than fail when `node` is missing, which is the worse
outcome: every pull request reaches review with the JavaScript unverified, and the skip count
is the only trace. A hook cannot install a runtime for the same reasons it cannot install a
server, so `node` and `npm` go into the image the same way, off by default.

**1. Build the worker image with a runtime.** Set `ISSUEBOT_NODE_VERSION` in this checkout's
`.env` — `.env.example` carries the key, empty — and rebuild:

```bash
docker compose build worker
docker compose up -d worker
```

Pick the LTS line the target repository's own CI runs on, rather than treating any number here
as permanent: a repository whose workflow just uses the GitHub runner's default node is on
whatever that runner ships, and that moves. Node 24 is the active LTS at the time of writing.
The major resolves at build time to the newest patch on that line — the build reads
`https://nodejs.org/dist/latest-v<major>.x/SHASUMS256.txt`, picks the Linux tarball for the
image's architecture and verifies its checksum against that same list — so `docker compose run
--rm --entrypoint node worker --version` says which one you got. As with the server, only the
`worker` service takes the argument, empty installs nothing, and changing it needs
`docker compose build worker` rather than a restart. The pin moves by hand: a tarball fetched
by URL is invisible to Dependabot.

**2. Install the target repository's JavaScript dependencies in `after_create`.** That is the
hook where a target repository's dependencies get installed, and it runs once per workspace.
In that checkout's `configs/WORKFLOW.local.md`:

```yaml
hooks:
  timeout_ms: 600000
  after_create: |
    if [ "$(git rev-parse --is-shallow-repository)" = true ]; then git fetch --unshallow; fi
    npm ci --prefix tests/web/js
```

Neither extra line is decoration. An overlay hook *replaces* the base one rather than appending
to it, and the shipped `after_create` is that `git fetch --unshallow`, which the self-review's
`git diff origin/HEAD...HEAD` needs. And `hooks.timeout_ms` bounds *each* hook at 60 s by
default, which a real `npm ci` from a cold cache will overrun; a hook that times out fails the
session and burns an attempt, so raise it once here for all four. The other bound on a hook is
not a setting: 4 MiB of stdout and 4 MiB of stderr, past which its process group is killed and
the run fails saying so, since `hooks.timeout_ms` bounds how long a hook runs and never how
much it writes inside that time, and the process holding what it writes is the worker that
supervises every session. An ordinary install log is tens of KiB; a build that wants to print
more than four megabytes wants `> build.log` rather than a larger cap. Raising it past about 100 s
also means raising `ISSUEBOT_STOP_GRACE_PERIOD` in this checkout's `.env`, which is the
`worker` service's `stop_grace_period` and has to exceed the shutdown wait (`hooks.timeout_ms`
+ 20 s) so that Docker never SIGKILLs a worker still running `after_run`; 600 s here wants
`620s` there, and it takes effect on the `docker compose up -d worker` that recreates the
container. Point `--prefix` at wherever the harness keeps its `package.json`, or drop it if
that is the repository root.

**3. Make a missing runtime fail rather than skip.** Installing a runtime so the tests can run
is pointless if they would still quietly skip, so give the agent the target repository's own
"the harness must work" switch. It goes in
[`.issuebot/env`](#issuebotenv-what-a-hook-hands-the-agent), the file a hook writes and
issuebot merges into the environment of every turn. The recipe in the section above writes
that file with `>`, truncating it every session, so the line has to come from the same
`before_run` rather than be appended to the file by hand. One more line after that `printf`:

```bash
printf 'ACME_JS_HARNESS=1\n' >> .issuebot/env
```

If the target repository needs no PostgreSQL, there is no recipe above to append to and
`before_run` exists only for this, writing the file from nothing — the directory is already
there, since `.issuebot/` is what marks a workspace whose creation finished:

```yaml
hooks:
  before_run: |
    printf 'ACME_JS_HARNESS=1\n' > .issuebot/env
```

`ACME_JS_HARNESS` is a placeholder for the target repository's own switch — the variable its
CI sets so the harness *fails* rather than skips when `node` or jsdom is unavailable; use
whatever that repository calls it. It goes in that file for the same reason the DSN does, and the section below says
what else the file will and will not carry.

`npm`'s cache and logs live under `$HOME/.npm`, inside the container's `issuebot` home, so they
survive between sessions and are gone when the container is recreated. If a session reports
`node: command not found`, check it in a login shell, which is what the hooks get:
`docker compose exec worker bash -lc 'command -v node'`.

## uv for the target repository's tests

The third of these, and the one issuebot needs against its own repository. A Python target
repository's suite, linter and formatter are run through `uv`, and nothing in the container
stands in for it: `/app/.venv` is the *worker's* virtualenv — root-owned, built `--no-dev`, and
so carrying neither pytest nor ruff — and the base image's `pip` is not what a project with a
`uv.lock` is reproduced from. A session that cannot run the suite cannot show its own commit
green, which is how this started: a session working on issuebot reported itself blocked with
"no `uv`/`pytest`/`ruff` with PyPI refused by the egress proxy".

Three things have to be true together, and the failure looks different depending on which one
is missing.

**1. Build the worker image with `uv`.** Set `ISSUEBOT_UV_VERSION` in this checkout's `.env` —
`.env.example` carries the key, empty — and rebuild:

```bash
docker compose build worker
docker compose up -d worker
```

An exact release (`0.12.11`), not a major, which is where this differs from
`ISSUEBOT_NODE_VERSION`: uv is pre-1.0 and its minors are not interchangeable, so pin the
version the target repository's own CI runs. The build downloads that release's tarball for the
image's architecture from `github.com/astral-sh/uv/releases` and verifies it against the
`.sha256` published beside it. As with the other two, only the `worker` service takes the
argument, empty installs nothing, and changing it needs `docker compose build worker` rather
than a restart. The pin moves by hand: a tarball fetched by URL is invisible to Dependabot.
(The `ghcr.io/astral-sh/uv` pin in the builder stage is a different thing and Dependabot's own
— that one builds issuebot, this one runs the target repository's suite, and they are entitled
to differ.)

**2. Let the session reach PyPI.** The [shipped allow-list](../README.md#what-a-session-may-reach) carries
the hosts the workflow itself needs and no registry, so `uv sync` is refused with a `403` until
this checkout's `.env` says otherwise:

```bash
ISSUEBOT_EGRESS_ALLOW=pypi.org,files.pythonhosted.org
```

Then `docker compose up -d egress`, which is a restart of the proxy rather than a rebuild — the
allow-list is read from its environment at start, and the worker needs nothing. Both hosts are
required: the index lives on the first and the wheels on the second. Add
`registry.npmjs.org` and the rest to the same line if the repository also needs them.

**3. Install in `after_create`.** That is the hook where a target repository's dependencies get
installed, and it runs once per workspace. The shipped `configs/WORKFLOW.md` already carries it,
because this repository is itself a Python project:

```yaml
hooks:
  after_create: |
    if [ "$(git rev-parse --is-shallow-repository)" = true ]; then git fetch --unshallow; fi
    uv sync
```

If your deployment overrides `after_create` in `configs/WORKFLOW.local.md`, remember that an
overlay hook **replaces** the base one rather than appending to it — both lines have to be
repeated there, along with anything else that checkout's hook already does. This is the single
most likely way to end up with a worker that installs nothing and says nothing about it.

There is no `command -v uv` guard on that line on purpose. A hook that quietly skipped the
install would leave the session to discover a missing pytest several turns in and guess at why,
where a failed `after_create` fails the run with the reason in it. So the three steps above are
also an ordering: **rebuild the image before the new `configs/WORKFLOW.md` reaches a running
worker**, since `configs/` is bind-mounted and reloads live. `docker compose stop worker`
before pulling, and `up -d worker` after the build, closes that window entirely.

If a session reports `uv: command not found`, check it in a login shell, which is what the hooks
get: `docker compose exec worker bash -lc 'command -v uv'`. If it reports a `403` from the
proxy instead, the image is fine and the allow-list is what is missing —
`docker compose logs egress | grep egress_denied` names the host it wanted.

## `.issuebot/env`: what a hook hands the agent

The agent and the hooks run under a filtered environment — `PASSTHROUGH_NAMES` and
`PASSTHROUGH_PREFIXES` in `src/issuebot/agent/runner.py` — so a variable set on the compose
service, or exported by `before_run`, does not reach `claude` or `pytest`: it dies with the
shell that exported it. A hook that wants to hand something over writes it to `.issuebot/env`
inside the workspace instead, and issuebot merges that file into the environment of every turn
and of every hook after the one that wrote it:

```bash
printf 'ACME_JS_HARNESS=1\n' >> .issuebot/env
```

One hook owns the file: the PostgreSQL recipe above writes it with `>`, which is what makes
`before_run` idempotent on a workspace a retry reuses, so a second variable belongs in that
same `before_run` — appended with `>>` after the recipe's line, as above — rather than in a
hook that would truncate it again or append a duplicate per session.

- **One `KEY=VALUE` per line.** A leading `export ` is accepted and stripped, blank lines and
  `#` comments are skipped, and the value is everything after the first `=`: no quote stripping
  and no `$VAR` expansion, because a hook that wants either has a shell. Only the surrounding
  whitespace of the line goes, so an indented here-doc and a CRLF file both parse. Keys match
  `[A-Za-z_][A-Za-z0-9_]*`.
- **Read fresh for every turn and every hook.** `before_run` runs once per session, so a session
  resumed after a retry still gets the file, and a hook may rewrite it between turns.
- **Only a regular file is read.** issuebot opens the name without following symbolic links and
  looks at what it found before reading a byte: a link, a FIFO, a device or a directory there
  is refused with a warning naming the reason, and at most 64 KiB is read, cut at a line
  boundary. The file sits in a directory the session can write, and under `agent.run_as` the
  worker's uid can read files the session's cannot, so a link there would otherwise hand the
  session whatever it pointed at.
- **Some names are protected**, and a line naming one is dropped with a warning naming the key.
  `PATH`, `HOME`, `GH_TOKEN` and the fixed entries (`GH_PROMPT_DISABLED`,
  `GH_NO_UPDATE_NOTIFIER`, `NO_COLOR`, `GH_PAGER`, `DISABLE_AUTOUPDATER`,
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY`) keep `gh` and `claude` running as issuebot launched them,
  so a typo cannot take either down in the middle of a run and a line cannot switch the shared
  home's auto memory back on. So are the six proxy variables (`HTTP_PROXY`,
  `HTTPS_PROXY`, `NO_PROXY` and their lower-case spellings), for the reason `PATH` is and no
  stronger one: what bounds egress is the container's lack of a route rather than a variable,
  so a line emptying them would take `gh`, `git` and the next turn's `claude` off the network
  without admitting anything off the allow-list. So is anything starting
  `ANTHROPIC_` or `CLAUDE_`: the file lives in the agent's own workspace, so the *session* can
  write it as easily as a hook can, and it must not be able to re-point or re-credential the
  `claude` issuebot launches for the next turn. The file's job is to add what the target
  repository's tests need.
- **Nothing here ever fails a turn.** No file is the normal case; an unreadable one, a line that
  does not parse, a value with a null byte in it, and anything past 64 KiB are all warnings and
  the turn runs. A warning about a line names its number and nothing else, and the log records
  which keys were applied, never their values — the usual contents are a DSN with a password
  in it.
- **It is a workspace file, so it outlives the session.** A retry or a rework session on the
  same workspace finds what the last one left, which is why the recipe's `before_run` writes it
  with `>` rather than appending to it.
- `after_create` is the one hook that cannot use it, in either direction: it runs before
  `.issuebot/` exists, because that directory's presence is what marks a workspace whose
  creation finished. Write the file from `before_run`.
- **A hook cannot hand anything over through `~/.profile`**, which is what a toolchain
  installer (`rustup`, `nvm`, `pyenv`) appends its `PATH` line to. The worker sweeps the session
  account's shell start-up files before every hook and every turn, so an installer's line
  is gone before the next login shell would read it — between two sessions, which is the point,
  and within one. `PATH` itself is a protected name here too, so the routes for a tool the image
  does not carry are the ones the requirements list gives: build an image `FROM` this one, or
  have the hooks and the session call the tool by its full path (a hook can export the
  directory's *name* through this file and the agent can use it).
