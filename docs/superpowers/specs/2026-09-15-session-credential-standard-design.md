# One credential route for the container, and the pool as its default

Date: 2026-09-15
Status: Draft for review (2026-09-15)
Issue: #142
Amends: `2026-09-14-session-account-pool-design.md` (#121),
`2026-09-14-session-privilege-domain-design.md` (#75)

## Problem

#121 gave the container a pool of session accounts, one per concurrent slot, and settled the
credential question by taking it out of the filesystem: each account has a home of its own,
`claude` reads its login from `$HOME`, and a pool therefore shares no login. It left the
single-account route beside it, with its login in the `claude-home` volume mounted at
`/home/agent/.claude`, and a `credential_complaint` that fires only for a pool.

The two routes have already stopped composing. This deployment runs a pool of five and still
mounts `claude-home` at a home no pool account has:

```text
$ docker inspect issuebot-worker-1 --format '{{range .Mounts}}{{println .Destination}}{{end}}'
/home/agent/.claude          <- the login volume, for an account no session runs as
/workspaces
/configs
```

So the worker carries a volume it cannot read from, and the README documents an interactive
login recipe that the default route cannot use, beside a credential table with three rows of
which the container supports one.

**The pool is also not the default, and turning it on takes two variables that nothing
connects.** `ISSUEBOT_AGENT_POOL_SIZE` is a build arg that creates the accounts;
`ISSUEBOT_AGENT_USER` is runtime text that names them. Reproduced in this deployment, raising
concurrency from three to five:

```text
[FAIL] agent.run_as: agent-4: agent.run_as: no account named 'agent-4';
                     agent-5: agent.run_as: no account named 'agent-5'
```

The configuration is exactly as documented; the image is the stale half, and nothing in the
message says so. `docker history` had the answer -- the `useradd` layer was built with
`ISSUEBOT_AGENT_POOL_SIZE=3` -- which is not where an operator looks. Both variables are
passed into the container wholesale by compose's `env_file`, so a runtime copy of the size
cannot be trusted to describe the image either: it describes the file the operator just
edited.

**Invariant.** A session that runs as an account other than the worker takes its credential
from the environment. The account a session runs as is never one anybody logs into
interactively; the operator's own account, on the host route, still is.

## Decision 1: the pool is the image's default, expressed once

The `RUN` that creates the accounts also writes them to `/etc/issuebot/session-accounts`, one
per line. `resolve.py` names it `SESSION_ACCOUNTS_FILE`, a module constant so a test can point
at a file of its own rather than at the host's `/etc`, and `agent.run_as` resolves in this
order:

1. `agent.run_as` in `WORKFLOW.md` (or the overlay), as today;
2. `ISSUEBOT_AGENT_USER`, as today;
3. the baked list, when the file exists;
4. `()` -- the host route, where no such file does.

The default is then definitionally the accounts the image has, and the failure above cannot
occur on the default path: raising the pool is one build arg and a rebuild, and `run_as`
follows without being told.

**Why a file and not `ENV ISSUEBOT_AGENT_USER=agent-1,...`.** A `Dockerfile` cannot compute an
`ENV` from a loop, and the shape that comes closest -- baking the *number* as an `ENV` and
expanding it at runtime -- reintroduces the bug: compose's `env_file` puts the operator's
`ISSUEBOT_AGENT_POOL_SIZE` into the container's environment, where it would shadow the built
value with the one that is wrong. The list is written by the loop that does the `useradd`, so
it is the built fact rather than a number to re-derive from.

**The explicit path stays diagnosable.** An operator who names accounts by hand keeps every
way to get it wrong, so `validate`'s `agent.run_as` check reports the built pool alongside its
verdict, and a runtime `ISSUEBOT_AGENT_POOL_SIZE` that disagrees with the built list is a
warning naming `docker compose build worker`. That is the line this session went looking for
in `docker history`. On the host route there is no file and no built pool, so the check says
what it says today and nothing more.

## Decision 2: the interactive login leaves the container route

Removed: the `claude-home` volume and its declaration, the `claude-home:/home/agent/.claude`
mount, `/home/agent/.claude` from the image's `VOLUME` list, and the
`docker compose run --rm --user agent --entrypoint claude worker` recipe. The `VOLUME` entry
has to go with the mount rather than after it: left behind, Docker creates an anonymous volume
per container at a path nothing reads, which is the same stale-state problem with no name on
it.

`credential_complaint` widens from `settings.agent.run_as_pooled` to `settings.agent.run_as`
being non-empty, so the rule the worker's startup and `validate` enforce is the invariant
itself rather than a special case of it, and its message loses "or name a single account",
which is no longer a way out. `describe_claude_auth`'s logged-out detail names both routes:
`not logged in; set CLAUDE_CODE_OAUTH_TOKEN (claude setup-token), or run claude auth login on
the host`.

**What stays.** The host route, untouched: `agent.run_as` unset runs the session as the
worker, `claude` reads that account's own login, and `validate` still prints
`logged in (claude.ai, max)`. `claude.ai` remains a supported answer; it stops being a
container answer. The `agent` account stays in the image as the single-account container route
-- now authenticating from the environment like every other account -- and because CI's sweep
proof (#101) and the `test ! -r /home/agent` checks are written against it. Only its volume
goes.

## What this costs

A `claude-home` login keeps a refresh token that `claude` rotates as it goes; it never needs
attention. A token from `claude setup-token` is a fixed string that lapses and must be
re-minted, with #20's auth hold as the reminder -- dispatch held, the reason on the dashboard
and in `issuebot status`, no issue claimed that cannot be worked. That is the whole of what
this trades away, and it buys one documented route that the default deployment can use.

The trade is smaller in practice than on paper, because the environment credential already
wins. Measured on this host, with `claude-home` still mounted and holding a live
`.credentials.json`:

```text
$ docker exec -u agent issuebot-arrowbot-worker-1 claude auth status --json
{"loggedIn": true, "authMethod": "oauth_token", "apiProvider": "firstParty", ...}
```

So a deployment that has set the variable is already on this route, and removing the volume
changes nothing it does at runtime.

## Testing

- `test_the_login_volume_is_the_sessions_home` inverts: no login volume is mounted, and the
  image declares no `VOLUME` under a session account's home.
- New: the baked list is written by the account loop and read as `agent.run_as`'s third
  fallback; `ISSUEBOT_AGENT_USER` and an explicit `agent.run_as` still win over it; no file is
  the host route.
- New: `credential_complaint` fires for a single account with no environment credential, and
  stays silent for the host route.
- New: `validate` reports the built pool, and warns on a runtime size that disagrees with it.
- Unchanged: every `claude.ai` verdict test in `test_agent_runner.py`, `test_cli.py` and
  `test_orchestrator.py`. They are the host route and stay exactly as they are. #140 (a
  `validate` test asserting text that is only right on a machine without the pool accounts)
  overlaps this area and should be fixed in the same pass rather than around it.
- CI's sweep proof needs no change: it plants in the image's own `/home/agent/.claude`, never
  in the volume.

## Migration

A runtime no-op for both deployments on this host, which authenticate with
`CLAUDE_CODE_OAUTH_TOKEN` today: rebuild, recreate, then `docker volume rm` the two
`claude-home` volumes at leisure. The steps belong in the PR body, not the README, which
should read for an operator starting fresh rather than for one upgrading from a version nobody
runs any more. For the same reason this change prunes the README's "Upgrades" section to its
evergreen half -- overrides live in `WORKFLOW.local.md`, rebuild after pulling -- and drops
the three version-to-version notes, including the #75 note telling operators to `chown` a
volume this change deletes. They survive in git history and in the pull requests that added
them.

## Out of scope

The host route's credential. #137 (the session account's shell profile persisting across
sessions) and #126 (session egress) touch the same accounts and are separate.
