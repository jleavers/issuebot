# A `.issuebot/env` line does not decide what the dynamic loader maps into the next session's tools

Date: 2026-09-22
Status: implemented
Issue: #187 (related to #179, #171, #151, #137, #101, #126, #104, #121)

## Problem

`.issuebot/env` is the workspace file a hook writes to hand variables to the session, bounded by
`PROTECTED_ENV_NAMES`/`PROTECTED_ENV_PREFIXES` (`agent/runner.py`, read only by
`merge_workspace_env`). #171 closed the names that re-point `git` and `gh`; #179 closed the names
`bash` itself reads at start-up.

The dynamic loader's names were in neither list. Both earlier notes filed `LD_PRELOAD` under
"the shape of what is left out: variables of *other* tooling, not rungs of git's or gh's own
chains" — beside `NODE_OPTIONS` and `PYTHONSTARTUP` — and #179 separated it out without deciding
it. **That filing does not survive inspection.** `NODE_OPTIONS` is a variable of a tool a hook
*chooses* to run; the loader's names are read by `ld.so` out of whatever environment the process
was handed, for **every dynamically linked program**, before that program reaches `main`. That
is the same set #171 and #179 drew their bound around: the `bash -lc` of every hook and the
post-clone setup, the `git` a session runs, and the `claude` child itself.

This note is that decision, taken per name.

### Measured

On `main`@a336a69, in the image itself: Debian glibc 2.41, GNU bash 5.2.37, **no compiler**.
`libmemusage.so` ships with glibc and prints a summary from a constructor and at exit, so it
stands in for the prebuilt shared object the issue describes a session fetching — which is the
honest form of the threat here, not a hypothetical build step.

Which of issuebot's own tools the loader actually reaches, checked rather than assumed:

```text
bash     /usr/bin/bash          ELF, dynamic
git      /usr/bin/git           ELF, dynamic
claude   /usr/local/bin/claude  ELF, dynamic
gh       /usr/bin/gh            ELF, static      <- not reached
```

**`gh` is a static Go binary and is the one exception**, so the issue's "every tool issuebot
launches" is very nearly right rather than exactly right. It is recorded here rather than
repeated, because the three that *are* reached include the shell every hook is and the `claude`
every turn is, which is more than enough for the reach argument to carry.

Then the channel end to end through issuebot's own seam. A throwaway probe over
`workspace_environment` with a real `.issuebot/env` logged
`workspace_env_applied keys=['LD_PRELOAD', 'LD_AUDIT', 'LD_LIBRARY_PATH', 'DATABASE_URL']` and
handed all three to the merged environment. That probe is not committed, deliberately: the two
new tests below carry the same claim permanently and fail without the change, which is a better
record than a script in the workspace.

