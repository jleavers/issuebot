# A `.issuebot/env` line does not decide what the next session's hook shell runs

Date: 2026-09-22
Status: implemented
Issue: #179 (related to #171, #151, #137, #101, #126, #104, #121)

## Problem

`WorkspaceManager.hook_shell` is `bash -lc`, and every script issuebot runs for a session goes
through it: the post-clone setup and all four hooks (`after_create`, `before_run`, `after_run`,
`before_remove`). `bash` reads a handful of variables out of the environment it is handed and
acts on them *before*, or *around*, the command it was given — `BASH_ENV` first among them.

`.issuebot/env` is the workspace file a hook writes to hand variables to the session, and
`merge_workspace_env` refuses only the names in `PROTECTED_ENV_NAMES` and the prefixes in
`PROTECTED_ENV_PREFIXES`. None of the shell's own names was in either list, and none of them is
in `PASSTHROUGH_NAMES` or `PASSTHROUGH_PREFIXES` — so `.issuebot/env` was the one route in, and
it was open.

Measured on `main`@c7efea9, GNU bash 5.2.37, `/bin/sh` → dash, first as the issue reports it:

```text
$ printf 'printf "SOURCED:%s\n" "$MARK"\n' > /tmp/marker.sh
$ MARK=bash-lc-BASH_ENV BASH_ENV=/tmp/marker.sh bash -lc 'echo hook-ran'
SOURCED:bash-lc-BASH_ENV
hook-ran
```

and then end to end through issuebot's own seam, `agent_environment` layered with a real
`.issuebot/env` by `workspace_environment` and the merged environment handed to the shell a hook
gets:

```text
$ uv run python .issuebot/before.py
workspace_env_applied  keys=['BASH_ENV', 'SHELLOPTS', 'PS4', 'DATABASE_URL']
applied: ['BASH_ENV', 'SHELLOPTS', 'PS4', 'DATABASE_URL']
hook stdout: ['PLANTED-BASH_ENV-RAN', 'hook-ran']
hook stderr (first 2): ['PPLANTED-PS4-RAN+id -u', "PLANTED-PS4-RAN+'[' 1012 -eq 0 ']'"]
```

Two commands of the planted file's choosing ran before the hook's own `echo` — one from the file
`BASH_ENV` named, one from the substitution in `PS4`, during `/etc/profile`.

**This is #137's channel in environment-variable form.** `SHELL_STARTUP_SWEEP` (`runas.py`)
removes `~/.bash_profile`, `~/.bash_login`, `~/.profile`, `~/.bashrc` and `~/.bash_logout` from
the session account's home before every login shell and every turn, precisely because that login
shell sources them. `BASH_ENV` names such a file without writing one, so the sweep's guarantee
was conditional on a variable nothing checked — the shape #171 found for `TOOL_CONFIG_SWEEP` and
`GIT_CONFIG_GLOBAL`, one tool further out again.

