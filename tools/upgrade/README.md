# Upgrading every checkout on a host

One database and one dashboard serve every repository, and each repository gets its own worker in
its own checkout ([More than one repository](../../docs/operations.md#more-than-one-repository)).
Upgrading them is not the one-checkout recipe in
[Upgrades](../../docs/operations.md#upgrades) repeated once per deployment, and the difference is
not convenience.

The checkouts are clones of the same repository against one store, and `migrate.py` refuses to
start when the recorded schema version is newer than the code it is running. So finishing one
checkout before starting the next is precisely how a stale worker ends up in a restart loop: the
newest code migrates, and every worker still on the old image fails its next start. This runs
every phase across every checkout before the next phase begins.

```bash
python3 tools/upgrade/upgrade.py --dry-run     # what is pending, and which keys a release wants
python3 tools/upgrade/upgrade.py               # do it
```

`python3`, not `uv run`: this upgrades the checkout that holds its own virtual environment, so
running it through that environment would resolve dependencies it is in the middle of replacing.
It imports nothing but the standard library for the same reason.

## What it does

| phase | |
|---|---|
| inspect | `git fetch`, then branch, upstream, cleanliness, commits behind, services |
| stop | `docker compose stop worker` — only the worker, and never `down` |
| pull | `git merge --ff-only <upstream>` |
| env | which environment **keys** the new example expects, and which you hold that it dropped |
| build | `docker compose build`, hub first |
| validate | `docker compose run --rm worker validate` against the new image |
| up | `docker compose up -d`, hub first |
| health | container states, and each worker's own `status` snapshot |

Only the worker is stopped, because `./configs` is mounted into it and reloads live, so pulling
underneath a running worker hands it a `WORKFLOW.md` its image does not understand. `down` is
never used: in the hub it would take the database away from every other checkout.

Nothing tells it which checkout is the hub. Each declares its own role through
`COMPOSE_PROFILES`, and `docker compose build` and `up -d` are profile-aware — so the hub builds
`web` and `worker` beside `db`, and every other checkout builds `egress` and `worker`, with no
flag from here. A `db` service in `docker compose config --services` is read for one purpose
only: the hub goes first, so the database is up and the newest schema applied before another
worker tries.

## What it will not do

**It will not pull over a checkout that is dirty, carries local commits, or tracks no upstream.**
That ends the run before a single worker has been stopped. The hub checkout is frequently the
development checkout as well, and stopping every deployment to discover the last problem is worse
than not starting.

**It will not start a checkout whose build or `validate` failed.** That one is left stopped and
named in the summary while the others come up, and the exit code is 1. A stopped worker is a
visible, safe state; the same worker started on the old image beside the others on the new one is
the mixed-schema state the phase ordering exists to prevent.

**It will not read a value out of an environment file.** The drift report compares key *names*
between the example the upstream ships and the file the checkout holds, so you learn that
`ISSUEBOT_EGRESS_ALLOW` is now expected without the database password or the Claude credential
reaching a printed line. `tests/test_tools_upgrade.py` enforces that rather than this paragraph.

## Which checkouts

By default: this one, and every sibling directory that is a checkout of the same repository —
which is where the other deployments are, since a repository is added by cloning issuebot again.
Pass paths for any other arrangement.

A sibling qualifies when it holds a `compose.yaml`, its `.git` is a directory, and its
`git remote get-url origin` is identical to this checkout's. So the **origin URL** is what
matches, not the directory name: a `bot-two` clone of this repository is found, and an unrelated
project beside it that happens to have a `compose.yaml` is not. Three things follow.

- The URL is compared as a string, so an HTTPS clone beside an SSH one is not matched, and
  neither is a fork. Name those paths.
- Only the one directory level is scanned. Checkouts under `~/deploy` and `~/srv` do not see
  each other.
- A **git worktree is skipped**, because its `.git` is a file rather than a directory. A worktree
  shares its parent's `origin` and is normally on a feature branch tracking no upstream, so one
  sitting beside a checkout would otherwise be discovered and abort the whole run. That is a
  guess about what is a deployment, not a refusal: a deployment genuinely run from a worktree is
  reached by passing its path, which skips discovery entirely.

```bash
python3 tools/upgrade/upgrade.py ~/deploy/issuebot ~/deploy/issuebot-docs
```

| flag | for |
|---|---|
| `--dry-run` | report and change nothing (a `git fetch` aside) |
| `--force` | run even when every checkout is already at its upstream |
| `--skip-validate` | restart without validating against the new image |
| `--health-wait N` | seconds before the health report (default 40) |
| `--log-dir DIR` | one log per checkout (default: a stamped directory under `~/.cache/issuebot-upgrade`) |

Each checkout's own `@{upstream}` is what it pulls to, so a fork or a pinned release branch
works. If the checkouts are not all on the same branch it says so and carries on — one store
serves them all, so a schema applied by the newest code will stop an older worker starting, but a
deployment may be pinning one deliberately and that is not this tool's call.

## Two things worth knowing

`validate` also fails on a lapsed `CLAUDE_CODE_OAUTH_TOKEN`, which is a real problem but not this
upgrade's. That worker is left stopped; `--skip-validate` restarts it anyway, into the dispatch
hold it would have entered regardless.

A release that changes this tool was upgraded by the *previous* version of it — Python compiles
the whole module before running a line, so the file changing underneath is harmless, but the old
behaviour is what ran. Run it again if `tools/upgrade` moved in the diff.
