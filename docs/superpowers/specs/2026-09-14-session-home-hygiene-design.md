# The session account's ~/.claude does not carry config between sessions

Date: 2026-09-14
Status: implemented
Issue: #101 (related to #75)

## Problem

After #75 the session runs as its own account (`agent`, uid 1001), and the `claude-home`
volume is that account's `~/.claude`. That home is shared by every session and survives
restarts, so anything one session writes there — `~/.claude/commands/`, `~/.claude/skills/`,
`~/.claude/rules/`, `~/.claude/agents/`, `~/.claude/workflows/`, `~/.claude/plugins/`, a
user-level `CLAUDE.md`, `settings.json`, the auto memory under `~/.claude/projects/<project>/memory/`
— is read by every later session at the same privilege, in this repository or another on the
same host. This is persistence *within* the session's privilege domain, not an escalation
across it (#75 closed the escalation): one hostile issue can leave a slash command, a skill, a
rule or a memory note that a session working a different issue next week loads and runs.

What `claude.setting_sources` gates, per the SDK's `settingSources` table: the `user` source
is what loads the user-level `settings.json` and its hooks, `CLAUDE.md`, `rules/`, `skills/`,
`commands/` and `agents/`, so the shipped workflow's `[project]` already keeps those six out
of a turn. But the setting defaults to unset, which is every source, an overlay's
`setting_sources: null` drops the pin by the documented merge rule, and `plugins/`,
`output-styles/`, `workflows/` and `agent-memory/` are not in that table at all. Two inputs
are read whatever the flag says: auto memory (`projects/<project>/memory/MEMORY.md`, loaded
into the system prompt at session start, keyed by git repository, and excluded from claude's
own retention sweep so it never ages out) and `~/.claude.json`. And the credential
(`.credentials.json`, which rotates its refresh token) has to stay writable, so the volume
cannot simply be made read-only.

`--bare`, the "`--setting-sources`-style gate" the issue floated, does skip all of these, but
it never reads OAuth credentials, so it would break the claude.ai login the README's recipe
exists for. It is not an option while the deployment authenticates that way.

**Invariant.** A session cannot leave anything under the session account's `~/.claude` that a
later session loads as instructions or behaviour, other than the credential.

## Design

Immediately before each `claude -p` turn, the worker sweeps the loadable config surfaces from
the account's `~/.claude`, keeping the credential and claude's own per-session runtime state,
and auto memory is switched off in the session's environment so nothing writes it.

- **`issuebot.agent.runas` grows a `sweep` verb.** The home is the account's and closed to the
  worker's uid, so the sweep is delegated exactly as the clone, the kill and the removal are:
  `sudo -n -u agent -- python -P -m issuebot.agent.runas sweep <~/.claude>`, run by the worker's
  own root-owned interpreter as the session's account. `CLAUDE_HOME_SWEEP` names what it
  removes — `CLAUDE.md`, `rules`, `skills`, `commands`, `agents`, `workflows`, `agent-memory`,
  `plugins`, `output-styles`, `settings.json`, `settings.local.json` — and
  `CLAUDE_HOME_MEMORY_DIR` (`projects`, `memory`) adds each project's auto memory directory,
  reached by walking `projects/` rather than by a glob so that a symlink at `projects` or at a
  project entry is skipped rather than followed. Any target that is a symlink is unlinked
  rather than followed, so a link a session planted cannot redirect the removal outside the
  home. `RunAs.sweep_home()` defaults the path to the account's own `~/.claude`; the tests pass
  an explicit path so the sweep is proved without touching a real home. Unlike the kill and the
  removal it reports whether the helper ran and exited 0, and `sweep_agent_home` logs
  `claude_home_sweep_failed` at WARNING when it did not: the turn still runs, since the startup
  probe proved sudo can become the account and the next turn sweeps again, but a control that
  silently never ran would be no control.

- **`WorkspaceManager.sweep_agent_home()`** delegates it, off the event loop, and
  `session._turn_loop` calls it immediately before every `runner.run_turn`, the first included.
  Every turn rather than once per session because sessions run concurrently
  (`agent.max_concurrent_agents`, default 3, 2 in the shipped workflow) and each turn is a
  fresh `claude -p --resume` that re-reads `~/.claude`: a sweep at session start would leave
  everything a concurrent session planted afterwards to be loaded by this session's later
  turns. The `before_run` hook runs as the account too, and the first turn's sweep follows it.
  Under `agent.run_as` only: on the host route the home is the operator's own and is left
  untouched, and the container is the boundary regardless.

- **`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in `FIXED_ENVIRONMENT`** (`agent/runner.py`), which
  the docs name as the multi-tenant setting. It is a fixed entry, so it reaches every turn and
  every hook, and it is protected like the other fixed entries, so a workspace `.issuebot/env`
  line cannot switch it back on. The sweep still removes `projects/<project>/memory/`, since an
  image built before this setting, or a session writing there by hand, can have left one.

## Why a denylist sweep, not the alternatives

- **Not a per-session `HOME` with the credential shared in.** The credential rotates its
  refresh token, so a symlinked or copied-in `.credentials.json` has to persist the rotation
  back to the volume across an atomic-rename write, and getting that wrong breaks
  authentication for every session. The sweep never moves the credential, so auth, rotation and
  the login recipe (`docker compose run --rm --user agent --entrypoint claude worker`) are
  untouched. A per-session `CLAUDE_CONFIG_DIR` would close the concurrency residual below too,
  and hits the same wall.

- **Not a whole-home allowlist sweep that keeps only `.credentials.json`.** A whole-home sweep
  at one session's start would delete a concurrent session's live `projects/`/`sessions/`
  transcripts and break its `--resume`. The swept paths are never claude's own runtime state,
  so the denylist sweep does not interfere with a concurrent session.

## Residuals

- **The denylist.** It misses a new claude user-config location until that is added to
  `CLAUDE_HOME_SWEEP` by hand; it fails safe (a security gap, not a broken session) where an
  allowlist would fail unsafe (wiping a runtime dir claude adds). The list is pinned against
  the `claude-directory` docs by a test, so a change is a deliberate edit in both places.

- **Concurrency.** The sweep runs immediately before a turn's `claude -p`, but a session
  running beside it can plant between that sweep and that start, and claude reloads a personal
  skill on change within a running process. The window is one turn's, not a session's, and
  closing it takes a per-session config directory, which is the credential problem above.

- **`~/.claude.json`** (user-scoped `mcpServers` and project-trust state) is read whatever
  `setting_sources` says. It sits in `$HOME`, outside the mounted volume and outside this
  issue's literal scope, and a single long-running worker container serves many sessions over
  its lifetime. Its one executable surface is closed by #119: every turn runs with
  `--strict-mcp-config`, so no server named in the file, or in a repository's `.mcp.json`,
  reaches a session (`2026-09-14-mcp-config-confinement-design.md`). The rest of the file --
  account metadata, trust state, the `projects` map -- persists for the container's lifetime
  and is not instructions.

- **The account's shell profile** (#137). `/home/agent` is the account's and writable, only
  `.claude` in it is the volume, and hooks run under `bash -lc`, so a `~/.profile` one session
  writes is sourced by every later session's hooks for the container's lifetime. The same
  class as this issue, one directory up; filed rather than folded in.

## Tests

`tests/test_agent_runas.py` drives the `sweep` verb and `RunAs.sweep_home` on tmp homes (every
named surface and a project's `memory/` removed, the credential, the project's transcript and
the rest of the runtime kept, a symlinked surface unlinked not followed, a symlinked project or
`projects` never walked into) and the delegation through the fake `sudo`, and pins
`CLAUDE_HOME_SWEEP` against the docs; `tests/test_agent_runner.py` pins the auto-memory
variable as fixed and protected; `tests/test_agent_session.py` records the sweep immediately
before every turn; `tests/test_agent_workspace.py` proves `sweep_agent_home` is a no-op on the
host route; `tests/test_image_layout.py` pins the CI step. The CI `docker` job proves it in the
real image and the real uid split, through the worker's own `RunAs("agent").sweep_home()` with
its default target: a planted `commands/evil.md`, `skills/evil/SKILL.md`, `rules/evil.md` and
`projects/<project>/memory/MEMORY.md` are gone after the worker sweeps, while
`.credentials.json` and the transcript beside the memory remain.
