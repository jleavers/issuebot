# The steering keys in `~/.config/gh/hosts.yml` do not carry between sessions

Date: 2026-09-22
Status: implemented
Issue: #190 (related to #173, #151, #137, #126, #101, #75)

## Problem

#151 swept the config a *tool* the session runs reads out of the account's home — git's two
spellings and `~/.ssh/config` — and pinned `~/.config/gh` as a survivor beside them. #173 took
`~/.config/gh/config.yml` as well, on the strength of `http_unix_socket`: a key that re-points
`gh`'s HTTP transport on an *ordinary core command*, where `aliases:` fires only on an invented
subcommand name. It kept `~/.config/gh/hosts.yml`, which was both its acceptance bar and the
invariant #151 pinned: `hosts.yml` is credential state, the line `~/.claude/.credentials.json`
sits on — it authenticates the next session rather than steering it.

That decision left a residual, and this is it. **`hosts.yml` is not only credential state.**
`gh config set -h <host> <key> <value>` writes *there*, not into the `config.yml` #173 takes,
and one of the keys it can carry re-points `gh` on an ordinary core command.

**Where #173 stands matters for reading the rest of this, so it is said here rather than in a
footnote: it has not landed.** PR #189 is open, `~/.config/gh/config.yml` is *not* swept on
`main` today, and nothing in this change sweeps it. The reproductions below are run in the state
the issue specifies — a `hosts.yml`, no `config.yml` anywhere — because that isolates what this
file contributes, which is the question asked. What that state is not is a description of
`main`, and the section on what this does not close says exactly which half is left over.

## Reproduction

All of it against `gh version 2.100.0 (2026-09-03)`, the `gh` in this image, in the exact
post-#173 state — a `hosts.yml`, no `config.yml` anywhere — with throwaway `HOME`s and
listeners opened for the purpose.

```text
$ cat ~/.config/gh/hosts.yml
github.com:
    oauth_token: not-a-real-token
    user: nobody
    api_host: 127.0.0.1:8099
$ HOME=... GH_TOKEN=ghp_SENTINEL... gh api user
Get "https://127.0.0.1:8099/user": http: server gave HTTP response to HTTPS client
```

It is not confined to `gh api`. A bare hostname — which is what the key is documented to take —
reaches every core command, issuebot's own clone included:

```text
$ HOME=... gh issue list -R jleavers/issuebot          # api_host: localhost
Post "https://localhost/graphql": dial tcp [::1]:443: connect: connection refused
$ HOME=... gh repo clone jleavers/issuebot /tmp/clone1 # api_host: 127.0.0.1
Post "https://127.0.0.1/graphql": dial tcp 127.0.0.1:443: connect: connection refused
$ HOME=... gh repo clone jleavers/issuebot /tmp/clone3 # control, no api_host
Cloning into '/tmp/clone3'...
```

`gh repo clone` is `WorkspaceManager._run_argv`, run through `RunAs` as the session account. So
the previous session's leavings break the *next* session's workspace clone.

The validation #173 recorded is narrower than it reads: `gh repo clone`, `gh issue list` and
`gh pr list` refuse a value carrying a scheme or a port (`must be a hostname without a scheme
or port`) while `gh api` honours `host:port`. They do not refuse the substitution — only that
spelling of it.

## How far it reaches, measured

The three bounds #173 asserted were all untested end to end. Each was measured here, and two of
them came back stronger than claimed while the third is exactly as claimed.

**The token does not follow the substituted host.** Measured with a listener holding a
certificate the client was made to trust, so the request completed and could be read in full:

```text
$ HOME=... GH_TOKEN=ghp_SENTINEL... gh api user        # api_host: localhost:18193
{"login": "forged", "data": {}}
--- everything the substituted host received ---
GET /user HTTP/1.1
Host: localhost:18193
User-Agent: GitHub CLI 2.100.0 Agent/claude-code_2-1-263_agent
Accept: */*
Content-Type: application/json; charset=utf-8
Time-Zone: Etc/UTC
X-Github-Api-Version: 2022-11-28
Accept-Encoding: gzip
```