**Reach.** Narrower than the home's, and the same as #171's: the file is in one workspace, a
workspace belongs to one issue and is bound to one account and sealed `0700` between runs (#121,
#75), so this is a channel from one session to the *next session on that issue*, not to every
later session at that uid. Persistence inside the session's privilege domain, as in #101, #137
and #151 — not an escalation across it, which #75 closed.

**Invariant.** A line in `.issuebot/env` cannot reach the next session's shell *through the
variables that shell itself reads*: it cannot name a file `bash` sources, make it evaluate text
of the line's choosing around the hook's commands, or send the hook's own relative paths into a
tree the line chose. What the hook wrote is what `bash` runs, after `/etc/profile` and the
image's `profile.d` entries, which are root's. Stated with that qualifier on purpose: this is a
bound on `bash`'s own start-up variables and not on everything that can influence a process the
session's environment reaches -- the dynamic loader's names are the residual below, and the file
is a denylist throughout.

## Decision

**`BASH_ENV` is protected, with the rest of what `bash` itself reads; `ENV` is not, because it
is not reachable through any shell issuebot runs.**

`SHELL_ENV_NAMES` (`agent/runner.py`, beside `TOOL_CONFIG_ENV_NAMES`, joined to
`PROTECTED_ENV_NAMES` and pinned by a test):

| name | what `bash` does with it |
|---|---|
| `BASH_ENV` | sources the file it names before the command the shell was given |
| `SHELLOPTS` | enables `set -o` options from the environment before any start-up file |
| `BASHOPTS` | the `shopt` half of the same thing |
| `PS4` | expanded before every traced command, command substitution and all |
| `CDPATH` | the directories a `cd` with a relative argument resolves through |

Each measured, against the image's own `bash` 5.2:

```text
$ BASH_ENV=/tmp/marker.sh bash -lc 'echo hook-ran'
SOURCED:bash-lc-BASH_ENV
hook-ran
$ env SHELLOPTS=xtrace bash -lc 'echo hook-ran'
++ id -u
+ '[' 1012 -eq 0 ']'                       (... /etc/profile, traced, then:)
+ echo hook-ran
$ env BASHOPTS=xpg_echo bash -lc 'shopt xpg_echo'
xpg_echo   on
$ env SHELLOPTS=xtrace PS4='$(date +%s > /tmp/probe/ps4.out)+ ' bash -lc 'echo hook-ran'
hook-ran
$ ls -l /tmp/probe/ps4.out                 (written: the substitution in PS4 ran)
$ cd ws && env CDPATH=/tmp/probe/elsewhere bash -lc 'cd sub >/dev/null && sh ./build.sh'
PLANTED-CDPATH-RAN                         (without CDPATH: HONEST-RAN, the workspace's own)
```

The rule is *what `bash` reads out of the environment it is handed and acts on before, or
around, the commands the hook wrote* — checkable against `bash(1)`'s "Shell Variables" and
"Invocation", and finite. It is the same shape as #171's "the tails of git's and gh's own
precedence chains": a statement someone can re-derive from the tool's manual, rather than a list
of everything that looked dangerous on the day.

`SHELLOPTS` and `BASHOPTS` run nothing by themselves and are here for what they turn on;
`PS4` does nothing without `xtrace`. Protecting either alone would close nothing, which is why
both halves are in. (They read as `readonly` when the *invoking* shell is itself `bash`, which
is why they were measured through `env` — an accident of the probe, not a defence: the worker
hands the hook its environment from Python, and under `agent.run_as` through `RunAs`'s
descriptor, so neither is a shell assignment.)

`CDPATH` is the one entry that is not about start-up. It is here because it is `PATH`'s rule for
the one lookup `PATH` does not cover: `PATH` is protected so that `git` is the `git` issuebot
installed, and `CDPATH` decides which `./build.sh` a hook's `cd sub && ./build.sh` finds.

### Why `ENV` is not protected

`ENV` is POSIX's start-up file for an **interactive** shell, and nothing issuebot runs is
interactive. Measured, with the same marker script and `MARK` naming the case:

```text
$ MARK=bash-lc-ENV  ENV=/tmp/marker.sh bash -lc 'echo hook-ran'          -> hook-ran only
$ MARK=bash-posix   ENV=/tmp/marker.sh bash --posix -c 'echo hook-ran'   -> hook-ran only
$ MARK=bash-as-sh   ENV=/tmp/marker.sh /tmp/shbin/sh -c 'echo hook-ran'  -> hook-ran only
$ MARK=dash-c-ENV   ENV=/tmp/marker.sh dash -c 'echo hook-ran'           -> hook-ran only
$ MARK=sh-c-ENV     ENV=/tmp/marker.sh sh -c 'echo hook-ran'             -> hook-ran only
```

`bash` reads `BASH_ENV` and not `ENV` when invoked as `bash`; in posix mode it reads neither,
which is the same rule seen from the other side. The only other shell in the picture is the
`sh -c` of `cli._mcp_unreadable_by`, which is `validate`'s own probe, runs `test -r` under
`os.environ` and never sees this file at all.

So `ENV` stays out, for the reason `XDG_CONFIG_DIRS` stayed out of #171: the list states what
was shown to work, and a name that closes nothing is a name a later reader has to re-derive the
absent threat for. The README says so where a hook author will be reading, and a test pins it
beside `PS1`, `PS2` and `BASH_XTRACEFD` as deliberately unprotected.

### Why protect, rather than document the residual

The issue offered both. Protecting wins on three counts.

**It is the rule the list already states.** `PATH` is protected so `gh` and `claude` keep
running as issuebot launched them; the `ANTHROPIC_`/`CLAUDE_` prefixes so the file cannot
re-point the `claude` issuebot launches next; `GIT_`/`GH_` and the chain tails so it cannot
re-point that session's `git` or `gh` (#171). `bash` is not one more tool reached *from* a hook:
it is the process every hook and the post-clone setup *is*. Leaving the one tool that runs all
of them out of a bound drawn for the tools it runs would be arbitrary.

**The cost is as close to zero as a protection gets.** Nothing in the tree sets any of the five —
no `Dockerfile` line, no compose service, no hook in the README's recipes. A hook that wants a
file sourced before its own commands has `source` in the script it already owns; one that wants
a trace has `set -x`; one that wants a directory has an absolute path. Each is *inside the
script the hook already writes*, and none of them is a hand-over to the next session, which is
the only thing being refused.

**The sweep is already there, and this completes it.** #137 decided that the files a login shell
sources must not persist between sessions. The variable form reaches the same shells with the
same effect, so protecting it is not a new position — it is the position #137 took, held against
the spelling that does not touch the home.

## What this does not close

- **The clone's own `.git/config`, and the workspace generally.** A workspace outlives its run
  (#180). Nothing here changes that; what is closed is the `bash` issuebot starts, not the files
  inside the tree it starts it in.
- **`BASH_FUNC_<name>%%`**, bash's exported-function import, measured defining a command in the
  shell it starts (`env 'BASH_FUNC_hookcmd%%=() { printf FUNC-RAN\n; }' bash -lc 'hookcmd'` →
  `FUNC-RAN`). It is not in `SHELL_ENV_NAMES` because it cannot be written here at all:
  `parse_workspace_env` accepts `[A-Za-z_][A-Za-z0-9_]*` as a key, so the `%%` is a
  `line N: not a variable name` complaint. Recorded — with its own test — so that a later change
  to the key pattern has this consequence written down beside it rather than re-derived.
- **`PROMPT_COMMAND`.** The best-known "variable that makes `bash` run a command", and out on
  the measurement rather than by oversight: it is run before each *interactive* prompt, and
  nothing issuebot runs draws one. Named here so the next reader does not have to re-derive it.
  `POSIXLY_CORRECT` was measured too, since it is the one name that could have changed which
  start-up file is read: it makes `bash` skip `BASH_ENV` and does not make it read `ENV`.
- **`BASH_XTRACEFD`, `PS1`, `PS2`, `IFS`, `GLOBIGNORE`.** Measured or read as behaviour-only:
  they shape output, prompts and word splitting, and none of them names or produces a command.
  Out, by the rule.
- **The dynamic loader: `LD_PRELOAD`, `LD_AUDIT`, `LD_LIBRARY_PATH`.** Named separately from
  the bullet below rather than folded into it, because the usual reason for excluding another
  tool's variables does not apply: these reach *every* dynamically linked program the session's
  environment is handed to, `bash`, `git`, `gh` and the `claude` child included, which is the
  same set this change is about. They are out because the rule here is about what `bash`
  reads at start-up, and a loader that runs code out of a `.so` is a different question with a
  different answer — `LD_LIBRARY_PATH` in particular is something a target repository's build
  legitimately sets, which `BASH_ENV` never is. Lower reach in practice, since the default
  image carries no compiler and a session would need a prebuilt object, but not zero. #171 had
  `LD_PRELOAD` in the bullet below and this note inherited it; separating it is the honest
  filing, and the decision itself is #187 rather than something settled here.
- **Other tooling's variables** — `NODE_OPTIONS`, `PYTHONSTARTUP` and their kind. `.issuebot/env`
  is a denylist and has to be: its purpose is handing over what a target repository's tests
  need, which cannot be enumerated in advance. So this bounds the tooling *issuebot itself*
  launches — `bash`, `git`, `gh`, `claude` — and is never a claim that the next session's
  environment is uninfluenced. Fails safe: a gap is a name that still gets through, never a
  broken session.
- **`/etc/profile` and `/etc/profile.d`.** A login shell still sources them, and it should: they
  are root's, outside the session's privilege domain, and they are where the image puts node,
  `uv` and the PostgreSQL binaries on `PATH`.
- **The refusal is visible only in the worker's log** (`workspace_env_ignored`, one line naming
  the key), not to the hook that wrote it. That is the existing behaviour for every protected
  name; the README is where a hook author finds the rule before debugging a variable that
  silently did not arrive.

## Tests

`tests/test_agent_runner.py`: `merge_workspace_env` refuses each of the five, one parametrised
case per name annotated with what `bash` does with it, and still applies an ordinary key beside
it. Four negative cases pin that the bound stops where it says it does — `ENV`, the decision of
this change, beside `BASH_XTRACEFD`, `PS1` and `PS2`. `SHELL_ENV_NAMES` is pinned as a list, as
the sweep lists and `TOOL_CONFIG_ENV_NAMES` are, so dropping a name is a deliberate edit in two
places, with the rule in the docstring. End to end through `workspace_environment` with a real
`.issuebot/env`, a file carrying all five plus a `DATABASE_URL` hands the next turn its DSN and
nothing else, with one `workspace_env_ignored` line per key naming the key and never its value.
And the `BASH_FUNC_git%%` spelling is pinned as refused by the key pattern.

`tests/test_agent_workspace.py`: the channel through the shell a hook actually gets. A
`before_run` hook writes `BASH_ENV=<planted script>` and a `DSN` line into `.issuebot/env`; the
`after_run` hook — run through `bash -lc`, the shipped `hook_shell` — prints `hook-ran` and the
DSN. Its stdout is exactly those two lines, the planted script having run nowhere, and the
worker's log carries `BASH_ENV is protected`.

Two-sided: with `*SHELL_ENV_NAMES` taken back out of `PROTECTED_ENV_NAMES`, all eight of these
tests fail, and the workspace one fails by printing `PLANTED-BASH_ENV-RAN` ahead of the hook's
own output in `hook_finished`.
