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
the list re-point, for `git` and `ssh`, exactly the surfaces #101, #137 and #151 sweep out of
the session account's home -- `TOOL_CONFIG_SWEEP` (`runas.py`) removes `~/.gitconfig`,
`~/.config/git/config` and `~/.ssh/config` before every turn and every login shell.

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

**Invariant.** A line in `.issuebot/env` cannot configure the tooling issuebot launches for
the next session: not `claude` (the `ANTHROPIC_`/`CLAUDE_` prefixes, which already did this),
and now not `git` or `gh` either -- it cannot name a command they run, a file or directory they
read configuration, aliases or hooks from, or the repository `git` operates on.

## Decision

**All of them are protected, and the set is closed under the other spellings of the same thing.**

The issue asked whether any of `GIT_CONFIG_GLOBAL`, `XDG_CONFIG_HOME` and `GIT_SSH_COMMAND`
belongs in `PROTECTED_ENV_NAMES` or in a prefix. Yes, and the prefix is the right shape for the
first of them.

- **`TOOL_CONFIG_ENV_PREFIXES`** (`agent/runner.py`, beside the existing entries): `GIT_` and
  `GH_`, joined to `PROTECTED_ENV_PREFIXES`.
- **`TOOL_CONFIG_ENV_NAMES`**: `XDG_CONFIG_HOME`, `SSH_ASKPASS` and `SSH_ASKPASS_REQUIRE`,
  joined to `PROTECTED_ENV_NAMES`.

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
in the session account's home that a later session loads, and #151 landed `TOOL_CONFIG_SWEEP`
beside them: `~/.gitconfig`, `~/.config/git/config` and `~/.ssh/config`. That sweep names both
spellings of the user-level git config deliberately, "because git reads both". But
`GIT_CONFIG_GLOBAL` replaces *both* of them with a path of the line's choosing, and
`XDG_CONFIG_HOME` moves the second one somewhere the sweep does not walk -- so the guarantee
#151 just established was conditional on a variable nothing checked. This change is what makes
it unconditional. The reaches still differ -- the home is every later session at that uid,
`.issuebot/env` is the next session on this issue -- so this was never a *full* bypass of the
sweep; for that narrower set it was an exact one.

**The cost is smaller than "taking a name away from hook authors" suggests** -- see below.

### Why whole prefixes, and not the three names

A partition of these names is not a line, because each produces the others. A config file sets
`core.sshCommand`, so protecting `GIT_SSH_COMMAND` while leaving `GIT_CONFIG_GLOBAL` closes the
narrowest route and leaves the widest -- that same file carries `[alias] x = !...`, `core.pager`
and `credential.helper` too.

Two drafts of this change were enumerations, and self-review found each of them incomplete. That
is the argument, and it is an empirical one rather than a stylistic preference.

The first draft was `GIT_CONFIG_` as a prefix plus `GIT_SSH_COMMAND`, `GIT_SSH`, `GIT_ASKPASS`
and `GIT_EXEC_PATH` as names. Measured against it:

```text
$ GIT_EDITOR=/tmp/g2/ed.sh git commit --allow-empty
PLANTED-EDITOR-RAN
error: there was a problem with the editor '/tmp/g2/ed.sh'
$ GIT_TEMPLATE_DIR=/tmp/g2/tpl git init -q r2 && cd r2 && git commit -q --allow-empty -m x
PLANTED-TEMPLATE-HOOK-RAN
```

`GIT_EDITOR` is the one that matters most: it runs on a plain `git commit`, which a session does
constantly, and it needs no terminal. `GIT_TEMPLATE_DIR` plants hooks into the next repository
`git init` creates. `GIT_SEQUENCE_EDITOR`, `GIT_PAGER` and `GIT_PROXY_COMMAND` are the same
shape, and `GIT_DIR`/`GIT_WORK_TREE` re-point which repository is operated on at all. So the
entry became `GIT_` whole.

The second draft still enumerated on the `gh` side -- it had `XDG_CONFIG_HOME` as a name,
justified in as many words by `$XDG_CONFIG_HOME/gh/config.yml` carrying shell aliases. But `gh`
reads `GH_CONFIG_DIR` *ahead* of it:

```text
$ printf 'aliases:\n  zz: "!printf PLANTED-GH-CONFIG-DIR-RAN\\n"\n' > cfg/config.yml
$ GH_CONFIG_DIR=/tmp/ghp/cfg gh zz
PLANTED-GH-CONFIG-DIR-RAN
```