No `Authorization` header — and the same with no `GH_TOKEN` at all, authenticating from the
file's own `oauth_token`. `gh help config` says so in as many words: *"`api_host`:
experimental: the hostname to use when making API requests for a GitHub host. Note: this is not
a security boundary and requests to the canonical host will remain authenticated"*. The control
is the `http_unix_socket` measurement below, where the same sentinel token reached the real
`api.github.com` and came back `Bad credentials`: the header is otherwise sent.

**A forged answer needs the system trust store, which is root's.** A self-signed certificate is
refused:

```text
$ HOME=... gh api user                                 # api_host: 127.0.0.1:18191
Get "https://127.0.0.1:18191/user": tls: failed to verify certificate: x509: certificate signed by unknown authority
```

The session's home offers no trust anchor: Go reads `/etc/ssl/certs`, root's, and the two
environment overrides that would change that (`SSL_CERT_FILE`, `SSL_CERT_DIR`) are the
environment's question rather than the home's.

**#126's proxy does refuse it, and that was the untested claim.** Through the deployment's own
`egress` service:

```text
$ https_proxy=http://egress:3128 HOME=... gh api user  # api_host: example.com   (OFF the list)
Get "https://example.com/user": Forbidden
$ https_proxy=http://egress:3128 HOME=... gh api user  # api_host: www.githubstatus.com (ON the list)
gh: HTTP 404
```

and the proxy's own record of the first, `{"event": "egress_denied", "host": "example.com",
"port": 443}`. Reproduced identically against a locally run `uv run issuebot egress`. The
allow-list is exactly the bound: an off-list name is refused, an on-list one completes — the
404 is a real answer from the substituted host, so the request was made.

**`http_unix_socket` is not honoured host-level.** The key #173's whole decision rests on
cannot be re-planted in the file that survives it. Planted in `hosts.yml` with no `config.yml`
anywhere, the request went to the real `api.github.com` and nothing reached the socket:

```text
$ cat ~/.config/gh/hosts.yml
github.com:
    oauth_token: not-a-real-token
    user: nobody
    http_unix_socket: /tmp/pwn.sock
