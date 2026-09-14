# The session runs at a different privilege from its supervisor

Date: 2026-09-14
Status: implemented
Issue: #75 (security sweep findings `copycat-1`, `copycat-2`, `copycat-7`, `hostile-issue-4`)

## Problem

`claude -p` was spawned with no `user=`, no `preexec_fn` and no namespace change, so the
session ran at the worker's own uid, in its container and pid namespace, and with its `HOME`.
Everything the worker used to supervise and authenticate the session then sat on the session's
side of a line nobody drew: `/app` (the worker's code and venv, `--chown`ed to the agent's
uid), `/home/issuebot/.claude` (the live OAuth credential, inside the passed-through `HOME`),
`.issuebot/session.json` (which configures the next turn) inside the workspace the session
owns, and `/proc/<worker>/environ` (every variable the `agent_environment` allow-list withheld,
`GH_TOKEN` and the database URL among them). The tree documented three finer boundaries — the
allow-list, the workspace, the protected-key list — as if that co-location did not exist. #67
had already ruled that the protected-key list "is about not letting a typo take `gh` or
`claude` down mid-run, **not about hostile input**"; this change makes the boundary that ruling
assumed real.

**Invariant.** Nothing the session can read or write at its own privilege is an input to the
process that supervises or authenticates it: the worker's code and interpreter, its
environment, its credential store, and the configuration of the session's next turn all lie
outside the session's uid and `HOME`.

## Design

The fix lives at the process boundary at spawn, defined jointly by the runner's spawn call, the
image's uid layout and the compose mounts.

- **Two accounts.** `issuebot` (uid 1000) is the worker; `agent` (uid 1001) is the session.
  `claude -p`, every hook, the clone and the post-clone setup run as `agent`. `/app` is
  root-owned and writable by neither; `/home/issuebot` and `/home/agent` are each closed to the
  other; the login the session authenticates with lives in `/home/agent/.claude`, which is
  where compose mounts `claude-home`.

- **The worker stays unprivileged.** It does not run as root and drop; the process that parses
  the session's output is never the most privileged one in the container. Instead `sudo`
  carries exactly one rule — `issuebot ALL=(agent) NOPASSWD: ALL` — and its binary is
  `4750 root:issuebot`, so the session's uid cannot invoke `sudo` at all, not even to be
  refused. The worker can become `agent` and nothing else; it cannot become root.

- **`issuebot.agent.runas`.** The delegation wrapper. `RunAs.prepared` builds the argv
  `sudo -n -u agent -C <fd+1> -- python -m issuebot.agent.runas exec --env-fd N -- <argv>`. The
  session's environment (the `agent_environment` allow-list plus the workspace's
  `.issuebot/env`) crosses the uid change on an anonymous file (`anonymous_fd`: a memfd
  where the interpreter has `os.memfd_create`, an immediately unlinked file on a tmpfs where
  it does not, which `uv`'s CPython regularly does not — #115), not through sudo's
  environment policy, so what the session sees is exactly what the worker built, with
  `HOME`/`USER`/`LOGNAME` the account's own. The `exec` verb — run by the worker's own
  root-owned interpreter, `-P` so the agent's workspace cwd cannot shadow the package —
  installs that environment whole and execs. `kill`
  (the session's process group, which the worker's uid may not signal) and `remove` (the
  session's files under a workspace, which the worker's uid may not unlink) are the other two
  verbs; a `probe` reports whether the delegation works at all.

- **Workspace state is the worker's.** Under `agent.run_as` the workspace directory and its
  `.issuebot` are created by the worker and made sticky (`1777`): the session writes what it
  likes inside them but can neither unlink nor rename the worker's entries, so `session.json`
  and `runs/` stay the worker's. `session.json` is trusted only when the worker owns it, and is
  written under a freshly created name the session could not pre-place. A repository that ships
  its own `.issuebot` is refused rather than have worker state kept in a session-owned
  directory.

- **git's ownership check.** Splitting the uid puts the worktree and the account that works in
  it on opposite sides of a check git makes for itself: a repository whose worktree belongs to
  another account is one git refuses to touch (`detected dubious ownership`, which `git config
  --local` reports downstream as "--local can only be used inside a git repository"). The
  workspace directory is deliberately the worker's, so the image declares the workspace root
  safe system-wide, `safe.directory = /workspaces/*`. Scoped to that root rather than a bare
  `*`: the only other account that can own anything under it is the worker, the more privileged
  side. Without it every git command a session runs fails, the post-clone setup first — which
  is how this reached a live board rather than CI, where the uid layout was proved but no
  repository was ever cloned into a directory the worker owned.

- **Startup and validation.** The worker probes the delegation at startup and refuses to start
  when `agent.run_as` is set but cannot be established, the same shape as the `claude auth`
  probe: a boundary that does not work would otherwise fail every run. `validate` reports the
  account as a fifteenth check, and warns when `agent.run_as` is unset (the host route, where
  the session runs as the operator's own user).

- **The host route is unchanged.** `agent.run_as` unset — the default, the tests, `uv run` on
  a developer's machine — runs everything as the worker exactly as before, save that the
  workspace's `.issuebot/runs` is pre-created and a `created` marker file replaces the bare
  `.issuebot` directory as the completion sentinel. The image sets `ISSUEBOT_AGENT_USER=agent`,
  the setting's fallback, so every container splits the privilege without a workflow change.

## What this does not do

The Anthropic credential is necessarily the session's own — `claude` authenticates itself — so
the split moves it into the session's home rather than hiding it from the session. The pid
namespace is shared; the uid, not a namespace, is what makes `/proc/<worker>/environ`
unreadable. Docker-in-Docker and a mounted host socket stay rejected (#62): this adds no
privilege, it removes shared privilege.

## Tests

`tests/test_agent_runas.py` drives the wrapper, its helper and the seams above it with a fake
`sudo` that execs as the same uid (a unit test cannot change uid); `tests/test_image_layout.py`
pins the Dockerfile, compose and CI shapes the CI `docker` job proves; the runner, workspace,
CLI and orchestrator suites cover the wiring. The image layout itself — the uids, the one sudo
rule, the unreadable environ and home, claude under both accounts — is proved by the CI
`docker` job on every PR, since it is exactly what a unit test cannot see.