Which is the very failure this section argues against, committed in the same change that argues
against it: the narrow spelling closed and the wide one left open. `GH_EDITOR` and `GH_BROWSER`
name commands too, while `GH_PAGER` was already fixed and protected as one of `FIXED_ENVIRONMENT`
-- an asymmetry with no reason behind it. So the second entry is `GH_` whole.

An enumeration is a list somebody has to keep complete against those tools' own manuals, it was
wrong twice here before it landed, and "closed under the other spellings of the same thing" is a
claim only a prefix can support. The prefixes also settle the bare `GIT_CONFIG`, which
`GIT_CONFIG_` would have missed, and whatever either tool adds next.

**`SSH_ASKPASS` is a name and not an `SSH_` prefix**, deliberately. git's documented chain is
`GIT_ASKPASS` -> `core.askPass` -> `SSH_ASKPASS` -> the terminal, and with no controlling
terminal -- the session's condition -- git executes `SSH_ASKPASS` itself, no ssh client
involved (`SSH_ASKPASS-RAN ... username=hunter2` out of `git credential fill`). So protecting
`GIT_ASKPASS` through the prefix and leaving this one would be the same mistake again. But
`SSH_AUTH_SOCK` is a legitimate route for exactly the deploy-key case this bound has to leave a
hook author, so the `SSH_` namespace is not one to take wholesale. Names here, prefixes there,
and the reason is in the tests as a negative case.

`XDG_CONFIG_DIRS` was measured and is *not* included: neither `git` nor `gh` reads it. Nor are
the specification's other roots, `XDG_DATA_HOME` and `XDG_CACHE_HOME` -- a hook pointing a cache
somewhere is exactly what this file is for. The line is drawn at what was shown to work.

### What the prefixes cost, and who pays it

**`GIT_AUTHOR_*` and `GIT_COMMITTER_*`** are caught, and `PASSTHROUGH_PREFIXES` inherits them,
so they can no longer be set from `.issuebot/env`. That is not a loss to the deployment, because
this file was never its channel for them: the README's own setup step has them in `.env`, from
where they reach the worker's environment and are inherited exactly as before, and a test pins
both halves. What is refused is the *session-writable file* re-pointing the identity that the
commits on a pull request carry -- worth refusing on its own account, not a cost reluctantly
accepted.

**Behaviour-only `GIT_*` switches** are the real cost, and they are worth naming because the
three routes below do not cover them: `GIT_TERMINAL_PROMPT=0` (the standard anti-hang measure
for a headless agent, with no `git config` counterpart), `GIT_TRACE*` and `GIT_CURL_VERBOSE` for
diagnosing a failing push, `GIT_LFS_SKIP_SMUDGE`. A hook that wants one has its own shell around
the git *it* runs, or a derived image for a deployment-wide setting; what it cannot do is hand
the switch to the session's own git. Accepted rather than carved out, because a carve-out is an
allow-list inside a prefix and the next thing somebody would have to keep complete.

**The refusal is visible only in the worker's log** (`workspace_env_ignored`, one line naming
the key), not to the hook that wrote the line and not in the run record. That is the existing
behaviour for every protected name and this change does not alter it, but it is worth saying
plainly here: a hook author who does not know the rule debugs a variable that silently did not
arrive, which is why the README states the rule where they will be reading rather than leaving
it to the log.

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