$ HOME=... GH_TOKEN=ghp_SENTINEL... gh api user
{
  "message": "Bad credentials",
  ...
```

So `http_unix_socket` is #173's to close and not this file's — once #189 lands.

## The `pager`, `editor` and `browser` question, closed

Inert in this position, measured in both directions. The same value at top level — what #173
sweeps — fires, so this is a property of where the key sits and not of the test:

```text
# host-level, in hosts.yml, gh api under a real TTY
pager.flag:   (not created -- host-level pager did NOT run)
browser.flag: (not created -- host-level browser did NOT run)
# the identical value at top level, in config.yml
pager.flag:   TOP-PAGER-RAN
browser.flag: TOP-BROWSER-RAN
```

`gh config get <key>` with no `-h` returns empty for all three while `gh config get -h
github.com <key>` returns the plant, and the behaviour follows the hostname-less lookup — which
is the lookup gh's own pager, editor and browser resolution uses. (`gh config list` with no
`-h` does print the host-level values, so the *listing* and the *lookup* disagree; the lookup is
what runs a command.)

`editor` is the one of the three that could not be driven end to end, because every `gh` command
that opens an editor also creates an issue, a pull request, a release or a gist. It is closed by
that shared lookup rather than by its own invocation, and two things sit behind it in any case:
`FIXED_ENVIRONMENT` pins `GH_PAGER=cat` for every turn and every hook, and `GH_EDITOR`,
`GIT_EDITOR`, `VISUAL`, `EDITOR`, `GH_BROWSER` and `BROWSER` all outrank the config file in gh's
documented precedence — the chains #171 already protects in `.issuebot/env`.

They are swept anyway, for the reason a list is cheaper than a judgement: naming a key that does
nothing costs nothing, where leaving one out costs the channel back if a later `gh` starts
honouring it. "Not honoured today" is a property of this `gh`; "never written by a deployment"
is a property of the file.

## Decision: a key-level edit, keeping the credential

**Act.** The file stays and the steering keys come out of it.

What settles it is the reproduction against the bounds. The confidentiality story is closed —
no token, no forged answer without a root-owned trust anchor, no name off the allow-list. The
**availability** story is not, and needs no network at all: `api_host: 127.0.0.1` in a home the
sweep keeps breaks every core `gh` command of the next session bound to that account, including
the `gh repo clone` issuebot itself runs to build that session's workspace. It is cheap,
certain, persistent for the container's lifetime, and it fires on an ordinary command — which
is the exact property that settled #173 against #151's earlier reading.

The issue asks whether a key-level edit of a credential file is a shape this project should
take, and the answer is yes because the keys this position can carry are enumerable: `gh config`
manages a fixed set, `gh config --help` advertises it, and `gh config set -h <host> <key> <value>`
writes **every one of them** into `hosts.yml` rather than into `config.yml`.

Getting that measurement right took two attempts, and the first one is worth recording because
it is the easy thing to get wrong. `gh config set` *validates* the enum-valued keys, so a probe
passing a placeholder value is refused for eight of the thirteen — and a probe that swallows the
refusal reports only the five free-form ones and calls that the closed set. With a value each
key accepts, all thirteen land here:

```text
github.com:
    api_host: api.example.com
    git_protocol: ssh
    editor: vi
    prompt: disabled
    prefer_editor_prompt: enabled
    pager: cat
    http_unix_socket: /tmp/s
    browser: firefox
    color_labels: enabled
    accessible_colors: enabled
    accessible_prompter: enabled
    spinner: disabled
    telemetry: disabled
```

So the list is gh's own configuration surface, removed whole. That is the shape of the decision:
the sweep does not judge which keys are dangerous, it declines to let a session leave
*configuration* in a credential file — and what survives is what `gh config` does not manage,
which is `oauth_token`, `user` and the `users:` subtree. None of the thirteen is credential
state, so the acceptance bar is met by construction rather than by care.

Two of the thirteen are measured live from this position, and they are different shapes:

- **`api_host`**, #190's subject, and the only one that is *only* reachable from here — at top
  level it is inert (measured: a `config.yml` carrying `api_host: 127.0.0.1` left the request on
  the real `api.github.com`). So this change closes it completely.
- **`git_protocol`**, which shows why the list is the whole surface rather than a hand-picked
  pair:

  ```text
  $ gh config get -h github.com git_protocol      -> ssh      (bare lookup reads https)
  $ gh auth status                                -> Git operations protocol: ssh
  $ gh repo clone jleavers/issuebot /tmp/gp-clone
  Cloning into '/tmp/gp-clone'...
  error: cannot run ssh: No such file or directory
  $ which ssh
  (no ssh)
  ```

  Dropping the key leaves gh's own `https` default, which is what issuebot clones and pushes
  over — the post-clone setup's credential helper is a token, not a key. Unlike `api_host`,
  this one is *also* reachable at top level, which the section below is honest about.

The remaining eleven are `http_unix_socket`, `pager`, `editor` and `browser` — measured **inert**
in this position, against the same values at top level, which do fire — and seven that are
cosmetic or, like `prompt`, documented as global. They are removed all the same: naming a key
that does nothing costs nothing, where leaving one out costs the channel back if a later `gh`
starts honouring it from here.

Host-level only, and never deeper: a steering key inside the `users:` subtree is measured *not*
honoured. `users.<name>.api_host` left the request on the real `api.github.com`, where the
identical key one level up re-pointed it — so the subtree holding the per-account tokens is
preserved whole without leaving the channel open beneath it.

### Why a denylist, where `--strict-mcp-config` names what survives

#119 chose the opposite shape for `~/.claude.json`, and said why: clearing keys out of a file is
a denylist over an undocumented format, where a flag naming what survives covers what the format
grows next. The same argument does not carry here, because the two failures are not symmetrical.

A steering key a future `gh` adds and this list misses costs the channel measured above — which
carries no credential, forges nothing without root's trust store and reaches no name off #126's
allow-list. A keep-list that stripped a *credential* key a future `gh` adds would break
authentication for every session in the deployment, which is the cost the issue names as larger
than the channel. So the list is a denylist, and the asymmetry is covered by proving the set
rather than asserting it: the `docker` CI job reads the image's own `gh config --help` and
fails when it advertises a key this list does not name, and separately checks that
`gh config set -h` really does route each of them here — with a value each key accepts, and with
no `|| true` to swallow a refusal, which is what made the first attempt wrong. A release that
adds a fourteenth fails a pull request rather than a session.

The other half of #119's argument does not apply either: `~/.claude.json` is claude's own file
in a format nobody documents, while `hosts.yml` is a file `gh` itself rewrites in place. It
normalised the reproduction file on first use, adding a `users:` subtree that was never written
there, so a round trip through the parser is what this file already gets from the tool that owns
it.

## Design

`GH_HOSTS_FILE`, `GH_HOSTS_STEERING_KEYS` and `GH_HOSTS_LIMIT` in `agent/runas.py`, beside the
three sweep lists, and `_sweep_gh_hosts` called from `_sweep` after the path-level removals.

- **After the removals, not among them.** An intermediate symlink on the way to the file
  (`~/.config` replaced by a link) is already one of `TOOL_CONFIG_SWEEP`'s targets and is gone
  by then, so the walk resolves inside the real home; and this is an edit, so it belongs after
  every path-level decision has been made.
- **`_walk`, not a join**, so a link at `~/.config`, `~/.config/gh` or `hosts.yml` itself is
  yielded and unlinked rather than descended through. `gh` writes a regular file in a real
  directory, so a link at any of them is a session's redirection — and editing through one would
  rewrite a file outside the home altogether.
- **Fail-safe in one direction only.** Anything the sweep cannot read, cannot parse, or cannot
  understand as the mapping-of-hosts `gh` writes keeps its contents exactly as they are (its
  mode may have been widened to the owner read and write `gh` needs anyway, which is
  `_relax_file`'s repair and the only mark a declined file carries). Rewriting a
  credential file on a guess is the one outcome worse than the plant: a session whose `gh`
  cannot authenticate does no work at all, where a session carrying the plant is held by the
  bounds above. `GH_HOSTS_LIMIT` (256 KiB) is #110's rule at the one seam that parses a file the
  session can grow, and past it the file is untouched. The read is `O_NONBLOCK` with an `fstat`
  on the descriptor, `Boundary.read`'s rule: a FIFO at that name would otherwise hang the open
  waiting for a writer that never comes, once per turn and once per hook.
- **Not written at all unless a key came out.** The sweep runs before every turn and before
  every hook, and rewriting a credential file on each of those — reformatting it, racing a `gh`
  that is reading it — for no change is a cost with no benefit. A home no session has planted in
  keeps its `hosts.yml` byte for byte, inode included.
- **Atomic, and the original's mode.** A temporary file in the same directory and `os.replace`,
  so a `gh` running beside the sweep reads the old document or the new one and never a partial
  write of its own credentials. `gh` refuses a `hosts.yml` wider than `0600`, so the mode is
  carried across rather than taken from the umask.
- **Value-faithful, which a plain `safe_load` is not.** `_HostsLoader` strips PyYAML's implicit
  scalar resolvers, so every plain scalar loads as the text `gh` wrote. PyYAML resolves YAML 1.1
  where `go-yaml` — which is what reads this file — does not: `user: no` would come back
  `user: false`, and an all-digit `oauth_token` beginning with a zero would come back an integer
  in octal. Nothing here is compared as a number or a boolean; only key names are looked at.
- **Declined when the file moved under it.** The read and the write are two steps, and `gh`
  rewrites this file on ordinary commands of its own — it normalises the document, and it
  refreshes an OAuth token in place. So the rename is declined unless the name still resolves to
  the `(ino, dev, mtime_ns, size)` the document was parsed from: a rename over a file that
  changed in that window would discard a credential `gh` had just written. Declining costs the
  plant one more turn, where the sweep runs again; the other order costs a login. The file must
  also be this account's own, the rule `_relax` and `_relax_file` keep — a `hosts.yml` an
  operator seeded as root is not a session's plant, and replacing it with an account-owned copy
  would hand the next session a file it can rewrite freely. And the bytes are `fsync`ed before
  the rename, since the rename is what makes the new file the credential.
- **`_relax_file`**, the one new repair. The directory form is enough for every *removal* —
  unlinking needs the parent's bits and nothing of the file's own mode — but an edit has to
  *read* its target, so a session that plants a key and then `chmod 0000`s the file would
  otherwise keep it at no cost to itself. `gh` reads the file as its owner, which the plant's
  author and this sweep both are.

## What this does not close

- **A host the operator adds to `ISSUEBOT_EGRESS_ALLOW` and the session can also reach.** The
  bound on the substituted host is #126's allow-list, measured above. `DEFAULT_ALLOW` is
  GitHub's, Anthropic's and Slack's names, none of which a session controls; an operator who
  widens it to a host a session can influence widens this with it. That is the same property
  every other egress channel has, and it is the allow-list's to state.
- **`SSL_CERT_FILE` and `SSL_CERT_DIR`,** which would let a trusted certificate be named without
  touching root's store. They are an environment question, not a home one: `.issuebot/env` is
  the spelling that matters and neither name is in `TOOL_CONFIG_ENV_NAMES` today. Filed
  separately rather than folded in, since it is #171's list and not this file.
- **A key `gh` starts honouring host-level that `gh config` does not manage.** The list is gh's
  advertised configuration surface, and CI fails when that surface grows a key the list does not
  name — but a key `gh` reads from `hosts.yml` without ever advertising it in `gh config --help`
  would be invisible to both. Nothing suggests one exists today; the honest statement is that
  the list is as complete as gh's own documentation of itself.
- **The top-level half of the same keys, which is #173's and is not on `main`.** `config.yml`
  is not swept here and PR #189 is still open, so a session can write `gh config set
  git_protocol ssh` — no `-h` — and the next session's `gh repo clone` fails exactly as it does
  from `hosts.yml`. Measured: with the key only in `config.yml`, `gh config get -h github.com
  git_protocol` still reads `ssh`, `gh auth status` reports `Git operations protocol: ssh`, and
  the clone fails with `cannot run ssh`. **So `git_protocol` is closed by this change only in
  the position this change owns.** The one key closed outright is `api_host`, the issue's
  subject, because top level is inert for it — measured, a `config.yml` carrying
  `api_host: 127.0.0.1` left the request on the real `api.github.com`. Closing the other half is
  #189's to do and is deliberately not duplicated here.
- **A session can still deny the next one its `gh` by leaving the file unparseable.** The
  fail-safe branch deliberately keeps a `hosts.yml` it cannot understand, and `gh` then refuses
  to run at all (`failed to migrate config: cowardly refusing to continue`). That is the same
  availability class the decision was taken over, and it is not closable in this direction: the
  alternative is deleting a credential file on a parse error, which is the outcome this design
  ranks worst. The bound is that it is loud — every `gh` in the session says so — where a
  planted `api_host` is silent.

## Tests

`tests/test_agent_runas.py`, beside the three sweep lists' own:

- all thirteen keys come out and `oauth_token`, `user` and the `users:` subtree stay, with
  nothing left beside the file, and a steering key *inside* `users:` is kept, since gh does not
  read it from there;
- `git_protocol` comes out, with its own test naming the clone measurement;
- a document nested deep enough to make PyYAML recurse is declined rather than raising, at both
  the depth that fails the dump and the one that fails the load — the sweep never raises, and a
  `RecursionError` is not a `YAMLError`;
- every host entry is stripped, not just `github.com`;
- a file with nothing to strip is left byte for byte, same inode;
- the mode is carried across a rewrite;
- a plant locked behind `chmod 0000` still comes out;
- a symlink at `hosts.yml`, and at `.config/gh`, is unlinked and what it points at is untouched;
- a file that is not YAML, not a mapping, not UTF-8 or empty is left alone, and so is one over
  the cap and one that is not a regular file (a FIFO, which must not block the sweep);
- the round trip does not rewrite a value: `user: no` and an all-digit `oauth_token` starting
  with a zero come back as the text `gh` wrote, where a plain `safe_load` would make them
  `False` and an octal integer;
- the rewrite is declined when the file moved under it, driven at the seam;
- a home that never held the file is a no-op;
- and the list itself is pinned, disjoint from the credential keys and absent from
  `TOOL_CONFIG_SWEEP`.

The `docker` CI job proves it in the image, on the real uid split through `RunAs.sweep_home`:
the plant re-points `gh` before the sweep and does not after, `gh config get -h github.com`
reads nothing for `api_host` and `https` for `git_protocol`, and the credential is still in the
file. A second step reads the image's own `gh config --help` and fails when it advertises a key
`GH_HOSTS_STEERING_KEYS` does not name, then checks that `gh config set -h` routes each of them
into `hosts.yml` — with a value each key accepts and no `|| true`, since that is what made the
first attempt at this measurement wrong.
