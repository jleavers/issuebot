# The session account's ~/.claude does not carry config between sessions

Date: 2026-09-14
Status: implemented
Issue: #101 (related to #75)

## Problem

After #75 the session runs as its own account (`agent`, uid 1001), and the `claude-home`
volume is that account's `~/.claude`. That home is shared by every session and survives
restarts, so anything one session writes there — `~/.claude/commands/`, `~/.claude/agents/`,
`~/.claude/plugins/`, a user-level `CLAUDE.md`, `settings.json` — is read by every later
session at the same privilege, in this repository or another on the same host. This is
persistence *within* the session's privilege domain, not an escalation across it (#75 closed
the escalation): one hostile issue can leave a slash command or an agent definition that a
session working a different issue next week loads and runs.

`claude.setting_sources: [project]` (the default workflow) already keeps a user-level
`settings.json` and its hooks out of a turn, but commands, agents, plugins, output styles and
the user memory file are not gated by that flag, and the credential (`.credentials.json`, which
rotates its refresh token) has to stay writable, so the volume cannot simply be made read-only.

**Invariant.** A session cannot leave anything under the session account's `~/.claude` that a
later session loads as instructions or behaviour, other than the credential.

## Design

Before each session's first `claude -p`, the worker sweeps the loadable config surfaces from
the account's `~/.claude`, keeping the credential and claude's own per-session runtime state.

- **`issuebot.agent.runas` grows a `sweep` verb.** The home is the account's and closed to the
  worker's uid, so the sweep is delegated exactly as the clone, the kill and the removal are:
  `sudo -n -u agent -- python -m issuebot.agent.runas sweep <~/.claude>`, run by the worker's
  own root-owned interpreter as the session's account. `CLAUDE_HOME_SWEEP` names what it
  removes — `CLAUDE.md`, `commands`, `agents`, `plugins`, `output-styles`, `settings.json`,
  `settings.local.json` — and a symlink is unlinked rather than followed, so a link a session
  planted cannot redirect the removal outside the home. `RunAs.sweep_home()` defaults the path
  to the account's own `~/.claude`; the tests pass an explicit path so the sweep is proved
  without touching a real home.

- **`WorkspaceManager.sweep_agent_home()`** delegates it once per session, off the event loop,
  and `session._execute` calls it after the workspace is ready and before the turn loop. Under
  `agent.run_as` only: on the host route the home is the operator's own and is left untouched,
  and the container is the boundary regardless.

## Why a denylist sweep, not the alternatives

- **Not a per-session `HOME` with the credential shared in.** The credential rotates its
  refresh token, so a symlinked or copied-in `.credentials.json` has to persist the rotation
  back to the volume across an atomic-rename write, and getting that wrong breaks
  authentication for every session. The sweep never moves the credential, so auth, rotation and
  the login recipe (`docker compose run --rm --user agent --entrypoint claude worker`) are
  untouched.

- **Not a whole-home allowlist sweep that keeps only `.credentials.json`.** Sessions run
  concurrently (`agent.max_concurrent_agents`, default 2). A whole-home sweep at one session's
  start would delete a concurrent session's live `projects/`/`sessions/` transcripts and break
  its `--resume`. The swept paths are never claude's own runtime state, so the denylist sweep
  is concurrency-safe.

- **The residual.** A denylist misses a new claude user-config location until it is added to
  `CLAUDE_HOME_SWEEP` by hand; it fails safe (a security gap, not a broken session) where an
  allowlist would fail unsafe (wiping a runtime dir claude adds). `~/.claude.json` (MCP servers,
  trust state) sits outside the volume — recreated with each container, so it does not persist
  across restarts or repositories — and is left to a follow-up rather than risk `claude -p`'s
  onboarding by removing it here.

## Tests

`tests/test_agent_runas.py` drives the `sweep` verb and `RunAs.sweep_home` on tmp homes (the
config removed, the credential and runtime kept, a symlink unlinked not followed) and the
delegation through the fake `sudo`; `tests/test_agent_workspace.py` proves `sweep_agent_home`
is a no-op on the host route; `tests/test_image_layout.py` pins the CI step. The CI `docker`
job proves it in the real image and the real uid split: a planted
`~/.claude/commands/evil.md` is gone after the worker sweeps, while `.credentials.json` and a
transcript under `projects/` remain.
