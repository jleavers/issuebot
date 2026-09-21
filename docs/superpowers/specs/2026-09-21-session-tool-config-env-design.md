# A `.issuebot/env` line does not re-point `git` or `gh` for the next session

Date: 2026-09-21
Status: implemented
Issue: #171 (related to #151, #137, #101, #126, #104, #121)

## Problem

`.issuebot/env` is the workspace file a hook writes to hand variables to the session:
`workspace_environment` layers it over `agent_environment`'s allow-list for every turn and every
hook after the one that wrote it, which is how a `before_run` DSN reaches `pytest` at all.
`PROTECTED_ENV_NAMES` is the short list of names it may not set -- `FIXED_ENVIRONMENT`,
`GH_TOKEN`, `PATH`, `HOME` and the proxy names -- and `PROTECTED_ENV_PREFIXES` (`ANTHROPIC_`,
`CLAUDE_`) is the trust boundary that keeps the file from re-pointing or re-credentialling the
`claude` issuebot launches next.

A hook writes the file, but it lives in the *agent's own workspace*, so the session can write it
too. That is the premise the existing list is already drawn against ("the line is drawn at the
tooling issuebot launches rather than at 'a hook would not do that'"), and three names outside
the list re-point, for `git` and `ssh`, exactly the surfaces #101 and #137 sweep out of the
session account's home -- and that #151 proposes to sweep for `~/.gitconfig` and `~/.ssh/config`
in particular.

Measured on `main`@c7512a3, `git` 2.47.3, against the real `git` and `gh`:

```text
$ uv run python -c '<merge_workspace_env with the three names planted>'
refused: []
  in next session's env: GIT_CONFIG_GLOBAL='/workspaces/issuebot-7/.issuebot/planted-gitconfig'
  in next session's env: XDG_CONFIG_HOME='/workspaces/issuebot-7/.issuebot/xdg'
  in next session's env: GIT_SSH_COMMAND='/workspaces/issuebot-7/.issuebot/payload.sh'

$ GIT_CONFIG_GLOBAL=/tmp/gitprobe/planted-gitconfig git st
PLANTED-ALIAS-RAN
$ HOME=/tmp/gitprobe/nohome XDG_CONFIG_HOME=/tmp/gitprobe/xdg git st
PLANTED-ALIAS-RAN
$ GIT_SSH_COMMAND='printf PLANTED-SSH-RAN\n; false' git ls-remote git@example.invalid:x/y
fatal: protocol error: bad line length character: PLAN
```

The last is the proof it executed: git read the planted command's own stdout as protocol.

**Reach.** None of the three is inherited from the worker (`PASSTHROUGH_NAMES`,
`PASSTHROUGH_PREFIXES`), so `.issuebot/env` is the only route in. The file outlives the session,
but a workspace belongs to one issue, and under a pool it is bound to one account and sealed
`0700` between runs (#121, #75). So this is a channel from one session to the *next session on
the same issue* -- narrower than the home's, which is every later session at that uid for the
container's lifetime, and which is why #151 recorded it as a residual rather than folding it in.
Persistence inside the session's privilege domain, as in #101 and #137, not an escalation across
it (#75 closed that).

**Invariant.** A line in `.issuebot/env` cannot name a command, or a file naming a command, that
the next session's `git` or `gh` runs.

## Decision

**All of them are protected, and the set is closed under the other spellings of the same thing.**

The issue asked whether any of `GIT_CONFIG_GLOBAL`, `XDG_CONFIG_HOME` and `GIT_SSH_COMMAND`
belongs in `PROTECTED_ENV_NAMES` or in a prefix. Yes, and the prefix is the right shape for the
first of them.

- **`TOOL_CONFIG_ENV_NAMES`** (`agent/runner.py`, beside the existing entries):
  `GIT_SSH_COMMAND`, `GIT_SSH`, `GIT_ASKPASS`, `GIT_EXEC_PATH`, `XDG_CONFIG_HOME`.
- **`TOOL_CONFIG_ENV_PREFIXES`**: `GIT_CONFIG_`, joined to `PROTECTED_ENV_PREFIXES`.

Both are pinned by a test, as the sweep lists are, so dropping a name is a deliberate edit in two
places. Nothing else changes: `PROTECTED_ENV_NAMES` has exactly one reader,
`merge_workspace_env`, so this bounds `.issuebot/env` and no other environment.

### Why protect

**It is the rule the list already states, one step in.** `PATH` is protected so that `gh` and
`claude` keep running as issuebot launched them. `PATH` decides *which* binary `git` and `gh`
are; these decide what that binary does and which further commands it runs. The clearest case is
the one that is not a git variable at all: `XDG_CONFIG_HOME` moves the config directory of
everything following the base-directory specification, and `gh` -- the one tool in the session
holding `GH_TOKEN` -- reads `$XDG_CONFIG_HOME/gh/config.yml`, whose aliases may be shell
commands:

```text
$ printf 'aliases:\n  co: "!printf PLANTED-GH-ALIAS-RAN\\n"\n' > xdg/gh/config.yml
$ XDG_CONFIG_HOME=/tmp/gitprobe/xdg gh co
PLANTED-GH-ALIAS-RAN
```

`GH_PAGER=cat` is already fixed and protected, which closes one route through that same file and
leaves the rest: naming the directory is the general case of what fixing `GH_PAGER` was the
specific case of.

**It is the environment spelling of what the home sweep removes.** #101 and #137 clear the files
in the session account's home that a later session loads, and #151 proposes `~/.gitconfig` and
`~/.ssh/config` beside them. `GIT_CONFIG_GLOBAL` replaces *both* user-level config files with a
path of the line's choosing, so a sweep of `~/.gitconfig` would leave a guarantee conditional on
a variable nothing checked. The reaches differ -- the home is every later session at that uid,
`.issuebot/env` is the next session on this issue -- so it is not a full bypass of the sweep;
for that narrower set it is an exact one. (#151 is open at the time of writing and
`SHELL_STARTUP_SWEEP` names neither file yet. This decision does not wait on it and does not
assume it: the channel above is there whether or not the home is ever swept of those two files.)

**The cost is smaller than "taking a name away from hook authors" suggests** -- see below.

### Why all of them, and why a prefix

A partition of these names is not a line, because each can produce the others.

- A config file sets `core.sshCommand`, so protecting `GIT_SSH_COMMAND` while leaving
  `GIT_CONFIG_GLOBAL` closes the narrowest route and leaves the widest -- that same file also
  carries `[alias] x = !...`, `core.pager` and `credential.helper`.
- `GIT_SSH` is git's older spelling of the same thing and still runs the program it names
  (`fatal: protocol error: bad line length character: GIT_`, the planted program's own output
  read as protocol).
- `GIT_CONFIG_SYSTEM` and `GIT_CONFIG_NOSYSTEM` do to `/etc/gitconfig` what `GIT_CONFIG_GLOBAL`
  does to `~/.gitconfig` -- which matters here because a root-owned `/etc/gitconfig` is one of
  the routes this design offers a hook author instead.
- `GIT_CONFIG_COUNT` with `GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>` sets *any* key at all,
  `alias.st = !...` included, with no file involved: `GIT_CONFIG_COUNT=1
  GIT_CONFIG_KEY_0=alias.st GIT_CONFIG_VALUE_0='!printf PLANTED-COUNT-RAN\n' git st` ran the
  planted command. A list naming `GIT_CONFIG_GLOBAL` alone is a list defeated on the next line
  down.

So `GIT_CONFIG_` is a prefix rather than a name: it covers every current spelling, and whatever
git adds next, in one rule. It catches nothing a deployment legitimately sets, because the git
variables `agent_environment` inherits from the worker are `GIT_AUTHOR_` and `GIT_COMMITTER_`,
and a test pins that a hook can still hand those over.

`GIT_ASKPASS` and `GIT_EXEC_PATH` are the same family, measured the same way: the first is the
program git runs to obtain a credential (`GIT_ASKPASS-RAN`, and `password=hunter2` came back
from it), the second is the directory `git <subcommand>` is looked up in
(`GIT_EXEC_PATH-RAN` from a planted `git-probecmd`).

`XDG_CONFIG_DIRS` was measured and is *not* included: neither `git` nor `gh` reads it. The line
is drawn at what was shown to work, not at everything that sounds like it might.

### What a hook that needs one should do instead

The legitimate case the issue names is real: a deployment whose target repository needs a deploy
key has a reason to set `GIT_SSH_COMMAND` from `before_run`. It keeps every route it actually
needs, and loses one spelling -- the spelling that was also the channel.

- **`git config --local core.sshCommand 'ssh -i /path/to/key -o IdentitiesOnly=yes'` from
  `after_create`.** The workspace directory *is* the clone and `after_create` runs after it, so
  this is the post-clone setup's own idiom -- `POST_CLONE_SCRIPT` already writes
  `credential.https://github.com.helper` exactly this way -- and it is where a target
  repository's setup already lives. Per-clone, which is the right scope for a per-repository
  key, and it reaches every later turn because the clone does.
- **`git -c core.sshCommand=...`, or `GIT_SSH_COMMAND=... git ...` in the hook's own shell**, for
  git the hook itself runs: a submodule fetch, a second clone. Nothing about this changes. The
  bound is on the hook *handing the variable to the session*, not on the hook's own process
  environment, which was never `.issuebot/env`'s business.
- **A root-owned `/etc/gitconfig` or `/etc/ssh/ssh_config` in an image built `FROM` this one**,
  for a deployment-wide setting. Strictly better than an environment variable for that purpose,
  because it is outside the session's privilege domain entirely: a deployment-wide key set
  through `.issuebot/env` was always a setting the session could rewrite. `GIT_CONFIG_SYSTEM` and
  `GIT_CONFIG_NOSYSTEM` being protected is what keeps that route honest.

The README's `.issuebot/env` section says all of this where a hook author will be reading it,
beside `PATH` and the proxy names.

## Residuals

- **The clone's own `.git/config`.** A workspace outlives its run and the clone is the session's
  to write, so `git config --local core.pager '!...'` in the clone is the same shape for git
  *run in the clone*, and it is inherent to reusing a workspace rather than something an
  environment allow-list closes. What this change closes is the wider blast radius: git run
  anywhere else, `gh`'s entire config namespace, and any other tool following the
  base-directory specification that a target repository's hooks reach for. Named because
  "protected" must not be read as "the next session on this issue inherits nothing"; filed as a
  follow-up rather than folded in, since the answer is about workspace reuse and not about this
  file.

- **`BASH_ENV`.** Measured firing under `bash -lc`, which is `WorkspaceManager.hook_shell`:
  `BASH_ENV=/tmp/gitprobe/bashenv.sh bash -lc 'echo hook-ran'` printed `BASH_ENV-RAN` first. That
  is #137's `~/.profile` channel in environment-variable form, reaching every hook and the
  post-clone setup. It is a different tool and a different sibling issue from the git and `ssh`
  configuration this one decides, and the repository's practice is to file rather than fold in
  (#151 out of #137, #171 out of #151), so it is filed as a follow-up and named here. A reader
  should not take this change for a claim that the next session's login shell is unreachable.

- **A denylist, as in #101 and #137.** `.issuebot/env` admits everything it does not refuse, and
  it has to: its purpose is handing over what a target repository's tests need, which cannot be
  enumerated in advance. So this is a bound on the tooling *issuebot itself* launches -- `git`,
  `gh`, `claude` -- and never a general claim that the next session's environment is
  uninfluenced. `PAGER`, `NODE_OPTIONS`, `LD_PRELOAD` and their kind are outside it by the same
  reasoning that keeps the list short. Fails safe: a gap is a name that still gets through, never
  a broken session.

- **Concurrency does not arise here**, unlike #101. The file is in one workspace, a workspace is
  open to one account at a time and sealed `0700` between runs (#121), so there is no second
  live session reading this file.

## Tests

`tests/test_agent_runner.py`: `merge_workspace_env` refuses each of the eleven spellings and
still applies an ordinary key beside it; both lists are pinned; a hook can still hand over
`GIT_AUTHOR_NAME` and `GIT_COMMITTER_EMAIL`, which is what makes the `GIT_CONFIG_` prefix safe to
state as a prefix; and, end to end through `workspace_environment` with a real `.issuebot/env`,
a file carrying all three of the issue's names plus the `GIT_CONFIG_COUNT` triple hands the next
turn its `DATABASE_URL` and nothing else, while the complaint is one `workspace_env_ignored`
line per key, each naming the key it dropped and never its value. The suite was run with the
pre-#171 lists substituted back: every one of those tests fails, so the proof is two-sided.
