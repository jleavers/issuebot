# One account per concurrent session, and the credential that makes it possible

Date: 2026-09-14
Status: implemented
Issue: #121 (security sweep findings `hostile-issue-2`, `copycat-3`, point 3 of #104)
Successor to: `2026-09-14-session-privilege-domain-design.md` (#75)

## Problem

#75 drew the line between the worker and the session: two accounts, one sudo rule, the
session's environment across a descriptor. It drew no line *between* sessions. `agent.run_as`
named one account for the whole deployment, so with `agent.max_concurrent_agents` at its
default of 3 every concurrent session ran at the same uid. The workspaces are siblings under a
traversable root (`/workspaces`, `0755`), each per-issue directory was `1777` for that account,
and the clone inside it was that account's to write. A hostile issue's session could therefore
edit an unrelated, honest issue's working tree before that session committed and pushed — the
same class of finding as #75's, one level in.

Reproduced live from a session in this deployment, before the change:

```text
$ id
uid=1001(agent) gid=1001(agent) groups=1001(agent)
$ ls -ld /workspaces/issuebot-119
drwxrwxrwt 10 issuebot issuebot 4096 Sep 14 21:24 /workspaces/issuebot-119
$ touch /workspaces/issuebot-119/.hostile-probe-121 && ls -l /workspaces/issuebot-119/.hostile-probe-121
-rw-rw-r-- 1 agent agent 0 Sep 14 21:28 /workspaces/issuebot-119/.hostile-probe-121
$ stat -c '%U %a' /workspaces/issuebot-119/.git
agent 775
```

**Invariant.** Two sessions running at the same time are as separate from each other as each is
from the worker: neither can read, write or enter the other's workspace, and the uid is what
says so.

This was not shipped with #104's other two points because a per-slot account is not a code
change alone. `claude` authenticates itself from `$HOME` (or `CLAUDE_CONFIG_DIR`), and N
accounts means N homes. The credential had to be decided first, and a session cannot test a
credential against a real login.

## The decision: the pool takes its credential from the environment

The four options #121 set out were N logins (correct, expensive to operate), one credential
copied into each home, one shared config directory, and an environment-borne credential.

**Chosen: the environment.** A pool of session accounts requires `CLAUDE_CODE_OAUTH_TOKEN` or
`ANTHROPIC_API_KEY` to be set. `credential_complaint` (`issuebot/agent/accounts.py`) is the one
rule, and both the worker's startup and `validate` refuse a pool without one, naming the
variables. A single account is untouched: it keeps its own login in its own home, which is
where compose mounts `claude-home`.

**Why this rules the failure mode out by design rather than by test.** Options 2 and 3 both
end with more than one account refreshing *one* OAuth credential. If refresh tokens rotate,
the second account to refresh presents a stale one and fails with exactly the error this
deployment logged during #104's own runs — `Failed to authenticate: OAuth session expired and
could not be refreshed`. Nobody has established whether they rotate, and establishing it needs
a real login, several hours and two accounts racing: not something a session can do, and not
something to ship on a guess. The environment-borne credential removes the question instead of
answering it. There is no credential file to share, so there is no refresh for two accounts to
race over: `CLAUDE_CODE_OAUTH_TOKEN` (what `claude setup-token` mints from a subscription) and
`ANTHROPIC_API_KEY` are both long-lived values the deployment supplies, and `claude` reads them
from the environment rather than from `$HOME`.

It costs nothing to wire: `agent_environment`'s `PASSTHROUGH_PREFIXES` already carries
`ANTHROPIC_` and `CLAUDE_`, so the value reaches every session account with no allow-list
change, and `PROTECTED_ENV_PREFIXES` already stops a workspace's `.issuebot/env` from
re-pointing it. Each pool account still gets a `~/.claude` of its own for whatever `claude`
caches there, and no volume: nothing in it is a credential, so nothing in it needs to persist.

It costs the operator one step: `claude setup-token` once, into this checkout's `.env`. That
is the price of the boundary, and it is stated rather than hidden — `validate` says so, and
the worker refuses to start rather than claim issues every session would fail to authenticate.

**#101** (the session account's `~/.claude` persisting between sessions) is the same question
in sequential form, and this answers it too: under a pool there is nothing to persist, because
the credential never lands in a home.

## Design

- **The pool.** `agent.run_as` accepts a list as well as a name, normalised to a tuple either
  way (`()` is the host route); `ISSUEBOT_AGENT_USER` may be comma-separated, since an
  environment variable is a string. `run_as_pooled` — more than one name — is what the pool's
  extra rules hang off. Below the orchestrator nothing has a pool to reason about:
  `settings_with_run_as` narrows the settings to the one bound account before the runner and
  the workspace manager are built, and `session_account` is the single reader.

- **The binding is to a workspace, not to a run.** A reworked issue is dispatched again into
  the same clone, and a different uid could not write it. `AccountRegistry` is the worker's own
  record — `<workspace.root>/.issuebot/accounts.json`, `0600` in a `0700` directory, read fresh
  on every call so a restart sees it, and under an advisory lock, since `run-once` is the
  operator's debugging tool and may be run beside a live worker. `allocate` binds the
  least-loaded account that no session is currently running as, `bound` answers without
  binding, and `prune` (on the terminal sweep) forgets a workspace that is gone. Beside the workspaces rather than inside one, because the
  account has to be known before the clone and a clone needs an empty directory.

  **Never derived from the directory.** A binding computed from the workspace key would be a
  binding whoever opens the issue chooses, which is exactly the account a hostile session would
  want to be handed. Only its *expiry* is derived from the directory, and a key with a session
  running or a retry pending is kept whether its clone exists yet or not.

- **Dispatch waits rather than sharing.** `_bind_account` runs before the claim, so a candidate
  whose account is busy is left on the board instead of being moved to `in-progress` to sit
  there; a due retry in the same position is requeued rather than dropped, so its attempt count
  survives. Effective concurrency is therefore `min(max_concurrent_agents, pool size)`, and
  `validate` warns when the pool is the smaller of the two. A record that will not read is not
  "busy": it stops every candidate, so it holds dispatch as a fourth `DispatchHold` kind
  (`accounts`, #29) rather than leaving a worker that looks healthy and claims nothing.

- **The wall.** A workspace is open to exactly one account, and only while that account is
  working in it — a session running, or a removal unlinking what one left. `share_with` makes
  the directory `1770`, owner the worker (sticky, so `session.json` and `runs/` stay the
  worker's, as #75 established) and group the bound account's own: a sibling session's uid is
  in neither, so it cannot enter, list or write. `seal` puts it back to `0700` when the run
  ends, again if a removal fails partway, and for every workspace at startup, since the only
  way one stays open across a restart is a worker killed outright. The root above stays
  `0755`.

  Both halves are needed, because a workspace outlives its run. An issue sitting in `review`
  keeps its clone for days; accounts are fewer than workspaces, so the binding is many-to-one;
  and without the seal a hostile session would eventually be handed an account that also held
  an honest, idle workspace, and could rewrite the clone that issue's rework will commit and
  push — the same attack one level along. Sealed, an idle workspace is unreachable by every
  account, so its contents' own modes stop mattering; and at most one workspace per account is
  open at a time, since dispatch will not claim an issue whose account is busy.

  POSIX lets the owner of a file change its group only to one it belongs to, so the worker is a
  supplementary member of every session account's group; that membership buys the worker
  nothing else, since every session home is `0700`, and the worker is the more privileged side
  of the line in any case. `probe_run_as` asks all three questions `validate` and the worker's
  startup report before an issue is ever claimed: can the worker run a command as the account,
  can it give a workspace to that account's group (`group_complaint`), and do the pool's
  accounts have groups of their own (`pool_complaint`) — two sharing one would open every
  workspace to both.

- **A clone belongs to its account.** `_is_complete` requires `<workspace>/.git` to be owned by
  the bound account as well as requiring the worker to own the state directories, so a binding
  that moved — the pool shrank, the setting changed, a pool was turned on over an existing
  `/workspaces` volume — re-clones rather than handing the session a tree git will refuse every
  command in.

  Re-cloning means removing what is there, and that is the account's own unlink: the tree
  belongs to the *previous* binding, which neither the new account (it owns nothing in it) nor
  the worker (it owns the workspace, but not the directories inside the clone) can remove. So
  `_remove_tree` runs a delegated pass for every account that owns an entry at the top of the
  workspace as well as for the bound one, opening the directory to each in turn — the only
  thing derived from the directory is which *removals* to attempt, never the binding, and the
  sudo rule still refuses anything outside the pool. A removal the worker's own pass then fails
  re-seals, so a tree that stays on disk is never left wider than it arrived, and the account
  it belongs to does not read as busy for ever after.

- **A reloaded setting is checked again.** `agent.run_as` is a setting like any other, so a
  reload can introduce exactly what startup refuses: a delegation that does not work, a worker
  outside an account's group, two accounts sharing one, a pool with no credential in the
  environment. The probes therefore run again whenever the setting changes *and on every tick a
  hold lasts*, and a failure holds dispatch as `accounts` rather than ending the process —
  putting the file back lifts it on the next reload, and a `useradd` on the next tick, which is
  what a running deployment wants of a typo. Not every fault clears without a restart, and the
  complaint says which: the group check asks `os.getgroups()`, which is what the kernel
  authorises `share_with`'s `chgrp` by and is fixed when the process is exec'd, so a
  `usermod --append` reaches the *next* worker however plainly `id` in a new shell says
  otherwise; the environment credential is the same. Re-probing is still right — it costs one
  bounded probe a tick, it lifts what can be lifted, and the alternative is a hold that
  outlives its cause. Nothing is sealed on a
  reload, unlike at startup: sessions are running, and their workspaces are open to the
  accounts they are running as.

- **The image.** `agent-1` .. `agent-N` (uids 1011 upwards, `ISSUEBOT_AGENT_POOL_SIZE`,
  default 3) beside `agent`, all in group `agents`, and the sudo rule becomes
  `issuebot ALL=(%agents) NOPASSWD: ALL`: the worker may become any session account and
  nothing else. The worker is not in `agents`, and `sudo` is still `4750 root:issuebot`, so no
  session account can invoke it. The pool is opt-in — `ISSUEBOT_AGENT_USER` still defaults to
  `agent` — because turning it on is a credential decision the image cannot make for the
  operator.

- **`run-once` beside a live worker.** The operator's debugging tool may run while the worker
  does, so the registry's read-modify-write is under an advisory lock and `busy_accounts` reads
  which accounts have a workspace open — the one signal of a running session another process
  can see, and the reason the seal is load-bearing twice over. `run-once` refuses rather than
  take an account a session is already in; the orchestrator unions that reading with its own
  `_running`.

- **`remove`, `kill` and the probes run per account.** They already ran through `RunAs`; what
  changed is which account the orchestrator hands them. A terminal removal goes through a
  manager narrowed to *that* workspace's bound account, not the pool's first member. Startup
  probes every member, and `validate` reports the pool in its `agent.run_as` check.

## What this does not do

The worker is still one process at one uid supervising all of them; the boundary added here is
between sessions, not around the worker. The pid namespace is still shared (#75). A single
account remains supported and remains what the image defaults to, so a deployment that has not
set an environment credential is exactly as it was — including the sharing, which `validate`
now warns about rather than leaving implicit.

## Tests

`tests/test_agent_accounts.py` drives the setting, the registry, the credential rule and
`share_with` (against the running account's own group, since a unit test cannot create one);
`tests/test_orchestrator.py` proves two concurrent sessions take two accounts, that a workspace
keeps its account across a rework, that a candidate whose account is busy is not claimed, and
that a pool without an environment credential fails startup; `tests/test_cli.py` covers what
`validate` reports; `tests/test_image_layout.py` pins the Dockerfile and CI shapes. The uid
boundary itself — two accounts, a workspace each, `EACCES` both ways round — is proved by the
CI `docker` job, calling the shipped `share_with` rather than a hand-written `chmod`, because
`EACCES` across two uids is exactly what a unit test cannot see.
