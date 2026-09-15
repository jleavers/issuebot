# The session account's shell profile does not carry between sessions

Date: 2026-09-15
Status: implemented
Issue: #137 (related to #101, #75, #119)

## Problem

#101 sweeps the session account's `~/.claude` before every turn, and #119 keeps `~/.claude.json`'s
`mcpServers` out of a session. One surface of the same class was still open: the account's own
shell start-up files.

`/home/<account>` is the account's and writable by it — the image creates the account with
`useradd --create-home` and `chmod 0700`, and only `.claude` inside it is the image's own
(`install -d -m 0700 -o <account>`). Every script issuebot runs for a session runs under
`WorkspaceManager.hook_shell`, `bash -lc`: a login shell, which sources `/etc/profile` and then
the first of `~/.bash_profile`, `~/.bash_login` and `~/.profile` that exists, reaches `~/.bashrc`
through whichever of those sources it, and runs `~/.bash_logout` on the way out. `claude` takes a
shell snapshot for the session's Bash tool the same way. So a session that writes `~/.profile`
leaves a script that every later session's hooks run at the same uid, in this repository or
another, for the container's lifetime — which under `restart: unless-stopped` spans worker
restarts.

Persistence *within* the session's privilege domain, like #101 and not an escalation across it
(#75 closed that): outside the volume, so it does not survive a container recreate or reach
another host, and outside `~/.claude`, so outside #101's literal reach.

Measured on `origin/main`@42868cf, through `WorkspaceManager.run_hook` at its own default hook
shell with a planted `~/.profile`: the hook's stdout is `PLANTED-PROFILE-RAN\nhook-ran`, and
`_sweep(~/.claude)` leaves the file where it is.

**Invariant.** A session cannot leave a shell start-up file in the session account's home that a
later session's hook, post-clone setup or Bash tool snapshot runs.

## Design

The sweep of #101 is aimed one directory up, at the *home*, and runs before every login shell as
well as before every turn.

- **`SHELL_STARTUP_SWEEP`** (`agent/runas.py`): `.bash_profile`, `.bash_login`, `.profile`,
  `.bashrc`, `.bash_logout` — what `bash` and `sh` themselves read, checkable against `bash(1)`.
  `.bash_aliases` and friends are deliberately absent: nothing reads them but a `.bashrc` that
  sources them, and `.bashrc` is on the list. Pinned by a test, as `CLAUDE_HOME_SWEEP` is.

- **The `sweep` verb takes the home**, not `~/.claude`: `sudo -n -u <account> -- python -P -m
  issuebot.agent.runas sweep <home>`, and `_sweep_targets` yields the start-up files from the home
  and the `.claude` surfaces from `home / CLAUDE_HOME_DIR`. `RunAs.sweep_home()` defaults to the
  account's own home (`pw_dir`). Everything else about the sweep is unchanged: still a denylist,
  still best-effort, still unlinking a symlink rather than following it.

- **`WorkspaceManager._run_script` sweeps first.** This is the half a per-turn sweep cannot do:
  the post-clone setup and the `after_create` and `before_run` hooks all run at the session's uid
  *before* `_turn_loop` reaches its first sweep, so the previous session's `~/.profile` would run
  in this session's first login shell. `_run_script` is the one seam every hook and the setup go
  through, and it is the right one because what matters is the login shell rather than which hook
  opened it; `_run_argv`'s other caller is the clone, `gh` as an argv, which reads no start-up
  file. A hook that is not configured returns before `_run_script` and so opens nothing and sweeps
  nothing. `session._turn_loop`'s call stays exactly as it was.

- **Nothing else changes.** No image change, no ownership change, no new setting. On the host
  route (`agent.run_as` unset) the sweep is still a no-op, so nothing ever removes a developer's
  own `.profile`; `probe_run_as` refuses an account that is this process's own in any case.

## Why the sweep, not the image

The issue offered three routes. The sweep is the one taken.

- **Not `/home/<account>` root-owned with only `.claude` (and `.claude.json`) writable.** Session
  accounts write in their home outside `.claude` today, and not only through `claude`: `gh` creates
  `~/.local/state/gh/device-id`, and the image's own CI proves `npm ci` against a writable
  `$HOME/.npm` — which is why the optional Node toolchain exists at all. A home whose writable
  entries are an enumerated list would break whichever tool a target repository's hooks reach for
  next, one session at a time, to close a residual the sweep closes without guessing. `claude` can
  still write what it needs because nothing about the home's ownership or mode moved.

- **Not "hooks stop running as a login shell".** `/etc/profile` and `/etc/profile.d` are where the
  image puts `node`, `npm` and the PostgreSQL binaries on a hook's `PATH` (Debian's `/etc/profile`
  overwrites `PATH` for a login shell, which is why those drop-ins exist); dropping `-l` would take
  the toolchain off `PATH` for every hook that drives them. It would also leave claude's own shell
  snapshot, which this does not control, to be shown not to source the files.

- **Not leaving the start-up files in place and root-owned.** The home is the account's and
  writable, so the account can unlink a root-owned file in it and write its own.

## Residuals

- **The denylist**, as in #101: a shell issuebot does not run today (`zsh`'s `~/.zshenv`, say) is
  not on the list, and the list is what `hook_shell` and claude's snapshot actually read.
  Fails safe — a gap, never a broken session — and pinned by a test.

- **Concurrency**, exactly as in #101: with one account for the deployment a session running
  beside this one can plant between a sweep and the shell it protects. A pool closes it, since no
  two concurrent sessions share a home.

- **Other dotfiles a tool executes.** `~/.gitconfig` (aliases, `core.pager`) and `~/.ssh/config`
  (`ProxyCommand`) are the same shape one tool further out, and neither is a shell start-up file;
  out of this issue's scope, filed rather than folded in.

- **`~/.claude.json`**, unchanged from #101 and #119: claude's own file, kept by a denylist that
  does not name it, with its one executable surface closed by `--strict-mcp-config`.

## Tests

`tests/test_agent_runas.py`: `_sweep` removes every start-up file and keeps what the sweep does
not name (`.claude.json`, `gh`'s state, npm's cache, `.claude` itself); a symlinked `.profile` is
unlinked, never followed; the list is pinned; `RunAs.sweep_home` delegates the home and defaults to
`pw_dir`; and, end to end, a planted `~/.profile` does not run for the next session's hook — the
real wrapper, the real `bash -lc`, the account's home substituted through `pwd.getpwnam` so no real
home is swept, and only `sudo` a fake, because a uid change is the one thing the suite cannot have.
That test is two-sided: with the sweep removed the plant *is* what the hook runs.
`tests/test_agent_session.py` records the order — a sweep before the post-clone setup, before each
configured hook and before every turn, and none for a hook that is not configured.
`tests/test_image_layout.py` pins the CI step. The CI `docker` job proves it in the real image,
the real uid split and the real home, through the worker's own `RunAs("agent").sweep_home()`: the
same `bash -lc` runs before the sweep, where the plant must run, and after it, where it must not,
and `claude --version` still answers for the account both directly (the README's login recipe) and
in a login shell once the start-up files are gone.
