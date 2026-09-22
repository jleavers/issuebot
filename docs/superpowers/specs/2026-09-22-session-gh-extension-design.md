# A session account's `gh` extensions do not carry between sessions

Date: 2026-09-22
Status: implemented
Issue: #186 (related to #173, #151, #137, #101, #75)

## Problem

#101 sweeps the session account's `~/.claude` before every turn, #137 aimed the sweep at the
home itself and took the shell start-up files a login shell runs, and #151 took it one tool
further out: the config files a *tool* the session runs reads out of that home, each of which
can name a command. #173 asks the same question of `~/.config/gh/config.yml`.

`gh` has a second surface in that home, of a different kind, and #151 reached for none of it:
**extensions**. `gh extension install` puts an executable under
`~/.local/share/gh/extensions/gh-<name>/`, and `gh <name>` runs it. That is not a config file a
tool reads, which is what `TOOL_CONFIG_SWEEP` names, so it is its own question — an executable
the session *wrote*, rather than a setting naming one.

Persistence *within* the session's privilege domain, exactly as in #101, #137 and #151, and not
an escalation across it (#75 closed that): gone on a container recreate, and under a pool (#121)
the sharing is with the next session bound to that account rather than with the ones beside it.

## Measured

`gh version 2.100.0` on this image, run as a session account, against a throwaway `HOME`.

**The install step is not part of the channel.** A directory and an executable file, written by
hand, are dispatched exactly as an installed extension is — no manifest, no registry entry, no
network, and `gh` lists it as its own:

```text
$ mkdir -p $R/home/.local/share/gh/extensions/gh-pwn
$ printf '#!/bin/sh\necho PLANTED-GH-EXTENSION-RAN\n' > .../gh-pwn/gh-pwn && chmod +x ...
$ HOME=$R/home gh pwn
PLANTED-GH-EXTENSION-RAN
$ HOME=$R/home gh extension list        # with a hosts.yml in place; see below
gh pwn
```

(Paths abbreviated. `gh extension list` is the one line here that wants a credential -- it
exits 4 with `gh auth login` advice without one -- where the dispatch itself does not, which is
the next measurement but one.)

**It has #151's narrow property for `aliases:`, not the `http_unix_socket` one #173 is
weighing.** An extension cannot shadow a core command, and no ordinary `gh` command touches the
directory at all:

```text
$ HOME=$R/home gh issue --help          # with .../gh-issue/gh-issue planted
Work with GitHub issues.
$ HOME=$R/home gh --version; gh api user; gh issue list; gh help; gh extension list
marker absent: no ordinary command executed the plant
```

(The last line is the summary of the check, not `gh` output: the plant was a script that
`touch`ed a marker file, and the file was not there. `gh repo` and `gh auth` were held to the
same test.)

**`gh` dispatches from that one directory and nowhere else.** Not from `PATH`, the way `git`
finds `git-<name>`, and not from `GH_CONFIG_DIR`; `XDG_DATA_HOME` moves the whole lookup and
hides the home:

```text
$ HOME=$R/home PATH="$R/bin:$PATH" gh pathpwn        # gh-pathpwn executable on PATH
unknown command "pathpwn" for "gh"
$ HOME=$R/home GH_CONFIG_DIR=$R/ghcfg gh cfgpwn
unknown command "cfgpwn" for "gh"
$ HOME=$R/home XDG_DATA_HOME=$R/xdg gh xdgpwn
PLANTED-XDG-DATA-HOME-RAN
$ HOME=$R/home XDG_DATA_HOME=$R/xdg gh pwn
unknown command "pwn" for "gh"
```

**A session can overwrite an extension a deployment installed.** This is the measurement the
decision turns on, and the one the narrow reading above misses:

```text
$ HOME=$R/home gh deploy
DEPLOYMENT-EXTENSION
$ printf '#!/bin/sh\necho SESSION-REPLACED-IT\n' > .../gh-deploy/gh-deploy
$ HOME=$R/home gh deploy
SESSION-REPLACED-IT
```

**`gh` injects no credential into an extension.** With `GH_TOKEN` unset in the caller and a
`hosts.yml` in place, the child saw the caller's own environment — `GH_NO_UPDATE_NOTIFIER`,
`GH_PAGER` and `GH_PROMPT_DISABLED` among it, which are issuebot's `FIXED_ENVIRONMENT` and not
`gh`'s doing — plus `GH_EXTENSION`, and no `GH_TOKEN`. What the measurement is for is the
absence rather than the list, which is `gh`'s to grow: an extension is a program `gh` hands its
argv and its own environment to, and nothing else.

And the issue's framing holds: this is `gh`'s own lookup and not the shell's. A login shell's
`PATH` is root's, with no directory of the account's on it.

```text
$ bash -lc 'echo "$PATH"'
/opt/uv/bin:/opt/postgresql/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games
$ bash -lc 'case ":$PATH:" in *":$HOME/.local/bin:"*) echo YES ;; *) echo NO ;; esac'
NO
```

**Invariant.** A session cannot leave a program at the path `gh` dispatches from — the
extension directory in the session account's home — for a later session's `gh` to run.

The path, because the lookup itself can be moved and `XDG_DATA_HOME` moves it (measured
above). That was written here as a residual and filed as #191, which has since landed: the
name is in `TOOL_CONFIG_ENV_NAMES`, so a workspace's `.issuebot/env` is refused it, and it
was never in `PASSTHROUGH_NAMES`, so it is not inherited from the worker either. With both
halves the sweep of the default path is a guarantee rather than a default, which is the
standing `XDG_CONFIG_HOME` gives `TOOL_CONFIG_SWEEP`'s git entry. What no list bounds, here
or there, is a session exporting the name in its own shell for its own `gh` — a session
running its own code needs no extension directory to do that.

## The decision the issue asks for: swept

The issue asks the narrow-channel question — a plant on an invented subcommand name fires only
if some later session happens to invoke that name — against a deployment that legitimately
installs an extension for its sessions. The two are not independent, and that is what settles
it.

**Where a deployment has installed an extension, the channel is not narrow at all.** The
directory is the account's to write, so a session can replace the executable of an extension the
deployment installed (measured above), and the plant then fires on the ordinary command that
deployment's own sessions and hooks already run — `core.pager`'s shape, not the invented name's.
So the deployment with the strongest reason not to sweep is the deployment where not sweeping
costs the most. Swept, the same deployment gets an `unknown command` from `gh`: a loud failure
its operator fixes, rather than a quiet substitution nobody sees.

**And where no deployment installed one, a name on the list costs nothing.** That is #151's
argument for `~/.ssh/config`, which nothing issuebot runs reads today: the entry is free where
the directory does not exist, and the alternative is a documented residual that comes back the
first time a deployment installs an extension.

**What such a deployment does instead.** An extension is an executable that `gh` hands its argv
and its own environment to; it is not given a credential, a library or a protocol of `gh`'s.
So the same program, installed root-owned on the session's `PATH` — `/usr/local/bin/<name>` in
an image built `FROM` this one — does the same work under its own name, from a location no
session can write and the sweep never touches. That is #137's and #151's answer (`/etc/profile`,
`/etc/gitconfig`, `/etc/ssh/ssh_config`: the system file, not the account's) in the only
spelling `gh` leaves, because `gh` has no system-wide extension location: measured above, it
dispatches from the data directory alone, not from `PATH`, not from `GH_CONFIG_DIR`, and not
from `GH_DATA_DIR`, `GH_EXTENSION_DIR` or `XDG_DATA_DIRS`, none of which this `gh` honours.
What such a deployment loses is the `gh ` prefix on the command, and nothing else.

**The cost, stated rather than argued away.** The sweep runs before every turn and before every
script that opens a login shell, so an extension an `after_create` hook installs is gone before
turn 1, and one a session installs for itself is gone by its next turn. A session still has one
it installed for the rest of that turn -- the sweep is between turns and before each login
shell, not inside one -- and an extension is an ordinary program, so a session that needs it
past a sweep can keep it in the workspace and run it by path. What it cannot do is leave it in
the home for somebody else.

## Design

- **`TOOL_EXTENSION_SWEEP`** (`agent/runas.py`), beside `TOOL_CONFIG_SWEEP` and pinned by a test
  the same way: `.local/share/gh/extensions`. A list of its own rather than a fourth entry in
  that one, for the reason #151 gave for keeping its list apart from `SHELL_STARTUP_SWEEP` —
  the two are checked against different things. `TOOL_CONFIG_SWEEP` names config a tool
  *reads*, checkable against `git-config(1)` and `ssh_config(5)`; this names a program the
  session *wrote*, checkable against `gh extension`'s own layout, and it answers the
  "what does a deployment do instead" question differently, since `gh` has no `/etc` of its own.

- **Directory-specific, as the issue requires.** `extensions` and not `~/.local/share/gh`, which
  is the tool's data directory; and certainly not `~/.local/share` or `~/.local`, which are
  every tool's. `~/.local/state/gh` sits beside it — `gh`'s own state, holding `device-id` —
  and `tests/test_agent_runas.py`'s `_plant_home` already pins it as a survivor. The same
  file-or-directory rule `hosts.yml` forced on #173's question.

- **Walked, not joined.** The entry is its four path components, like the nested entries of
  `TOOL_CONFIG_SWEEP`: `_walk` resolves one component at a time and yields the first symlink it
  meets, so a session that replaces `.local`, `share`, `gh` or `extensions` with a link has the
  link unlinked as the plant it is, rather than the tree it points at swept.

- **Nothing else changes.** No new setting, no image change, no new call site: `_sweep` already
  runs before every turn and before every script that opens a login shell (#137), and both
  halves of that schedule are what a `gh` invoked by a hook, by the post-clone setup or by the
  session itself sits behind.

## Residuals, and what has closed since

- **`XDG_DATA_HOME`, through `.issuebot/env` — recorded here, filed as #191, and closed by
  it.** It moves the extension lookup wholesale (measured above) and is not inherited from the
  worker (`PASSTHROUGH_NAMES`), so the one way it reached a session was the workspace's own
  `.issuebot/env` — which a hook writes, and which sits in the agent's own workspace, so the
  session can write it too, and which outlives the session: the reach was that workspace and
  the issue it belongs to, rather than the account's every later session, which is what the
  home is. Filed separately rather than folded in, exactly as #151 filed #171, and for the same
  reason: protecting a name is a decision about what a hook may configure, and it had to be
  weighed against `XDG_DATA_HOME` being the only escape hatch a deployment has for a `gh`
  extension, since `gh` has no system-wide location for one. #191 weighed it that way and
  protected the name (`2026-09-22-session-gh-extension-env-design.md`); the answer for such a
  deployment is the `PATH` route above, which is the one this note already gives and the one a
  session cannot reach. The pin here (`tests/test_agent_runas.py`) asserts both halves, so
  this list's promise fails if either moves.

- **Concurrency**, exactly as in #101, #137 and #151: with one account for the deployment a
  session running beside this one can plant between a sweep and the command it protects. A pool
  closes it, since no two concurrent sessions share a home.

- **`~/.config/gh/config.yml`** is #173's question and is untouched here. The two are different
  surfaces with different shapes — a setting that names a command against a program that *is*
  one — and this change neither waits on that decision nor forecloses it.

## Tests

`tests/test_agent_runas.py`: `_plant_home` gains a planted extension beside `gh`'s state
directory, so every existing sweep test carries it; the sweep removes the extension directory
and leaves `~/.local/state/gh`, `~/.local/share` and `~/.local`; the list is pinned, including
both halves of what makes `~/.local/share` the path `gh` actually reads — `XDG_DATA_HOME` is
not passed through from the worker and, since #191, not settable from `.issuebot/env` either; a symlink at any of the four components is unlinked rather than followed, and
the tree it points at is neither removed nor walked. And, end to end beside #137's profile proof
and #151's gitconfig one, a planted extension does not run for the next session's `gh` — the
real wrapper, the real hook path, the real `bash -lc` and the real `gh`, only sudo a fake —
two-sided, so with the sweep removed the plant *is* what `gh` runs, while `~/.local/state/gh`
survives both halves. That end-to-end test needs two things the earlier ones did not, both
commented where they are: the real `PATH` rather than the suite's `fake_path()`, since
`tests/fakes/gh` would shadow the `gh` whose dispatch is the question; and a well-formed
`~/.config/gh/hosts.yml` over the marker `_plant_home` leaves, since the real `gh` refuses to
run at all against a host entry it cannot migrate, and a `gh` that never reached its dispatch
would pass the swept half for the wrong reason.

The CI `docker` job's home-sweep step gains the same arm against the image's own `gh`, beside
#137's profile and #151's gitconfig: the plant runs before the sweep and is an `unknown command`
after it, the extension directory is gone and `~/.local/state/gh` is still there. That is the
real uid split, the real sudo rule and the real home, through the worker's own
`RunAs.sweep_home`.
