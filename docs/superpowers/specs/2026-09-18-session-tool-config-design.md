# A session account's git and ssh config does not carry between sessions

Date: 2026-09-18
Status: implemented
Issue: #151 (related to #137, #101, #75)

## Problem

#101 sweeps the session account's `~/.claude` before every turn, #119 keeps `~/.claude.json`'s
`mcpServers` out of a session, and #137 aimed the sweep at the home itself and took the shell
start-up files a login shell runs. One class of the same shape was still open, one tool further
out: the config files a *tool* the session runs reads out of that home, each of which can name a
command to execute.

- **`~/.gitconfig`.** Every session runs `git` as the account -- the clone, the branch, the
  commits, the push -- and user-level git config names commands: `core.pager`, `core.editor`,
  `core.fsmonitor`, `credential.helper`, `diff.<driver>.textconv`, `[alias] x = !sh -c '...'`.
  A session that writes one leaves a command every later session's `git` runs at that uid.
- **`~/.config/git/config`.** The same file under its other name. `git` reads
  `$XDG_CONFIG_HOME/git/config` -- which is `~/.config/git/config` here, since `XDG_CONFIG_HOME`
  is not in `PASSTHROUGH_NAMES` and so never reaches a session -- *before* `~/.gitconfig`. The
  issue names only the first spelling; a sweep that took it alone would leave the channel open
  at the name git looks at first, which is why this is one decision about git and not about a
  filename.
- **`~/.ssh/config`.** `ProxyCommand`, `LocalCommand` and `Match exec` run a shell command for a
  matching host. issuebot's own clone is HTTPS through `gh` and the default image installs no
  ssh client (`--no-install-recommends`), so nothing issuebot does reads it today; a target
  repository's hook or a submodule URL, in an image built `FROM` this one that does carry ssh,
  can.

