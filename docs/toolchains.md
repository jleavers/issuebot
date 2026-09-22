# Toolchains for the target repository

The worker's image carries Python 3.14, `git`, `gh` and `claude`, and nothing else. Whatever
the target repository's own suite needs is the deployment's to add, and `hooks.after_create` is
where most of it goes: an `npm ci`, a `bundle install`, a `go mod download`.

Four things a hook cannot install, because the session runs as a session account (by default
the pool the image built, `agent-1` .. `agent-N` at uids 1011 upwards) with no Docker and no way
to invoke `sudo`:

| What | Build variable | Recipe |
|---|---|---|
| a PostgreSQL server, for a suite whose fixtures fail rather than skip without one | `ISSUEBOT_POSTGRES_VERSION` | [below](#a-postgresql-server-for-the-target-repositorys-tests) |
| `node` and `npm`, to execute the repository's own client-side JavaScript | `ISSUEBOT_NODE_VERSION` | [below](#node-for-the-target-repositorys-tests) |
| `uv`, to run a Python repository's suite, linter and formatter | `ISSUEBOT_UV_VERSION` | [below](#uv-for-the-target-repositorys-tests) |
| `pwsh`, for a repository whose deliverables and suites are PowerShell | `ISSUEBOT_PWSH_VERSION` | [below](#powershell-for-the-target-repositorys-tests) |

Each key is empty in `.env.example`, so a deployment that does not need one keeps the image it
has; each is read at build time, so changing one needs `docker compose build worker` rather than
a restart; and only the `worker` service takes the argument. All four end at the same seam --
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

**2. Let the session reach PyPI.** The [shipped allow-list](security-model.md#what-a-session-may-reach) carries
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

**uv's cache lives on the `workspaces` volume, one directory per session account.** The
worker creates `<workspace.root>/.uv-cache/<account>` — `/workspaces/.uv-cache/agent-1` on a
default deployment — and hands it to every hook and every turn as `UV_CACHE_DIR`. There is
nothing to configure: it is derived from `workspace.root`, so a deployment whose workspaces are
somewhere else gets its caches there too.

Two things follow from it, and they are the reason it exists. uv would rather hardlink a
package out of its cache into the venv than copy it, and a hardlink cannot cross a filesystem:
with the cache in `$HOME/.cache/uv`, in the container's own writable layer, and the venv at
`<workspace>/.venv` on the volume, it never could. On the same filesystem it can, so a second
workspace's venv costs almost nothing — measured on the live worker, this repository's own
dependency set: 152 MB for two venvs copied, 77 MB for the two hardlinked out of one 78 MB
cache. And the cache is on the volume rather than in the container's writable layer, so it
survives `docker compose up -d worker` instead of being re-downloaded from PyPI by the first
session after every worker recreation.

**One directory per account, and that is the point of the shape.** A cache is a directory one
process writes and the next installs *from*, so a cache shared between session accounts would
be a surface one session could write for another to execute — exactly what the [account
pool](security-model.md#one-account-per-concurrent-session) exists to prevent. Each directory is `1770`, owner
the worker and group that account's own, inside a `0755` root: an account reaches its own and
is refused at every sibling's door. Per account it is the boundary that account's own home
already draws, and the next session bound to it is the one the cache is kept for.

What the hardlink *does* change is worth stating plainly, since it is not nothing. A hardlinked
`.venv` entry is the cache's own inode, so two workspaces bound to one account now share the
files their venvs were installed from — and an idle workspace is sealed `0700` precisely
because a hostile session may later be handed an account that also holds an honest, idle one. A
hardlink reaches past that seal into the honest workspace's `.venv`. Three things bound it. The
two sessions are the same account at the same uid, which already shares a home, and that home
already held a per-account uv cache the home sweep does not touch (it is a denylist of
instruction surfaces and shell start-up files, and names no cache) — so this is a channel uv's
default location had too, and what the hardlink adds is that a poisoning takes effect without
waiting for the honest workspace to sync again. The clone is untouched, so nothing reaches what
that session commits and pushes; only what its tests import. And the alternative gives up the
venv sharing this was measured for: a per-workspace cache would close it, and the second
workspace's venv is free only because it is the first one's files. Whether the residual is
worth closing is [#176](https://github.com/jleavers/issuebot/issues/176).

Nothing prunes the cache, and it shares the volume with the clones — once the venvs are
hardlinks into it, removing a workspace frees very little that the cache still holds, and a
full volume stops workspace creation rather than just caching. `uv cache prune` from a hook is
the lever if a deployment wants one; `uv cache clean` is not, since it removes the cache
directory itself and that directory's parent is the worker's.

The host route (`agent.run_as` unset) carries none of this: there is no session account, the
home is the operator's own, and uv's default cache stays where it is. Nor does an image built
without `ISSUEBOT_UV_VERSION`, which has no `uv` on `PATH` for the question to be about.

**`UV_LINK_MODE` is no longer set anywhere,** which is the other half of the same change. The
build used to default it to `copy` in `/etc/profile.d/issuebot-uv.sh`, because the copy was
unavoidable and uv warns three lines about falling back to one — on the stderr of
`after_create`, the first hook of every session, logged in full and quoted into the run's error
if that hook fails, which is a poor place to leave an unexplained warning about something that
is working. With the cache on the volume the fallback is gone and uv's own default is what
should happen, so the image states nothing and lets it. A deployment that wants something else
still has both routes: `uv sync --link-mode=copy` in the hook line itself, which is the only
one `after_create` has — it runs before anything has written `.issuebot/env`, and that file
only reaches the hooks *after* the one that wrote it — or `UV_LINK_MODE=copy` in an
`.issuebot/env` written from `before_run`, which covers the later hooks and every turn.
`UV_CACHE_DIR` is overridable from the same file, for the same reason: neither name is on the
protected list. Speed was never the argument either way — the copy took 122 ms for this
repository.

If a session reports `uv: command not found`, check it in a login shell, which is what the hooks
get: `docker compose exec worker bash -lc 'command -v uv'`. If it reports a `403` from the
proxy instead, the image is fine and the allow-list is what is missing —
`docker compose logs egress | grep egress_denied` names the host it wanted.

## PowerShell for the target repository's tests

The fourth of these, for a target repository whose deliverables and suites are PowerShell. It
is the simplest of the four to operate and the largest to carry: one build argument, no
registry, and about 220 MB of image.

**1. Build the worker image with `pwsh`.** Set `ISSUEBOT_PWSH_VERSION` in this checkout's
`.env` — `.env.example` carries the key, empty — and rebuild:

```bash
docker compose build worker
docker compose up -d worker
```

An exact release (`7.6.6`), not a major. That is uv's reason — pin what the target
repository's own CI runs — plus a harder one: GitHub publishes releases under their tags and
there is no `latest-v7.x` to resolve, so a major on its own names nothing to download. The
build fetches `powershell-<version>-linux-<arch>.tar.gz` from
`github.com/PowerShell/PowerShell/releases` and verifies it against the `hashes.sha256`
published for that release. As with the other three, only the `worker` service takes the
argument, empty installs nothing, and changing it needs `docker compose build worker` rather
than a restart. The pin moves by hand, for the reason `ISSUEBOT_NODE_VERSION`'s and
`ISSUEBOT_UV_VERSION`'s do.

The build also installs the ICU runtime, inside the same guard, so an image built without the
argument carries neither. .NET reads its globalization data from ICU and the base image has
none; without it `pwsh` starts in invariant mode, where `'{0:N2}'` formats `1234.5` as
`1234.50` with no group separator and culture-aware string comparison changes its answers. A
runtime that starts and then quietly disagrees with the developer's machine is worse than one
that does not start, so CI asserts the formatting rather than the version.

**2. There is no step 2.** Unlike node and uv, PowerShell needs nothing added to
[the allow-list](security-model.md#what-a-session-may-reach): the runtime ships in the image, and a repository
of plain `.ps1` deliverables installs nothing to run its tests. The exception is a suite that
pulls modules from the PowerShell Gallery — `Install-Module`, or a `#Requires -Modules` that
is not already vendored — which needs

```bash
ISSUEBOT_EGRESS_ALLOW=www.powershellgallery.com,psg-prod-eastus.azureedge.net
```

and then `docker compose up -d egress`, a restart of the proxy rather than a rebuild. Both
hosts: the gallery answers the search and the CDN serves the `.nupkg`.

**3. `after_create` is usually empty.** A PowerShell repository typically has no dependency
install, so the hook has only the base workflow's unshallow in it — which still earns its
place, since the clone is `--depth 1` and both the self-review's `git diff origin/HEAD...HEAD`
and the merge of the default branch need the merge base:

```yaml
hooks:
  after_create: |
    if [ "$(git rev-parse --is-shallow-repository)" = true ]; then git fetch --unshallow; fi
```

Remember that an overlay hook **replaces** the base one rather than appending to it. The
shipped `configs/WORKFLOW.md` ends its `after_create` with `uv sync`, because this repository
is itself a Python project; a deployment pointed at a PowerShell repository that leaves that
in place fails every session at `uv: command not found` before turn 1, and one that overrides
it has to repeat the unshallow line above.

**What this does not give you.** `pwsh` on Linux is PowerShell 7 on .NET, which is not Windows
PowerShell 5.1 and is not Windows. A suite that shells out to `w32tm`, `DISM` or `netsh`, or
that reaches a Windows-only module, still needs a Windows runner — so the session's local run
proves the parts that are pure logic, and the repository's own `windows-latest` checks remain
the authority on the rest. That division is already how the workflow reads a pull request: a
check that ran steps and failed holds the issue.

If a session reports `pwsh: command not found`, check it in a login shell, which is what the
hooks get: `docker compose exec worker bash -lc 'command -v pwsh'`.

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
- **So is anything starting `GIT_` or `GH_`, and the tails of those two tools' own fallback
  chains**, for the reason `PATH` is and one step in: `PATH` decides
  *which* binary `git` and `gh` are, and these decide what that binary does and which further
  commands it runs. A git config file names commands (`core.pager`, `credential.helper`,
  `[alias] x = !...`), and `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM` and the
  `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>` triple each supply one at a
  path — or a key with no file at all — of the line's choosing; `GIT_EDITOR` (on a plain
  `git commit`), `GIT_SEQUENCE_EDITOR`, `GIT_PAGER`, `GIT_ASKPASS`, `GIT_SSH`,
  `GIT_SSH_COMMAND` and `GIT_PROXY_COMMAND` name one outright; `GIT_EXEC_PATH` and
  `GIT_TEMPLATE_DIR` name a directory of them; and `GIT_DIR`/`GIT_WORK_TREE` re-point which
  repository is being operated on. On the `gh` side, `GH_CONFIG_DIR` and `XDG_CONFIG_HOME` both
  name the directory holding `config.yml`, whose aliases may be shell commands, and `GH_EDITOR`
  and `GH_BROWSER` name commands — so between them they re-point the one tool in the session
  holding `GH_TOKEN`.

  Whole prefixes rather than a list of those names, because a list is one somebody has to keep
  complete against git's and `gh`'s own manuals. But a prefix covers only the *head* of each
  chain those tools resolve a setting through, and the config rung in the middle is swept out
  of the home by #151 — so the environment tails are protected too, by name:

  | chain | head (prefixed) | tail (protected by name) |
  |---|---|---|
  | editor | `GIT_EDITOR`, `GH_EDITOR` | `VISUAL`, `EDITOR` |
  | pager | `GIT_PAGER`, `GH_PAGER` | `PAGER` |
  | browser | `GH_BROWSER` | `BROWSER` |
  | askpass | `GIT_ASKPASS` | `SSH_ASKPASS`, `SSH_ASKPASS_REQUIRE` |
  | commit identity | `GIT_AUTHOR_EMAIL` | `EMAIL` |
  | token | `GH_TOKEN`, `GH_ENTERPRISE_TOKEN` | `GITHUB_TOKEN`, `GITHUB_ENTERPRISE_TOKEN` |

  `EDITOR` runs on a plain `git commit` with no terminal at all, so protecting `GIT_EDITOR` and
  leaving it would close nothing. `XDG_CONFIG_HOME` is on no chain and is protected separately,
  for `gh`'s aliases. These are names and not prefixes on purpose: `SSH_AUTH_SOCK` is a
  legitimate route for a forwarded deploy key, `XDG_DATA_HOME`/`XDG_CACHE_HOME` are untouched,
  and the `GITHUB_` namespace holds plenty a hook may hand over. The workspace outlives the
  session, so what such a line would re-point is the *next* session on that issue — and it is
  the environment spelling of what the home sweep removes from the account's
  `~/.gitconfig`, `~/.config/git/config` and `~/.ssh/config`.

  **This is not the channel your `GIT_AUTHOR_*`/`GIT_COMMITTER_*` values travel on**, and they
  are unaffected: set in `.env`, they reach the worker's environment and the session inherits
  them through `PASSTHROUGH_PREFIXES` exactly as before. What is refused is a file the session
  itself can write re-pointing the identity your pull requests are committed under.

  A hook with a real reason for a git setting of its own — a deploy key for the target
  repository is the usual one — has three routes that are not this file:

  - `git config --local core.sshCommand 'ssh -i /path/to/key -o IdentitiesOnly=yes'` from
    `after_create`. The workspace directory *is* the clone, so this is the post-clone setup's
    own idiom (it writes `credential.https://github.com.helper` exactly this way) and it reaches
    every later turn, because the clone does.
  - `git -c core.sshCommand=...`, or `GIT_SSH_COMMAND=... git ...` exported in the hook's own
    shell, for git the hook itself runs — a submodule fetch, a second clone. Unchanged: what is
    bounded is handing the variable *to the session*, not the hook's own environment.
  - A root-owned `/etc/gitconfig` or `/etc/ssh/ssh_config` in an image built `FROM` this one,
    for a deployment-wide setting — better than a variable for that purpose, since it is outside
    the session's reach altogether. `GIT_CONFIG_SYSTEM` and `GIT_CONFIG_NOSYSTEM` being
    protected is what keeps that route honest.

  Two things those routes do *not* cover, so that you find out here rather than from a variable
  that silently did not arrive (a refused key is a `workspace_env_ignored` line in the **worker's**
  log, not something the hook sees):

  - **Behaviour-only `GIT_*` switches with no config equivalent** — `GIT_TERMINAL_PROMPT=0`,
    `GIT_TRACE*`, `GIT_CURL_VERBOSE`, `GIT_LFS_SKIP_SMUDGE`. Set them in the hook's own shell
    around the git it runs, or system-wide in a derived image.
  - **`XDG_CONFIG_HOME` for tools that are not `git` or `gh`** — `uv`, `ruff`, `npm` or anything
    else following the specification. With `HOME` protected too, a hook can no longer hand the
    session a relocated config root; what it keeps is per-command (`XDG_CONFIG_HOME=... tool ...`
    in the hook's own shell) and per-repository (config written into the clone, which every later
    turn sees). `XDG_DATA_HOME` and `XDG_CACHE_HOME` are not protected, which covers the cache
    and state cases. The trade is deliberate: a route to `gh`'s aliases is not one to leave open
    for the convenience of pointing another tool's config somewhere.

- **So are the five names that decide what the hook's own shell runs**: `BASH_ENV`,
  `SHELLOPTS`, `BASHOPTS`, `PS4` and `CDPATH`. Every script issuebot runs for a session — the
  post-clone setup and all four hooks — goes through `bash -lc`, and `bash` reads these out of
  the environment it is handed, before or around the commands the hook actually wrote:

  - `BASH_ENV` names a file a non-interactive `bash` **sources before** the command it was
    given. That is the `~/.profile` channel below in variable form, reaching every hook of the
    next session on that issue.
  - `SHELLOPTS` and `BASHOPTS` enable `set -o` and `shopt` options from the environment before
    any start-up file is read, `xtrace` among them — and with `xtrace` on, `PS4` is expanded
    before every traced command, command substitution and all, the first of them inside
    `/etc/profile`. It takes the pair: `PS4` is inert without `xtrace`, and `xtrace` with the
    default `PS4` only prints. So both are protected, `BASHOPTS` with them as the `shopt` half
    of the same switch.
  - `CDPATH` is `PATH`'s rule for directories: a hook's `cd sub` resolves through it, so a line
    here sends the hook into a tree of the last session's choosing and the relative command
    after the `cd` is that tree's file.

  The cost is about as small as a protection gets: a hook that wants a file sourced before its
  own commands has `source` in the script it already owns, `set -x` for a trace, and an absolute
  path for a `cd`. What it may not do is hand the variable to the *next* session's shell.

  **`ENV` is not protected, and does not need to be.** It is POSIX's start-up file for an
  *interactive* shell, and nothing issuebot runs is interactive: it was measured unread by
  `bash -lc`, by `bash --posix -c`, by `bash` invoked as `sh`, and by `sh -c` (dash). `PS1`,
  `PS2` and `BASH_XTRACEFD` are not protected either — none of them runs anything.

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
  and within one. Nor through `BASH_ENV`, which names such a file without writing one: it is a
  protected name above, so the sweep's guarantee does not rest on a variable nothing
  checked. `PATH` itself is a protected name here too, so the routes for a tool the image
  does not carry are the ones the requirements list gives: build an image `FROM` this one, or
  have the hooks and the session call the tool by its full path (a hook can export the
  directory's *name* through this file and the agent can use it).
- **Nor through `git config --global`.** The session account's `~/.gitconfig`,
  `~/.config/git/config` and `~/.ssh/config` are swept on the same schedule, so a hook
  that writes user-level git or ssh config finds it gone before the next login shell — again
  between two sessions and within one. Commit identity is already handled: set the
  `GIT_AUTHOR_*`/`GIT_COMMITTER_*` values in `.env` and they reach every session's `git` through
  the environment. Anything else that has to be global belongs in `/etc/gitconfig` or
  `/etc/ssh/ssh_config` in an image built `FROM` this one; a hook can always use
  `git config --local` inside the clone, which is what the post-clone setup does.