**Reach.** The same as #171's and #179's, and stated with the same qualifier: `.issuebot/env`
lives in one workspace, a workspace belongs to one issue and is bound to one account and sealed
`0700` between runs (#121, #75), so this is a channel from one session to the *next session on
that issue*, not to every later session at that uid. Persistence inside the session's privilege
domain, as in #101, #137 and #151 — not an escalation across it, which #75 closed.

**Exploitability** is lower than `BASH_ENV`'s and not zero, exactly as the issue says: the
default image carries no compiler, so a session needs a prebuilt object — which it can fetch
through the egress allow-list's own hosts, or build in an image that opted into a toolchain.
The measurements below used glibc's own `libmemusage.so` for that reason.

**Invariant.** A line in `.issuebot/env` cannot decide which shared objects the dynamic loader
maps into the next session's `bash`, `git` or `claude`, nor stop those programs running at all.
What it still cannot bound is every tool a hook may choose to run, which no denylist can; the
bound is on the loader that runs under all of them.

## Decision

**All five are protected: `LD_PRELOAD`, `LD_AUDIT`, `LD_LIBRARY_PATH` — and
`LD_TRACE_LOADED_OBJECTS` and `LD_DEBUG`, which the measurements turned up and which are the
two that fail silently.**

`LOADER_ENV_NAMES` (`agent/runner.py`, beside `TOOL_CONFIG_ENV_NAMES` and `SHELL_ENV_NAMES`,
joined to `PROTECTED_ENV_NAMES` and pinned by a test). Each name on its own terms, since the
legitimate uses differ:

### `LD_PRELOAD` — protected

Objects mapped ahead of all others into every dynamically linked program, their ELF
constructors run before `main`.

```text
$ LD_PRELOAD=/lib/x86_64-linux-gnu/libmemusage.so bash -lc 'echo hook-ran'
Memory usage summary: heap total: 52774, heap peak: 50294, stack peak: 20352
 malloc|  78  41780  0                                   (... then:)
hook-ran
$ LD_PRELOAD=/lib/x86_64-linux-gnu/libmemusage.so git --version
Memory usage summary: heap total: 27489, heap peak: 16900, stack peak: 2272
```

**No legitimate hand-over use here at all.** A hook that wants to preload something for a
command *it* runs writes `LD_PRELOAD=... cmd` in its own shell, which is unchanged. Handing the
variable to the next session is the only thing refused, and nothing wants to.

### `LD_AUDIT` — protected

The rtld-audit interface, loaded earlier still. The interesting measurement is the negative one:
the loader runs the named object's constructors **even when it is not a valid audit module**, so
"it would have to implement `la_version`" is no kind of bound on what it may run.

```text
$ LD_AUDIT=/lib/x86_64-linux-gnu/libmemusage.so bash -lc 'echo hook-ran'
Memory usage summary: heap total: 0, heap peak: 0, stack peak: 0    (the constructor ran anyway)
```

Same shape as `LD_PRELOAD`, same absence of a hand-over use. Protected.

### `LD_LIBRARY_PATH` — protected, and this is the one with a cost

The directories a `DT_NEEDED` soname is resolved through, ahead of the system ones. It names no
object, and that is the whole of the case for the split verdict the issue floats. **The case
does not survive the measurement.** A file planted at a soname the target needs, in a directory
of the line's choosing, is what the loader maps — and its constructor runs — with no
`LD_PRELOAD` anywhere:

```text
$ cp /lib/x86_64-linux-gnu/libmemusage.so libs2/libpcre2-8.so.0
$ LD_LIBRARY_PATH=/tmp/probe187/libs2 git --version
Memory usage summary: heap total: 27489, heap peak: 16900, stack peak: 2272
$ LD_LIBRARY_PATH=/tmp/probe187/libs ldd $(command -v git) | grep pcre2
	libpcre2-8.so.0 => /tmp/probe187/libs/libpcre2-8.so.0
```

So it reaches the same place by one more step, and the extra step — knowing a soname the target
links against — is `ldd` away. It is `PATH`'s rule one layer down, and **that is precisely the
reasoning already in this list twice**: `PATH` is protected so that `git` is the `git` issuebot
installed, and #179 protected `CDPATH` in as many words as "`PATH`'s rule for the one lookup
`PATH` does not cover". `LD_LIBRARY_PATH` is `PATH`'s rule for the lookup below both.

The cost is real and is stated rather than waved away. A target repository's `after_create` may
legitimately build against a library in a private prefix, whose tests the *agent's turn* then
runs — and a turn is not the hook's shell, so "export it around your own command" does not cover
that case. Three routes do, and `docs/toolchains.md` carries them:

- **A `RUNPATH` baked in at link time** — `-Wl,-rpath`, or `LD_RUN_PATH`, which is binutils
  `ld`'s link-time default and is **deliberately left unprotected**. This is the correct fix
  rather than a workaround: a built artefact that needs a library at run time has `RUNPATH` for
  exactly that, and `LD_LIBRARY_PATH` is the override you reach for while testing one. It is
  also why the common `after_create` never needs the variable — Python wheels (auditwheel) and
  node native modules already carry theirs.
- **`/etc/ld.so.conf.d/*.conf` with `ldconfig`, in an image built `FROM` this one.** Root's,
  outside the session's privilege domain — the same route `docs/toolchains.md` already gives for
  `/etc/gitconfig` and `/etc/ssh/ssh_config`, and better than a variable for a deployment-wide
  setting.
- **The hook's own shell**, `LD_LIBRARY_PATH=... cmd`, unchanged. What is bounded is the
  hand-over, never the hook's own environment.

This is the same trade #171 made and documented for `XDG_CONFIG_HOME`: a route to running code
in every tool issuebot launches is not one to leave open for the convenience of a build that has
a correct fix available.

### `LD_TRACE_LOADED_OBJECTS` — protected; found by measurement, not in the issue

Not a way to run code but a way to run **none**. The loader prints the dependency list and exits
**0** without entering `main`:

```text
$ LD_TRACE_LOADED_OBJECTS=1 git rev-parse --show-toplevel   -> a library list; exit 0
$ LD_TRACE_LOADED_OBJECTS=1 claude --version                -> a library list
$ LD_TRACE_LOADED_OBJECTS=1 bash -lc 'echo hook-ran'        -> a library list; `echo` never ran
```

One line voids every dynamically linked tool the next session touches **while reporting
success**: every hook "passes", and the turn's `claude` never starts. That is the half of this
list's rule that `PATH`, `HOME`, `GH_TOKEN` and the fixed entries already serve — "so a typo
cannot take either down in the middle of a run" — and it is the only entry in the whole file
that fails *silently*. It is one name in the same list, found while measuring the three the
issue names, and closing it here rather than filing it is the cheaper honest option. It is
called out as a fourth in the pull request so a reviewer sees it was a judgement and not a
smuggled scope increase.

### `LD_DEBUG` — protected; the same denial by a second spelling, and the one nearly missed

This one is recorded at length because the first draft of this change got it wrong, in the
instructive way. `LD_DEBUG` was measured with `libs` and `all`, found inert, and written into the
documentation and the tests as *certified safe*. It is not. **Any** value containing `help` makes the
loader print its option list and exit 0 without entering `main`:

```text
$ LD_DEBUG=help bash -lc 'echo hook-ran'    -> the option list; `echo` never ran; rc=0
$ LD_DEBUG=help git rev-parse --show-toplevel   -> the option list, not an answer; rc=0
$ LD_DEBUG=help claude --version            -> the option list; rc=0
$ LD_DEBUG=libs,help bash -lc 'echo hook-ran'   -> the same; it is a substring, not the value
$ LD_DEBUG=libs git --version               -> git version 2.47.3   (every other value is inert)
```

Worse than `LD_TRACE_LOADED_OBJECTS` in one respect: the loader writes this to **stdout**, so it
also displaces whatever a hook's stdout was being read for.

The lesson generalises past this name, which is why it is here rather than in a footnote: a
variable is not made safe by measuring a value. The rule has to be applied to what the variable
*can* be set to, and the value in the end-to-end test is `help` for that reason.

`LD_DEBUG_OUTPUT` stays out, and the contrast is the check on the rule: it only redirects what
`LD_DEBUG` asks for and is inert on its own, measured leaving `git --version` working with no
`LD_DEBUG` set.

### Why names, and not an `LD_` prefix

#171 chose whole prefixes for `GIT_`/`GH_` because an enumeration is one somebody has to keep
complete. The opposite answer is right here, and one counter-example decides it: **`LD_RUN_PATH`
is not the runtime loader's variable at all** — it is binutils `ld`'s link-time default for
`-rpath`, and it is the very route a hook is pointed at instead of `LD_LIBRARY_PATH`. An `LD_`
prefix would refuse the recommended workaround.

The rule is therefore stated and finite: *what makes the dynamic loader load an object of the
value's choosing into every dynamically linked program, or run none at all* — checkable against
`ld.so(8)`'s ENVIRONMENT section, as #171's rule is checkable against `git-var(1)` and #179's
against `bash(1)`'s "Invocation".

## What this does not close

- **The rest of `ld.so(8)`'s environment, out by the rule and measured rather than assumed.**
  `LD_BIND_NOW` and `LD_DYNAMIC_WEAK` change how symbols bind; `LD_DEBUG_OUTPUT` and
  `LD_PROFILE`/`LD_PROFILE_OUTPUT` write diagnostics the session could write at its own uid
  anyway; `LD_ORIGIN_PATH` applies to setuid binaries, which none of these is. Each was measured
  leaving `git --version` working, and none names an object the loader would not otherwise have
  loaded. (`LD_DEBUG` was in this bullet in the first draft and is now protected: see above.)
  `GLIBC_TUNABLES` is glibc's tunables namespace — allocator and hwcap parameters, no object —
  and is named here because it is the other name a reader will ask about.
- **`LD_RUN_PATH`**, above: out on purpose, pinned by a test as unprotected, and the reason this
  is four names rather than a prefix.
- **musl.** The names are the same there, so the bound is spelled the same on a musl base; the
  measurements are glibc's, which is what this image is.
- **Other tooling's variables** — `NODE_OPTIONS`, `PYTHONSTARTUP` and their kind — which is
  where `LD_PRELOAD` was wrongly filed and which this note corrects for the loader alone.
  `.issuebot/env` is a denylist and has to be: its purpose is handing over what a target
  repository's tests need, which cannot be enumerated in advance. So this bounds the tooling
  *issuebot itself* launches and is never a claim that the next session's environment is
  uninfluenced. Fails safe: a gap is a name that still gets through, never a broken session.
- **`gh` was never exposed**, being static. Nothing here changes it either way.
- **The clone, and the workspace generally.** A workspace outlives its run (#180). What is
  closed is the loader under the programs issuebot starts, not the files in the tree it starts
  them in — a `.so` the session leaves in the clone is still there, it simply has no variable to
  get itself loaded by.
- **The refusal is visible only in the worker's log** (`workspace_env_ignored`, one line naming
  the key), not to the hook that wrote it. That is the existing behaviour for every protected
  name; `docs/toolchains.md` is where a hook author finds the rule before debugging a
  variable that silently did not arrive.

## Tests

`tests/test_agent_runner.py`: `merge_workspace_env` refuses each of the five, one parametrised
case per name annotated with what the loader does with it, and still applies an ordinary key
beside it. Seven negative cases pin that the bound stops where it says it does — `LD_RUN_PATH`
first among them, with `LD_BIND_NOW`, `LD_DYNAMIC_WEAK`, `LD_PROFILE`, `LD_DEBUG_OUTPUT`
(annotated with why it is out while `LD_DEBUG` is in), `GLIBC_TUNABLES` and `LDFLAGS`.
`LOADER_ENV_NAMES` is pinned as a list, as the
sweep lists, `TOOL_CONFIG_ENV_NAMES` and `SHELL_ENV_NAMES` are, so dropping a name is a
deliberate edit in two places, with the rule in the docstring. End to end through
`workspace_environment` with a real `.issuebot/env`, a file carrying all five plus `LD_RUN_PATH`
and a `DATABASE_URL` hands the next turn its DSN *and its `LD_RUN_PATH`* and nothing else, with
one `workspace_env_ignored` line per refused key naming the key and never its value. The
`LD_DEBUG` line carries `help` and not `libs`, since the value is what makes it a denial.

`tests/test_agent_workspace.py`: the channel through the shell a hook actually gets, and
compiler-free — three of the five are visible without building anything. A `before_run` hook
writes all five plus a `DSN` into `.issuebot/env`, with `LD_PRELOAD` naming a file that is not an
ELF object and `LD_DEBUG` set to `help`; the `after_run` hook — run through `bash -lc`, the shipped `hook_shell` — prints
`hook-ran` and the DSN. Its stdout is exactly those two lines, its stderr carries no `ld.so`
complaint and not the planted path, and the worker's log names all four keys.

Two-sided, and the failure is the proof: with `*LOADER_ENV_NAMES` taken back out of
`PROTECTED_ENV_NAMES`, all eight of these tests fail, and the workspace one fails by printing
the loader's own output where `hook-ran` should be, with the `LD_PRELOAD` and `LD_AUDIT`
complaints about the planted file on its stderr — and `exit_code: 0`.

```text
E       AssertionError: assert ['Valid optio...cessing', ...] == ['hook-ran', ...issuebot@/db']
E         At index 0 diff: 'Valid options for the LD_DEBUG environment variable are:' != 'hook-ran'
'stderr': "ERROR: ld.so: object '.../plant.so' cannot be loaded as audit interface: file too short; ignored.
           ERROR: ld.so: object '.../plant.so' from LD_PRELOAD cannot be preloaded (file too short): ignored."
```

`LD_DEBUG` wins the race to deny here because the loader reaches it first; with that one line
removed the same test fails on `LD_TRACE_LOADED_OBJECTS` printing the shell's own library list
instead. Either way the hook exits 0 having run neither `echo`.
