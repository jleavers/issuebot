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

## Residuals

- **The denylist**, as in #101 and #137: a tool config file nobody has named is not on the list.
  `~/.gitmodules` and `~/.config/git/attributes` are deliberately absent -- an attributes file
  names a *driver*, and the driver's command comes from the config this does sweep, so without it
  there is no command to run.

- **`XDG_CONFIG_HOME` pointed somewhere else.** The variable does not reach a session
  (`PASSTHROUGH_NAMES`), but a session can put one in its workspace `.issuebot/env`, which is not
  a protected name, and plant a config under whatever directory it names. That plant only reaches
  a later session whose own env file names the same directory, so it is a channel a session opens
  to itself rather than one it leaves for the next -- and the sweep would have to sweep an
  arbitrary path to close it, which is not something a fixed list can do. Named here rather than
  guarded.

- **Concurrency**, exactly as in #101 and #137: with one account for the deployment a session
  running beside this one can plant between a sweep and the command it protects. A pool closes it,
  since no two concurrent sessions share a home.

- **`/etc/gitconfig` and `/etc/ssh/ssh_config`** are root's and outside the session's reach, which
  is what makes them the supported place for a deployment to put global git or ssh config. Not a
  residual so much as the other side of the decision above.

## Tests

`tests/test_agent_runas.py`: the sweep removes all three files and leaves their neighbours
(`~/.config/gh/hosts.yml`, `~/.ssh/known_hosts`) and the directories themselves; the list is
pinned, including the fact that `XDG_CONFIG_HOME` is not passed through, which is what makes the
second git spelling the path git actually reads; a symlinked `.ssh` is unlinked rather than
stepped through; a home that never held any of it is untouched. And, end to end beside #137's
profile proof, a planted `~/.gitconfig` alias does not run for the next session's `git` -- the
real wrapper, the real hook path, the real `bash -lc` and the real `git`, only sudo a fake --
two-sided, so with the sweep removed the alias *is* what `git` runs.
`tests/test_image_layout.py` pins the CI step, and the CI `docker` job proves it in the real
image, the real uid split and the real home, through the worker's own `RunAs("agent").sweep_home()`:
both git spellings run an alias before the sweep and neither has one after, `~/.ssh/config` and
both git files are gone, and `gh`'s config and `known_hosts` are still there.