**`XDG_CONFIG_HOME` is the one that costs something real**, and it is worth naming rather than
folding into the deploy-key story. It is not a git variable, so none of the three routes above
covers it, and it is the only protected name with legitimate *non*-git uses: a target
repository whose setup wants user-level config for `uv`, `ruff`, `npm` or anything else
following the base-directory specification. With `HOME` protected too, a hook now has no way to
hand the session a relocated config root at all. What it keeps is per-command
(`XDG_CONFIG_HOME=... some-tool ...` inside the hook's own shell, where the hook runs the tool)
and per-repository (config written into the clone, which every later turn sees because the
clone persists). `XDG_DATA_HOME` and `XDG_CACHE_HOME` are untouched, which covers the cache and
state cases a hook more often wants. The trade is deliberate: the variable reaches `gh` -- the
one tool in the session holding `GH_TOKEN` -- and a route to `gh`'s aliases is not one to leave
open for the convenience of pointing `ruff`'s config somewhere. If a deployment turns out to
need the general case, the answer is a narrower variable for the tool that needs it, not this
one back.

The README's `.issuebot/env` section says all of this where a hook author will be reading it,
beside `PATH` and the proxy names.

## Residuals

- **The clone's own `.git/config`.** A workspace outlives its run and the clone is the session's
  to write, so `git config --local core.pager '!...'` in the clone is the same shape for git
  *run in the clone*, and it is inherent to reusing a workspace rather than something an
  environment allow-list closes. What this change closes is the wider blast radius: git run
  anywhere else, `gh`'s entire config namespace, and any other tool following the
  base-directory specification that a target repository's hooks reach for. Named because
  "protected" must not be read as "the next session on this issue inherits nothing"; filed as
  #180 rather than folded in, since the answer is about workspace reuse and not about this
  file.

- **`BASH_ENV`.** Measured firing under `bash -lc`, which is `WorkspaceManager.hook_shell`:
  `BASH_ENV=/tmp/gitprobe/bashenv.sh bash -lc 'echo hook-ran'` printed `BASH_ENV-RAN` first. That
  is #137's `~/.profile` channel in environment-variable form, reaching every hook and the
  post-clone setup. It is a different tool and a different sibling issue from the git and `ssh`
  configuration this one decides, and the repository's practice is to file rather than fold in
  (#151 out of #137, #171 out of #151), so it is filed as #179 and named here. A reader
  should not take this change for a claim that the next session's login shell is unreachable.

- **A denylist, as in #101 and #137.** `.issuebot/env` admits everything it does not refuse, and
  it has to: its purpose is handing over what a target repository's tests need, which cannot be
  enumerated in advance. So this is a bound on the tooling *issuebot itself* launches -- `git`,
  `gh`, `claude` -- and never a general claim that the next session's environment is
  uninfluenced. `PAGER`, `NODE_OPTIONS`, `LD_PRELOAD` and their kind are outside it by the same
  reasoning that keeps the list short -- and `BASH_ENV` above is the measured proof that such a
  gap is real rather than theoretical. Fails safe: a gap is a name that still gets through,
  never a broken session. The `GIT_` prefix is the one part of this that is *not* a denylist,
  which is exactly why it is a prefix.

- **Concurrency does not arise here**, unlike #101. The file is in one workspace, a workspace is
  open to one account at a time and sealed `0700` between runs (#121), so there is no second
  live session reading this file.

## Tests

`tests/test_agent_runner.py`: `merge_workspace_env` refuses each of twenty-eight spellings,
grouped by what each one does -- a config file at a chosen path, a key with no file at all, a
command named outright, a directory of commands, the repository itself, the commit identity,
`gh`'s own config directory and aliases, the askpass fallback -- and still applies an ordinary
key beside each; six *negative* cases pin that the prefixes stop where they say they do
(`GITHUB_WORKSPACE` and `GHOSTSCRIPT_HOME` have no underscore where the prefix does,
`XDG_DATA_HOME`/`XDG_CACHE_HOME`/`XDG_CONFIG_DIRS` are not read by either tool, and
`SSH_AUTH_SOCK` is the deploy-key route the `SSH_` namespace had to keep); both entries are
pinned, with the docstring recording that two drafts missed `GIT_EDITOR` and `GH_CONFIG_DIR`,
so a future edit back to a list has to argue with that; the deployment's own `GIT_AUTHOR_NAME`
still reaches the session through
`agent_environment` while a `.issuebot/env` line cannot take it out from under the next turn,
which is the two halves of the prefix's cost; and, end to end through `workspace_environment`
with a real `.issuebot/env`, a file carrying all three of the issue's names plus the
`GIT_CONFIG_COUNT` triple and `GIT_EDITOR` hands the next turn its `DATABASE_URL` and nothing
else, while the complaint is one `workspace_env_ignored` line per key, each naming the key it
dropped and never its value. The suite was run with the pre-#171 lists substituted back: every
one of those tests fails, so the proof is two-sided.

Beyond the suite, the channel was walked end to end against the real `git`, before and after:
the same `.issuebot/env` that made the next turn's `git st` print `PLANTED-COUNT-RAN` now leaves
it `git: 'st' is not a git command`, with `DATABASE_URL` still handed over and six
`workspace_env_ignored` lines naming the keys that were dropped.