Persistence *within* the session's privilege domain, exactly as in #101 and #137 and not an
escalation across it (#75 closed that): gone on a container recreate, and under a pool (#121) the
sharing is with the *next* session bound to that account rather than with the ones beside it.

Measured on the clone as found, `main`@9a97c99, with a throwaway home: a planted `[alias]` in
either git file runs for a `git` that reads that home, and `_sweep` leaves all three files where
they are.

```text
$ HOME=/tmp/repro151/home git x            # ~/.gitconfig
PLANTED-GITCONFIG-ALIAS-RAN
$ HOME=/tmp/repro151/home git y            # ~/.config/git/config
PLANTED-XDG-ALIAS-RAN
$ python -c "from issuebot.agent.runas import _sweep; _sweep(Path('/tmp/repro151/home'))"
.gitconfig True
.config/git/config True
.ssh/config True
```

**Invariant.** A session cannot leave a config file in the session account's home that a later
session's `git` or `ssh` takes a command from.

## The decision the issue asks for: swept

The issue asks whether either file is issuebot's to sweep, and for `~/.gitconfig` in particular
whether a deployment has a legitimate reason to put one in a session account's home. It does not,
and that is what settles it: everything the deployment needs from git config it already gets
somewhere else, at a level the session cannot write.

- **Identity** comes from the environment: `GIT_AUTHOR_*` and `GIT_COMMITTER_*` are in
  `PASSTHROUGH_PREFIXES` (`agent/runner.py`), which is how the README tells an operator to set the
  bot's name and email in the first place.
- **`safe.directory`** for `/workspaces/*` is the image's, written `git config --system` in the
  Dockerfile -- root's file, because the workspace is the worker's and the clone the session's and
  the entry has to survive whatever the session does.
- **The credential helper** is the post-clone setup's, written `git config --local` inside the
  clone (`POST_CLONE_SCRIPT`), so it lives and dies with the workspace.
- **A deployment that does want global git config for its sessions** has `/etc/gitconfig`, which is
  root's and outside the session's privilege domain, set in the image or in one built `FROM` it.
  The same answer as #137's for `PATH`: the system file, not the account's.

So nothing legitimate is lost, and the same three arguments #137 made against the alternatives
apply unchanged -- a root-owned home would break the next tool that wants to write in it, and
leaving the files in place root-owned does not work because the account can unlink them.

`~/.ssh/config` is swept on the same terms. Nothing issuebot runs reads it today, and a name on
the list costs nothing where the file does not exist; the alternative is a documented residual
that comes back the first time a deployment adds an ssh client or a target repository's hook uses
one, which is a worse trade than three entries in a list.

## Design

- **`TOOL_CONFIG_SWEEP`** (`agent/runas.py`), beside `SHELL_STARTUP_SWEEP` and pinned by a test
  the same way: `.gitconfig`, `.config/git/config`, `.ssh/config`. A list of its own rather than
  entries in the shell one, because the two are checked against different documents -- that one
  against `bash(1)`, this one against `git-config(1)` and `ssh_config(5)`.

- **Each entry is its path components, not a name**, because every one of them is nested and the
  sweep has to *walk* rather than follow. `_walk(root, parts)` resolves a component at a time and
  returns the first symlink it meets instead of descending through it: with `.ssh` replaced by a
  link, `home / ".ssh" / "config"` names a file outside the home altogether, while the link itself
  is what a session planted and what `ssh` would read through. So the link is what is unlinked --
  the rule `_sweep_targets` already applies to `projects/<project>` -- and a missing component
  yields nothing, which is the ordinary case of a home that never held the file.

- **A denylist, still.** The sweep names files and never a directory another tool keeps state in:
  `~/.config` is where `gh` keeps its configuration and `~/.ssh` is where a deployment could have
  put keys, and both survive with the file beside them gone.

- **Nothing else changes.** No new setting, no image change, no new call site: `_sweep` already
  runs before every turn and before every script that opens a login shell (#137), and both halves
  of that schedule are what a `git` invoked by a hook, by the post-clone setup or by the session
  itself sits behind.

## A mode is not a defence, and neither is a link

Two things found by the self-review, both closed here, and neither of them new to this issue --
they are what the older lists were resting on without saying so.

- **A locked directory.** The sweep runs as the account whose home it is clearing, so a directory
  it cannot open is one the account chose not to open, and the plant needs none of the bits it
  drops: `git`, `ssh` and `claude` only read a path they already know. Three bits, each hiding a
  different step. Without **write**, `~/.ssh` at `0500` failed the unlink with `EACCES` and
  `_sweep`'s `suppress(OSError)` skipped it. Without **search**, nothing inside could be stat'ed:
  `~/.ssh` at `0600` left `_exists` reading the plant as absent, so `_sweep` concluded there was
  nothing to retry, and a closed `~/.config` -- the one directory a swept path passes *through* --
  left `_walk` concluding there was no target to yield at all. Without **read**,
  `~/.claude/projects` at `0100` could not be listed, and listing it is how auto memory is
  reached. `$HOME` itself did the first two to all three lists at once, and `sweep_home` reported
  success every time. Now `_sweep` retries a surviving target with the modes put back
  (`_relax`/`_relax_tree`, the repair `_remove` already made for a workspace tree), `_walk`
  relaxes an intermediate it cannot stat before concluding it is absent, `projects` is relaxed
  before it is read rather than after the read failed, and `_exists` tells "not there"
  (`FileNotFoundError`) from "cannot tell" (anything else), which is the reading the retry turns
  on. One extra attempt, never a loop.

- **A symlinked target's tree.** `os.walk` refuses a link below its top but follows the top
  itself, so the retry's `_relax_tree` would have widened modes right across whatever tree a
  planted `~/.ssh` or `~/.config/git` link points at -- any size, any place, the session's choice,
  before every turn and every hook, against a worker whose only bound on the sweep is the ten
  seconds it waits for sudo. Unlinking a link needs the parent's bits and nothing of its
  target's, so the target is not visited at all.

- **A symlinked `~/.claude`.** `_sweep_targets` walked `projects/<project>` without following a
  link at either level but reached `.claude` itself by joining, so a session could point `.claude`
  at any tree it can write and have the *next* session's sweep delete `CLAUDE.md`, `skills/` and
  the rest of the named entries inside it -- a plant that costs someone else's files rather than
  keeping the session's own. The same rule now applies one level up: the link is the target.


- **The denylist**, as in #101 and #137: a tool config file nobody has named is not on the list.
  `~/.gitmodules` and `~/.config/git/attributes` are deliberately absent -- an attributes file
  names a *driver*, and the driver's command comes from the config this does sweep, so without it
  there is no command to run.

- **`~/.config/gh/config.yml`**, which this change pins as a survivor for its neighbour
  `hosts.yml`'s sake, is the nearest thing to a fourth entry and is left off deliberately, with
  the reasoning recorded because the file is *named* in the tests. Measured against the `gh` on
  this host: an `aliases:` entry written into it directly does run a shell command
  (`gh pwn` -> `GH-ALIAS-RAN`), so the shape is real -- but it cannot shadow a core command
  (`gh issue` still ran the built-in with an `issue: '!echo ...'` alias in place), and issuebot
  and a session alike invoke core commands (`gh repo clone`, `gh issue edit`, `gh api`). So a
  plant fires only if some later session happens to invoke the invented subcommand name it chose,
  where git's `core.pager` or `credential.helper` fire on an ordinary command. Its other
  command-bearing keys are already answered by the environment, which the workspace env file
  cannot override: `GH_PAGER=cat` and `GH_PROMPT_DISABLED=1` are in `FIXED_ENVIRONMENT` and
  protected. Filed rather than folded in, since `config.yml` is also the file `gh config set`
  writes and taking it would be a decision about what a hook may configure. Filed as #173.

## Residuals

- **The environment variables that re-point these files, through `.issuebot/env`.** Three of
  them, and none is in `PROTECTED_ENV_NAMES`: `XDG_CONFIG_HOME` moves the second git spelling
  under a directory the list does not name, `GIT_CONFIG_GLOBAL` replaces *both* git spellings
  with a path of its own, and `GIT_SSH_COMMAND` names a command outright with no file at all.
  None is inherited from the worker (`PASSTHROUGH_NAMES`), so the only way one reaches a session
  is the workspace's own `.issuebot/env` -- which a hook writes and which, as the README says,
  outlives the session: a retry or a rework session on the *same workspace* reads what the last
  one left. So the reach is that workspace and the issue it belongs to, rather than the account's
  every later session, which is what the home is; and the account's *other* workspaces are
  another account's or sealed `0700` (#121, #75). Closing it is not something this list can do,
  since the path is whatever the variable says. Named here, and filed as #171 rather than
  folded in: protecting those names is a decision about what a hook may configure, which is not
  this issue's to make.

- **Concurrency**, exactly as in #101 and #137: with one account for the deployment a session
  running beside this one can plant between a sweep and the command it protects. A pool closes it,
  since no two concurrent sessions share a home.

- **`/etc/gitconfig` and `/etc/ssh/ssh_config`** are root's and outside the session's reach, which
  is what makes them the supported place for a deployment to put global git or ssh config. Not a
  residual so much as the other side of the decision above.

## Tests

`tests/test_agent_runas.py`: a plant locked behind a directory mode the session set is still
removed -- a file, a tree, a directory locked inside a swept tree, and auto memory behind a
`projects` that will not list -- over every way of locking one (`0500`, `0600`, `0400`, `0100`,
`0000`), with the home and `.config` among the locked directories, since those are the two a
target passes through rather than sits in; the tree a symlinked target points at is neither
removed nor walked; the neighbours, the credential and the transcripts survive all of it; a symlinked `.claude` is unlinked and the tree it
pointed at is untouched; the sweep removes all three files and leaves their neighbours
(`~/.config/gh/hosts.yml`, `~/.ssh/known_hosts`) and the directories themselves; the list is
pinned, including the fact that `XDG_CONFIG_HOME` is not passed through, which is what makes the
second git spelling the path git actually reads; a symlinked `.ssh` is unlinked rather than
stepped through, at the first component and at the second; a home that never held any of it is
untouched, which `_walk` is asked directly rather than inferred from a suppressed unlink. And, end to end beside #137's
profile proof, a planted `~/.gitconfig` alias does not run for the next session's `git` -- the
real wrapper, the real hook path, the real `bash -lc` and the real `git`, only sudo a fake --
two-sided, so with the sweep removed the alias *is* what `git` runs.
`tests/test_image_layout.py` pins the CI step, and the CI `docker` job proves it in the real
image, the real uid split and the real home, through the worker's own `RunAs("agent").sweep_home()`:
both git spellings run an alias before the sweep and neither has one after, `~/.ssh/config` and
both git files are gone, and `gh`'s config and `known_hosts` are still there.
