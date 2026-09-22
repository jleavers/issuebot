# A `.issuebot/env` line does not re-point the next session's `gh` extension lookup

Date: 2026-09-22
Status: implemented
Issue: #191 (related to #186, #171, #151, #137, #101, #121, #104)

## Problem

`.issuebot/env` is the workspace file a hook writes to hand variables to the session:
`workspace_environment` layers it over `agent_environment`'s allow-list for every turn and every
hook after the one that wrote it. `PROTECTED_ENV_NAMES` and `PROTECTED_ENV_PREFIXES` are what it
may not set: the `claude` entries from the start, the `git` and `gh` ones since #171
(`TOOL_CONFIG_ENV_NAMES`, `TOOL_CONFIG_ENV_PREFIXES`), the shell's own since #179
(`SHELL_ENV_NAMES`).

#186 found a second surface in the session account's home, of a different kind from the config
files #151 sweeps: `~/.local/share/gh/extensions`, the directory `gh` dispatches `gh <name>`
from, where a session can leave an executable for a later session's `gh` to run. Its answer is
a sweep of that directory (PR #195, open when this landed). The environment spelling of the same channel is `XDG_DATA_HOME`, which was measured
in that issue and filed here rather than folded in, for the reason #171 was filed out of #151:
protecting a name is a decision about what a hook may legitimately configure, and the reach
differs.

`XDG_DATA_HOME` is not in `TOOL_CONFIG_ENV_NAMES`, which carries `XDG_CONFIG_HOME` for the git
and `gh` *config* spellings and was drawn up when this directory was not yet anyone's question.
Worse, #171 named it in as many words as one of the base directories deliberately left out --
"neither `git` nor `gh` reads them, and a hook pointing a cache somewhere is exactly what this
file is for" -- and pinned that with a negative test. The first half of that sentence is false
for `gh` 2.100.0, measured below. This change corrects it.

**Reach.** `XDG_DATA_HOME` is not in `PASSTHROUGH_NAMES`, so it is not inherited from the
worker; the only way it reaches a session is the workspace's own `.issuebot/env`. That file
outlives the session, but a workspace belongs to one issue and under a pool is bound to one
account and sealed `0700` between runs (#121, #75), so this is a channel from one session to
the *next session on the same issue* -- and, since the file is re-read for every turn, from a
session to its own later turns. Narrower than the home's, which is every later session at that
uid for the container's lifetime. Persistence inside the session's privilege domain, as in
#101, #137, #151 and #186, not an escalation across it (#75 closed that).

**Invariant.** A line in `.issuebot/env` cannot name the directory a later `gh` dispatches a
subcommand from. Together with #186, the session's `gh` looks for extensions in exactly one
place, and that place is swept before every turn and every login shell.

## Measured

`gh version 2.100.0 (2026-09-03)` and `git 2.47.3` on this image. `$R` is a scratch directory;
`$R/home/.local/share/gh/extensions/gh-pwn/gh-pwn` and `$R/xdg/gh/extensions/gh-xdgpwn/gh-xdgpwn`
are two planted executables, written by hand -- no `gh extension install`, no manifest, no
network (#186 measured that the install step is not part of the channel).

**The variable moves the lookup wholesale, and hides the home's own extensions with it:**

```text
$ HOME=$R/home gh pwn
PLANTED-HOME-RAN
$ HOME=$R/home XDG_DATA_HOME=$R/xdg gh xdgpwn
PLANTED-XDG-DATA-HOME-RAN
$ HOME=$R/home XDG_DATA_HOME=$R/xdg gh pwn
unknown command "pwn" for "gh"
```

**And the route into the next session is open**, through the function that filters the file:

```text
$ uv run python -c '<merge_workspace_env with both XDG names planted>'
complaints: ['XDG_CONFIG_HOME']
XDG_DATA_HOME in next session's env: /workspaces/issuebot-191/.issuebot/xdg
XDG_CONFIG_HOME: None
protected? False
```

**`XDG_DATA_HOME` is the only spelling of that directory.** `gh` dispatches from the data
directory alone -- not from `PATH`, the way `git` finds `git-<name>`, and not from
`GH_CONFIG_DIR`, which is the `GH_` name that moves the *config* directory (#186 measured the
`PATH` half; the `GH_CONFIG_DIR` half was re-measured here with the extension planted both at
`$GH_CONFIG_DIR/gh/extensions` and at `$GH_CONFIG_DIR/extensions`):

```text
$ HOME=$R/home GH_CONFIG_DIR=$R/ghcfg gh cfgpwn
unknown command "cfgpwn" for "gh"
```

So the `GH_` prefix #171 landed does not already cover this, which is why the channel survived
that change.

**Which base directories the two tools actually read.** Each of the specification's roots was
set in turn, with a `gh` extension, a `gh` `config.yml` alias and a `git` `[alias]` planted
under it:

| variable | `gh <ext>` | `gh <alias>` | `git <alias>` |
|---|---|---|---|
| `XDG_CONFIG_HOME` | no | **ran** | **ran** |
| `XDG_DATA_HOME` | **ran** | no | no |
| `XDG_STATE_HOME` | no | no | no |
| `XDG_CACHE_HOME` | no | no | no |
| `XDG_RUNTIME_DIR` | no | no | no |
| `XDG_CONFIG_DIRS` | no | no | no |
| `XDG_DATA_DIRS` | no | no | no |

Two of the seven are read, and they are disjoint in what they reach: `XDG_CONFIG_HOME` for
configuration both tools read, `XDG_DATA_HOME` for the one program `gh` executes.

**What the protection costs, measured rather than guessed.** `XDG_DATA_HOME` does move the data
directory of other tools following the specification, `uv` 0.12.11 among them, and the per-tool
variables that remain are not protected:

```text
$ uv tool dir
/home/agent-3/.local/share/uv/tools
$ XDG_DATA_HOME=$R/xdg uv tool dir
$R/xdg/uv/tools
$ UV_TOOL_DIR=/tmp/td uv tool dir
/tmp/td
$ uv python dir
/home/agent-3/.local/share/uv/python
$ XDG_DATA_HOME=$R/xdg uv python dir
$R/xdg/uv/python
$ UV_PYTHON_INSTALL_DIR=/tmp/pyd uv python dir
/tmp/pyd
```

## Decision

**Protected: `XDG_DATA_HOME` joins `TOOL_CONFIG_ENV_NAMES`.**

### Why protect

**It is the strongest form of what that list already refuses.** Every other name on it re-points
a file or a directory whose *contents* can name a command -- a git config with `[alias] x = !...`,
a `gh` `config.yml` with a shell alias, an editor or a pager the tool then runs. This one
re-points the directory from which `gh` executes a program directly. The list's rule reads one
step further than it was written: not only what `git` and `gh` read as configuration, but what
they run as a subcommand.

**And the tool it re-points is the one holding `GH_TOKEN`.** That was #171's argument for
`XDG_CONFIG_HOME` and it applies here unchanged, with the `gh ` prefix on the command doing the
work: a plant fires when a later session, a hook or the post-clone setup runs `gh <name>`.

**It makes the sweep #186 proposes unconditional, the same relationship #171 has to #151.** A
sweep of `~/.local/share/gh/extensions` is a guarantee about the directory `gh` looks in only
while nothing can move the lookup -- and the third measurement above shows the move also *hides*
the home, so it defeats the sweep in both directions at once: the planted directory is read and
the swept one is not. #151 swept `~/.gitconfig` and `~/.config/git/config` and #171 closed
`GIT_CONFIG_GLOBAL` and `XDG_CONFIG_HOME` for exactly this reason. Leaving this one open would
have left #186's guarantee conditional on a variable nothing checked.

**The two changes stand alone in both directions.** This one closes the environment spelling
whether or not #186 lands (it was still open at PR #195 when this was written), and #186 closes
the home spelling whether or not this one does. They are only worth the same amount together.

### The hatch, weighed

`XDG_DATA_HOME` is also the one escape hatch a deployment had for a `gh` extension it wants
every session to have: `gh` has no system-wide extension location (measured above and in #186),
so pointing the data root at a directory the deployment controls was the only way to install one
for the session. Protecting the name closes the hatch as well as the channel. Three things
settle the trade.

**The hatch was never a control.** `.issuebot/env` is a session-writable file in the session's
own workspace, re-read for every turn: whatever a `before_run` hook writes there, the session can
overwrite for its own next turn and for the next session on that issue. So a deployment that
pointed `XDG_DATA_HOME` at a root-owned directory of extensions was already handing the session
a setting the session could take back -- and then it is the *deployment's* own `gh <name>`
invocations that run whatever the session pointed the variable at instead. That is #171's
finding for the deploy key ("a deployment-wide key set through `.issuebot/env` was always a
setting the session could rewrite"), and it is why the hatch's value is convenience and not
confinement.

**#186 measured the other half of the same point.** Where the data directory is the account's
home, a session can overwrite the executable of an extension the deployment installed, so the
plant fires on the ordinary command that deployment already runs. A deployment with extensions
is the deployment with the most to lose from either spelling being left open.

**What such a deployment should do instead** is what #186 already answers, unchanged by this:
install the same program root-owned on the session's `PATH` -- `/usr/local/bin/<name>` in an
image built `FROM` this one -- and invoke it under its own name. A `gh` extension is an ordinary
executable that `gh` hands its argv and its own environment to; it is given no credential, no
library and no protocol of `gh`'s -- with an empty caller environment the child saw exactly one
addition, `GH_EXTENSION=1`, and no token (#186 measured the same with a `hosts.yml` in place).
What the deployment loses is the `gh ` prefix on the command, and nothing else. That is #137's
and #151's answer in the only spelling `gh` leaves -- the system file rather than the account's,
outside the session's privilege domain -- because `gh` has no `/etc` of its own.

A hook that wants an extension for *its own* `gh` keeps the route that was never in question:
`XDG_DATA_HOME=... gh <name> ...` in the hook's own shell. The bound is on handing the variable
to the session.

**Rejected: passing `XDG_DATA_HOME` through from the worker.** Adding it to `PASSTHROUGH_NAMES`
would give the hatch back in a form the session cannot rewrite -- the operator sets it on the
compose service, the session inherits it, `.issuebot/env` is refused it. It is rejected because
it would buy the hatch by re-introducing precisely the conditionality this change removes: the
session's `gh` would look for extensions wherever an operator's variable said, and a sweep of
`~/.local/share/gh/extensions` would be clearing a directory nothing reads. A guarantee that
holds unless a deployment sets a variable is the shape of fault this pair of changes exists to
end. The `PATH` route above costs the deployment a command prefix and costs the guarantee
nothing.

### Re-reading the list-versus-prefix question of #171

The issue asks for this rather than for a name appended, since `XDG_DATA_HOME` is a
base-directory variable like `XDG_CONFIG_HOME` and moves the data directory of everything
following the specification, not `gh` alone. The answer is **names, and not an `XDG_` prefix**,
and the reason is that #171's argument for prefixes does not transfer.

`GIT_` and `GH_` are prefixes because they are *those tools' own namespaces*: git and `gh` add
names to them at will, an enumeration is one somebody has to keep complete against their manuals,
and two drafts of #171 failed to. `XDG_` is a specification's namespace and not a tool's.
Neither tool has ever added a name to it, the specification's roots are a short fixed list
-- seven in the current version, the table above being all of them -- and what changes over
time is not which names exist but which of them a tool reads -- which is a measurement, and is what the table and the tests are.

The rule, then, stated so it is checkable rather than ad hoc: **an XDG base directory is
protected when a tool issuebot launches resolves through it something it will execute or read as
configuration.** Measured against the two tools this list is drawn for, `git` and `gh`, two roots
do (`XDG_CONFIG_HOME`, `XDG_DATA_HOME`) and five do not, and stay out -- the table above is that
measurement, and the scope of the negative half is stated rather than implied, because a rule
quantified over every tool would be a claim nobody measured. That is the same shape as the
chain-tail rule #171 settled the names on -- a finite thing checkable against the tools' own
behaviour -- rather than a list of everything that might matter.

An `XDG_` prefix would take the rest with it, and the rest is what this file is *for*:
`XDG_CACHE_HOME` and `XDG_STATE_HOME` are a hook pointing a cache or a state directory
somewhere, which is the legitimate use #171 named. The line is drawn at what was shown to work,
in both directions.

### What it costs, and who pays

A hook can no longer hand the session a relocated *data* root, for `gh` or for anything else --
`uv`'s tool and python directories are the measured case, and with `HOME` and `XDG_CONFIG_HOME`
already protected a hook now has no way to move the session's user-level config or data roots at
all. What it keeps:

- **Per-tool variables**, which are not protected and are the better spelling anyway:
  `UV_TOOL_DIR`, `UV_TOOL_BIN_DIR`, `UV_PYTHON_INSTALL_DIR`, `UV_CACHE_DIR` (measured above), and
  their equivalents in other tools. A variable for the tool that needs it is exactly what #171
  said the answer would be if a deployment turned out to need the general case.
- **Per-command**, in the hook's own shell, for a tool the hook itself runs.
- **Per-repository**, written into the clone, which every later turn sees because the clone
  persists.
- **`XDG_CACHE_HOME` and `XDG_STATE_HOME`**, untouched.

As with every protected name, the refusal is visible only in the worker's log
(`workspace_env_ignored`, one line naming the key) and not to the hook that wrote it, which is
why `docs/toolchains.md` states the rule where a hook author is reading.

## Design

- **`TOOL_CONFIG_ENV_NAMES`** (`agent/runner.py`) gains `XDG_DATA_HOME`, beside
  `XDG_CONFIG_HOME`, with the comment carrying the rule above: the two XDG roots are there
  because a tool issuebot launches reads configuration or dispatches a program through them, and
  the other five were measured not to be.
- **Nothing else changes.** `PROTECTED_ENV_NAMES` has exactly one reader,
  `merge_workspace_env`, so this bounds `.issuebot/env` and no other environment.
  `PASSTHROUGH_NAMES` is untouched, which is the half that keeps the session's `gh` looking in
  the home #186 is about.

## Residuals

- **A program the session runs by path.** An extension is an ordinary executable, so a session
  that wants one for itself can keep it in the workspace and run it directly; the workspace
  outlives the run, so the next session on that issue finds it. That is the clone's own
  residual (#180) and not this one: what is closed here is `gh <name>` dispatching it, which is
  what makes a plant fire on somebody else's ordinary command rather than on a path only the
  planter knows.
- **A later `gh` that reads another base directory.** The table above is a measurement of
  `gh` 2.100.0, not a promise about `gh` 3. The test that pins the list carries the rule and the
  measurement together, so a version that started dispatching from `XDG_STATE_HOME` would need
  the same one-name edit -- and would be caught by re-running the measurement, not by the suite.
- **`XDG_DATA_DIRS`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `XDG_RUNTIME_DIR`, `XDG_CONFIG_DIRS`**
  remain settable, by decision rather than oversight, and the negative tests pin that they stop
  where they say they do.

- **`claude`'s own use of these names.** The measurement above is `git` and `gh`, and `claude`
  is a tool issuebot launches too: its binaries on this image reference `XDG_DATA_HOME`,
  `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_RUNTIME_DIR` and both `_DIRS` lists. Nothing was
  shown to work through them -- `claude --version` is unaffected with all four of those roots
  pointed at an empty directory, because this image's launcher resolves its version binary by
  absolute path under `/opt/claude` rather than through the data root -- so this is a gap in
  the measurement and not a known channel. It is named because the tool's own namespace is
  covered (`CLAUDE_` and `ANTHROPIC_` are `PROTECTED_ENV_PREFIXES`) while the XDG roots are not
  claude-namespaced, so a route through one of the remaining five would be the same one-name
  edit this change is.
- **A denylist, as in #171 and #179.** `.issuebot/env` admits everything it does not refuse and
  has to: its purpose is handing over what a target repository's tests need. This is a bound on
  the tooling issuebot itself launches, never a claim that the next session's environment is
  uninfluenced.
- **#186 is what closes the home spelling**, and it was open (PR #195) when this landed. Until
  it does, a session can still leave an extension in `~/.local/share/gh/extensions` for the next
  session at that uid. Named so that "protected" is not read as "the extension channel is
  closed": this change closes the wider-reaching half of it, the one that also hides the home.

## Tests

`tests/test_agent_runner.py`:

- `XDG_DATA_HOME` joins the parametrised refusal cases, annotated with what it does -- the
  directory `gh` dispatches `gh <name>` from -- and the pinned `TOOL_CONFIG_ENV_NAMES` list
  gains it, so dropping it is a deliberate edit in two places.
- It comes *out* of the negative over-reach cases, where #171 pinned it as deliberately
  unprotected; `XDG_STATE_HOME` and `XDG_DATA_DIRS` join `XDG_CACHE_HOME` and `XDG_CONFIG_DIRS`
  there, each one measured unread by both tools, so the negative list is the other half of the
  table above rather than a shorter claim.
- End to end through `workspace_environment` with a real `.issuebot/env`: a file carrying
  `XDG_DATA_HOME` hands the next turn its `DATABASE_URL` and its `XDG_CACHE_HOME` and not the
  data root, with one `workspace_env_ignored` line naming the key and never its value.
- And end to end against the real `gh`, shaped like #151's `git` proof in
  `tests/test_agent_runas.py` (skipped where `gh` is not installed): a planted extension under
  the directory the file names is dispatched by `gh <name>` when the file's own mapping is used,
  and is `unknown command` when the environment is the one `workspace_environment` built. The
  proof is two-sided, so it fails if the protection is removed rather than only asserting a key
  is absent.
