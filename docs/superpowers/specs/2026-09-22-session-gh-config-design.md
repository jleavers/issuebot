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
$ head -5 captured.txt                      # what the listener on that socket received
GET /user HTTP/1.1
Host: api.github.com
User-Agent: GitHub CLI 2.100.0
Accept: */*
Authorization: token ghp_SENTINELTOKEN0123456789012345678901
```

Three harms on that one ordinary command, and each is a different boundary:

- **The token is handed over.** `GH_TOKEN` is in every session's environment and every hook's,
  and it reaches the listener in the `Authorization` header verbatim.
- **The answer is forged.** `gh api user` is `GhCliAdapter.own_login()`, and #77's provenance
  rule resolves *which pull request and which workpad comment are issuebot's own* by that
  login. A login the session chose is a session choosing what issuebot reads as its own
  artefacts.
- **Nothing on the network path sees it.** #126 bounds egress by giving the worker `internal`
  networks alone and an allow-listing `CONNECT` proxy; a unix socket is not a route, so the
  proxy is not bypassed so much as never consulted.

And it reaches issuebot's own work rather than only a session's. `gh repo clone` -- the
worker's clone of the target repository, which runs through `RunAs` at the session's uid --
went through the planted socket carrying the token:

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
| `api_host` | A per-host setting. Measured as a global key it resolves (`gh config get api_host` reads it back) but does not re-point an ordinary command: with `api_host: 127.0.0.1:9` in place, `gh api user` still reached api.github.com and came back with GitHub's own `Bad credentials`. Swept with the file either way |

`aliases` is worth stating separately, because "the file `gh config set` writes" is what made
the trade sound even. It is not on `gh config list` at all, and `gh config set` does not write
one:

```text
$ gh config set aliases.pwn '!echo x'
! warning: 'aliases.pwn' is not a known configuration key
```

-- and it writes a dead top-level `aliases.pwn:` key rather than an alias. The alias channel is
`gh alias set`'s, a different command writing the same file. So the command the argument is
about and the channel the issue is about do not meet.

## And the cost is nil, which is the other half

`gh` writes itself a fresh default `config.yml` on the next invocation, including a read-only
one, and reads `hosts.yml` perfectly well with the file absent:

```text
$ ls ~/.config/gh                 # config.yml removed, hosts.yml left
hosts.yml
$ HOME=... gh api rate_limit --jq .rate.limit
5000
$ ls ~/.config/gh
config.yml  hosts.yml
$ cat ~/.config/gh/config.yml
version: "1"
```

So the file is self-healing, and a hook's `gh config set` still configures the `gh` in its own
shell; what it no longer does is hand a setting to the *next* session, which is the same line
#171 drew for the same two tools' environment variables. `gh` has no `/etc` file to point a
deployment at the way git has `/etc/gitconfig`, and needs none: everything a deployment
legitimately sets is in the table above, already answered by the protected environment.

## Design

- **A fourth entry in `TOOL_CONFIG_SWEEP`** (`agent/runas.py`): `(".config", "gh",
  "config.yml")`. Path components like the other three and for the same reason -- `_walk`
  resolves one at a time and yields the first symlink it meets, so a `.config` or a
  `.config/gh` replaced by a link is unlinked as the plant it is rather than stepped through.
  Nothing else changes: no new setting, no image change, no new call site, since `_sweep`
  already runs before every turn and before every script that opens a login shell (#137).

- **`hosts.yml` stays, and so does `~/.config/gh`.** The invariant #151 pinned, kept for the
  reason it was pinned: a credential authenticates the next session rather than steering it,
  which is the line `.claude/.credentials.json` already sits on. Still a denylist, still files
  and never a directory.

## Residuals

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
  #186 rather than folded in.

- **Concurrency**, as in #101, #137 and #151: with one account for the deployment a session
  running beside this one can plant between a sweep and the command it protects. A pool closes
  it, since no two concurrent sessions share a home.

## Tests

`tests/test_agent_runas.py`: the fourth entry is pinned beside the other three, with
`(".config", "gh")` and `(".config", "gh", "hosts.yml")` pinned as *not* on the list, so
taking the directory or the credential would be a deliberate edit; `_plant_home` plants a
`config.yml` carrying both channels, so every sweep test in the file -- the mode-locked plant,
the symlinked component, the neighbours -- covers it; and two end-to-end proofs through the
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
