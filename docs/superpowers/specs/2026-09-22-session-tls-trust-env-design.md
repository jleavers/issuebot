# A `.issuebot/env` line does not widen what the next session's TLS trusts

Date: 2026-09-22
Status: implemented
Issue: #205 (related to #190, #191, #179, #171, #151, #126, #121, #104)

## Problem

`.issuebot/env` is the workspace file a hook writes to hand variables to the session:
`workspace_environment` layers it over `agent_environment`'s allow-list for every turn, for
every hook, and for `gh repo clone` itself (`WorkspaceManager._run_argv` takes the same
environment). `PROTECTED_ENV_NAMES` and `PROTECTED_ENV_PREFIXES` are what it may not set: the
`claude` entries from the start, git's and `gh`'s since #171 (`TOOL_CONFIG_ENV_NAMES`,
`TOOL_CONFIG_ENV_PREFIXES`), the shell's own since #179 (`SHELL_ENV_NAMES`).

`SSL_CERT_FILE` and `SSL_CERT_DIR` are in none of them, and neither is reached by a prefix:
`ANTHROPIC_`, `CLAUDE_`, `GIT_` and `GH_` are the four, and these names belong to no tool's
namespace. A line naming either changes which certificate authorities the *next session on that
issue* accepts, on every tool that reads them.

It was found while measuring #190 and filed rather than folded in, for the reason #171 was filed
out of #151: that issue is about a file in the account's home, this is the environment spelling,
and protecting a name is a decision about what a hook may legitimately configure.

**Reach.** Neither name is in `PASSTHROUGH_NAMES`, so neither is inherited from the worker; the
only way either reaches a session is the workspace's own `.issuebot/env`. That file outlives the
session, but a workspace belongs to one issue and under a pool is bound to one account and
sealed `0700` between runs (#121, #75), so this is a channel from one session to the *next
session on the same issue* -- and, since the file is re-read for every turn, from a session to
its own later turns. Persistence inside the session's privilege domain, as in #101, #137, #151,
#171, #179 and #191, not an escalation across it (#75 closed that).

**Invariant.** A line in `.issuebot/env` cannot change which certificate authorities the next
session's `gh` will accept, nor take its trust store away.

## Measured

`gh version 2.100.0`, `git 2.47.3` (libcurl 8.14.1), `curl 8.14.1` (OpenSSL 3.5.7), CPython
3.14.7 with `pip` 26.2.1, and `uv` 0.12.11 on this image. The authority is minted by `openssl`
in a scratch directory and signs a certificate for `localhost`; the listener is a loopback
HTTPS server presenting it. `api.github.com` is reached through the deployment's own egress
proxy, exactly as a session reaches it.

**The reported behaviour reproduces, for both names.** Against the listener:

```text
$ gh api https://localhost:18205/user
Get "https://localhost:18205/user": tls: failed to verify certificate: x509: certificate signed by unknown authority
$ SSL_CERT_FILE=$R/ca.pem gh api https://localhost:18205/user
{"login": "forged", "path": "/user"}
$ SSL_CERT_DIR=$R/cadir  gh api https://localhost:18205/user
{"login": "forged", "path": "/user"}
```

`SSL_CERT_DIR` is measured here because the issue measured only `SSL_CERT_FILE`, and the two are
not one variable.

**And the route into the next session is open**, through the function that filters the file:

```text
$ uv run python -c '<workspace_environment over a .issuebot/env naming both>'
applied: ['SSL_CERT_FILE', 'SSL_CERT_DIR', 'DATABASE_URL']
SSL_CERT_FILE in next session's env: $R/ca.pem
SSL_CERT_DIR  in next session's env: $R/cadir
refused: []
```

### The pair can break TLS, not only widen it

The issue concludes that for `gh` the variable "only ever **adds** a trust anchor -- it cannot be
used to break TLS, only to widen it". That is true of one name at a time and false of the pair,
which matters because it decides what the residual is: a widening needs a second primitive to
pay off, and a breaking does not.

Go reads `SSL_CERT_FILE` *instead of* its default certificate **files** and `SSL_CERT_DIR`
*instead of* its default certificate **directories**, and the image has both a default file and
a default directory. So one name alone leaves the other half of the pair verifying GitHub, and
the two together leave nothing:

```text
$ SSL_CERT_FILE=$R/unrelated-ca.pem gh api user --jq .login
jleavers
$ SSL_CERT_DIR=$R/empty gh api user --jq .login
jleavers
$ SSL_CERT_FILE=$R/unrelated-ca.pem SSL_CERT_DIR=$R/empty gh api user --jq .login
Get "https://api.github.com/user": tls: failed to verify certificate: x509: certificate signed by unknown authority
```

End to end, through the real `workspace_environment`, one `.issuebot/env` does both at once --
the next session's `gh` trusts the planter's listener *and stops verifying GitHub*:

```text
--- planted CA: applied=['SSL_CERT_FILE', 'SSL_CERT_DIR', 'DATABASE_URL']
    gh api https://localhost:18205/user -> {"login": "forged", "path": "/user"}
    gh api user -> Get "https://api.github.com/user": tls: failed to verify certificate: ...
--- unreadable CA: applied=['SSL_CERT_FILE', 'SSL_CERT_DIR']
    gh api https://localhost:18205/user -> ... tls: failed to verify certificate: ...
    gh api user -> Get "https://api.github.com/user": tls: failed to verify certificate: ...
```

The second arm is two lines naming paths that do not exist, and it is the cheaper half of this:
no listener, no second primitive, no certificate. A value that will not load is not ignored.

```text
                        gh api user          curl https://api.github.com/     uv pip install
clean                   jleavers             200                              resolves
SSL_CERT_FILE missing   jleavers             curl: (77) bad certificate file  UnknownIssuer
SSL_CERT_DIR missing    jleavers             200                              UnknownIssuer
both missing            tls: failed ...      curl: (77) bad certificate file  UnknownIssuer
```

Three different failures, and only `gh` under one name survives. `uv` is the one that says so
(`warning: Invalid SSL_CERT_FILE. Path does not exist: ... No default certificates will be
trusted.`) -- and then trusts nothing, rather than falling back. `curl` fails before the
request. `gh` is the one that matters most: it is how a session reads its issue, pushes its
branch and opens its pull request, and how the *worker* clones for it, since `_run_argv` runs
`gh repo clone` under this same environment.

### `curl` does not "replace the bundle outright"

The issue's second measurement is `curl: (77) error setting certificate file:
/tmp/trusts-nothing.pem` where `gh` was unaffected, and reads the difference as curl replacing
the store where Go adds beside it. The asymmetry is an artefact of the file rather than of the
tool: exit 77 is `CURLE_SSL_CACERT_BADFILE`, what OpenSSL gives for a file it cannot parse --
an empty one, or one that is not there.

```text
$ SSL_CERT_FILE=$R/empty.pem curl -sS -o /dev/null https://api.github.com/
curl: (77) error setting certificate file: $R/empty.pem
$ SSL_CERT_FILE=$R/empty.pem gh api user --jq .login
jleavers
$ SSL_CERT_FILE=$R/unrelated-ca.pem curl -sS -o /dev/null -w '%{http_code}\n' https://api.github.com/
200
$ SSL_CERT_FILE=$R/unrelated-ca.pem SSL_CERT_DIR=$R/empty curl -sS -o /dev/null https://api.github.com/
curl: (60) SSL certificate problem: unable to get local issuer certificate
```

With a *valid* unrelated authority, curl behaves exactly as Go does: one name replaces its own
half, both replace the store. Go and OpenSSL agree here; where they differ is that Go tolerates
a file with no certificates in it and OpenSSL refuses to start the request at all.

### What each tool on this image does

| tool | `SSL_CERT_FILE` | `SSL_CERT_DIR` | one alone | both |
|---|---|---|---|---|
| `gh` 2.100.0 (Go `crypto/x509`) | read | read | replaces that half; the other still verifies GitHub | store replaced |
| `curl` 8.14.1 (OpenSSL 3.5.7) | read | read | as above | store replaced |
| CPython 3.14.7 `ssl`, `pip` 26.2.1 | read | read | as above | store replaced |
| `uv` 0.12.11 (rustls) | read | read | **store replaced on its own** | store replaced |
| `git` 2.47.3 (libcurl 8.14.1) | **not read** | **not read** | -- | -- |

`git` is the interesting negative: it links the same libcurl that reads these names, and it
still does not, because it sets `CURLOPT_CAINFO` explicitly from `http.sslCAInfo`, whose default
is compiled in -- the error names it. Its own spelling is `GIT_SSL_CAINFO` -> `http.sslCAInfo`,
already covered by the `GIT_` prefix (#171) and by the config sweep (#151).

```text
$ SSL_CERT_FILE=$R/ca.pem git ls-remote https://localhost:18205/x.git
fatal: unable to access '...': server verification failed: certificate signer not trusted. (CAfile: /etc/ssl/certs/ca-certificates.crt CRLfile: none)
$ GIT_SSL_CAINFO=$R/ca.pem git ls-remote https://localhost:18205/x.git    # no TLS complaint
```

`uv` is the other end: either name alone replaces its whole store, so a single line takes `uv`
off the network entirely.

```text
$ uv pip install --index-url https://api.github.com/simple/ x-nope       # resolves, TLS fine
$ SSL_CERT_DIR=$R/empty uv pip install --index-url https://api.github.com/simple/ x-nope
error: Failed to fetch: ...
  Caused by: invalid peer certificate: UnknownIssuer
```

### The per-tool spellings, which is what bounds the cost

Each was measured against the same listener with the planted authority: none of them reaches
`gh`, and each reaches the tool it is named for.

```text
$ CURL_CA_BUNDLE=$R/ca.pem      gh api https://localhost:18205/user  -> tls: failed to verify certificate
$ REQUESTS_CA_BUNDLE=$R/ca.pem  gh api https://localhost:18205/user  -> tls: failed to verify certificate
$ PIP_CERT=$R/ca.pem            gh api https://localhost:18205/user  -> tls: failed to verify certificate
$ UV_SYSTEM_CERTS=1             gh api https://localhost:18205/user  -> tls: failed to verify certificate
$ SSL_CERT_FILE=$R/ca.pem       gh api https://localhost:18205/user  -> {"login": "forged", ...}

$ CURL_CA_BUNDLE=$R/ca.pem curl -sS https://localhost:18205/user     -> {"login": "forged", ...}
$ PIP_CERT=$R/ca.pem pip download --index-url https://localhost:18205/simple/ ...        -> TLS accepted
$ REQUESTS_CA_BUNDLE=$R/ca.pem pip download --index-url https://localhost:18205/simple/  -> TLS accepted
$ uv pip install --cert $R/ca.pem --index-url https://localhost:18205/simple/ ...         -> TLS accepted
```

`UV_SYSTEM_CERTS=1` is the deployment route rather than a session one -- it reads the platform's
own store, which is root's:

```text
$ UV_SYSTEM_CERTS=1 uv pip install --index-url https://api.github.com/simple/ x-nope   # TLS fine
$ UV_SYSTEM_CERTS=1 uv pip install --index-url https://localhost:18205/simple/ x-nope
  Caused by: invalid peer certificate: UnknownIssuer
```

## Decision

**Protected: `SSL_CERT_FILE` and `SSL_CERT_DIR` join `TOOL_CONFIG_ENV_NAMES`.**

### Why protect

**It is the same class #171 drew the line around, and the same tool.** `.issuebot/env` may not
re-point the `git` or `gh` the next session on the issue runs. `gh` is the one tool in the
session holding `GH_TOKEN`, and this pair is the *only* spelling that reaches its trust store:
`gh` has no `GH_` name for it, so unlike every editor, pager and askpass rung on that list there
is no protected head above these -- they are head and tail of that chain at once.

**The breaking half needs no second primitive.** Widening trust pays off only with something
that redirects a request to a host the planting session controls, and the issue is right that
those are bounded: `gh`'s own `api_host` is swept by #190, `GH_HOST` and the proxy names are
protected, and #126's allow-list bounds which names are reachable at all. Taking the trust store
*away* is bounded by none of that. Two lines naming paths that do not exist leave the next
session with a `gh` that fails every request and a `curl` that fails every `https://`, and the
same environment is what the worker's own `gh repo clone` runs under. That is a durable,
cost-free denial of the next session on the issue, and it is the half the issue's "defence in
depth rather than a live hole" reading does not cover -- because the measurement it rests on was
one variable at a time.

**A list somebody has to keep complete is better closed than argued about per entry.** That is
the issue's own reason, and it is #171's: successive drafts of that list were incomplete, which
is why two of its four entries became prefixes. These two names are a finite, measured pair.

### The rule, stated so it is checkable

`TOOL_CONFIG_ENV_NAMES` carries two rules already -- the rungs of git's and `gh`'s documented
precedence chains that fall outside the `GIT_`/`GH_` prefixes (#171), and the XDG base
directories a tool issuebot launches resolves something executable or configuring through (#171,
#191). Neither reaches this pair, and the issue says why in as many words: `SSL_CERT_FILE` does
not name a command, which is what most of that list is about.

So a third rule, and the distinction the comment above the list did not draw:

> **A name is protected when it decides which certificate authorities a tool issuebot launches
> will accept.**

It is the one entry on the list that is neither a command nor a file naming one. What it decides
is *who the tool is talking to* rather than *what it runs* -- a trust decision, the other thing a
tool's configuration can carry, and the one the config sweep of #151 already takes out of the
home in its file spelling (`~/.gitconfig`'s `http.sslCAInfo`).

Finite for the same reason the chain rule is: which names a tool reads for its trust store is a
measurement, and the table above is it.

### Names and not a prefix, and which names

**Names.** There is no namespace here to take whole. `SSL_` is not a tool's prefix -- it is a
convention OpenSSL established and Go, rustls and CPython followed -- and taking it would catch
`SSL_CLIENT_CERT` (a client credential a hook may legitimately forward, the mutual-TLS analogue
of the `SSH_AUTH_SOCK` case #171 deliberately left open) along with whatever else a tool puts
there next. The same answer #191 gave for `XDG_`, for the same reason: a specification's
namespace rather than a tool's.

**These two and not the per-tool spellings.** `CURL_CA_BUNDLE`, `REQUESTS_CA_BUNDLE`, `PIP_CERT`
and `UV_SYSTEM_CERTS` stay settable, measured above as reaching the tool they name and not `gh`.
That is what makes this a bound on the tooling issuebot itself launches rather than on the
target repository's, which is the line #171 drew and the line `.issuebot/env` exists to serve.
It is also exactly #191's shape: protect the general root, keep the per-tool variables, which
are the better spelling anyway.

### What it costs, and who pays

A hook can no longer hand the session a general trust store. What it keeps, all measured above:

- **The per-tool spellings**, for the target repository's own tools: `CURL_CA_BUNDLE` for curl,
  `REQUESTS_CA_BUNDLE` or `PIP_CERT` for pip, `UV_SYSTEM_CERTS` for uv against the image's store.
- **Per-command**, in the hook's own shell, for a tool the hook itself runs, and `uv --cert` for
  a `uv` the session runs, since `uv` has no environment spelling of that flag.
- **Per-repository**, written into the clone, which every later turn sees because the clone
  persists.
- **The deployment's own answer**, which is the honest one for an authority every session needs:
  install it in the image's system trust store, root-owned, in an image built `FROM` this one
  (`/usr/local/share/ca-certificates/<name>.crt` and `update-ca-certificates`). `gh`, `git`,
  `curl` and `python` read it with no variable at all, and `uv` reads it with
  `UV_SYSTEM_CERTS=1`, which `.issuebot/env` may still set because it is a switch and not a
  path. That is #191's answer in the spelling TLS leaves -- the system file rather than the
  session's, outside the session's privilege domain -- and it is the only form of this that a
  session cannot take back, since `.issuebot/env` is a file the session can rewrite for its own
  next turn.

**Rejected: passing the names through from the worker.** Adding them to `PASSTHROUGH_NAMES`
would give a deployment the hatch in a form the session cannot rewrite, and is rejected for
#191's reason: the guarantee would then hold unless an operator set a variable, which is the
shape of fault this line of changes exists to end. The system trust store costs the deployment
nothing the variable would have bought.

As with every protected name, the refusal is visible only in the worker's log
(`workspace_env_ignored`, one line naming the key and never its value) and not to the hook that
wrote it, which is why `docs/toolchains.md` states the rule where a hook author is reading.

## Design

- **`TOOL_CONFIG_ENV_NAMES`** (`agent/runner.py`) gains `SSL_CERT_FILE` and `SSL_CERT_DIR`, with
  the comment carrying the third rule above and the measurement behind it -- including that the
  pair replaces the store rather than only widening it, which is the correction to the issue.
- **Nothing else changes.** `PROTECTED_ENV_NAMES` has exactly one reader,
  `merge_workspace_env`, so this bounds `.issuebot/env` and no other environment. The worker's
  own environment, the egress proxy and the image's trust store are all untouched.

## Residuals

- **Widening still needs a redirect to pay off.** Named because the decision does not rest on
  it: `gh`'s `api_host` (#190, open when this landed), an index URL a hook or a session may
  legitimately set (`PIP_INDEX_URL`, `UV_INDEX_URL`), or a `git remote` in the clone. A session
  that can choose the host can generally choose `http://` or an insecure-host flag as well, so
  the trust store is rarely the load-bearing part of such a chain -- which is why the case for
  closing this rests on the denial half and on the list being finite, rather than on a live
  hole.
- **`claude`'s own TLS.** Not measured, for the reason it could not be: the probes this session
  can run (`claude auth status --json`) are local, and provoking a networked one would have
  meant spending the deployment's credential on a measurement the decision does not turn on.
  Named as a gap rather than a finding. `claude`'s own namespace is covered (`ANTHROPIC_` and
  `CLAUDE_` are `PROTECTED_ENV_PREFIXES`) and its endpoint is not a session's to move; egress is
  CONNECT-only (#126), so the proxy sees no TLS to terminate.
- **`node` and `npm` were not measured**, the `ISSUEBOT_NODE_VERSION` arm not being built into
  the image this session runs in. Node's documented spelling is `NODE_EXTRA_CA_CERTS`, which is
  additive and stays settable; whether this image's node also reads `SSL_CERT_FILE` is a
  measurement for whoever needs it, and the answer would be one name's edit either way.
- **`SSL_CLIENT_CERT`** stays settable, by decision rather than oversight: a client certificate
  is a credential a hook forwards, the mutual-TLS analogue of `SSH_AUTH_SOCK`, and it does not
  decide who the tool will accept.
- **A denylist, as in #171, #179 and #191.** `.issuebot/env` admits everything it does not
  refuse and has to: its purpose is handing over what a target repository's tests need. This is
  a bound on the tooling issuebot itself launches, never a claim that the next session's
  environment is uninfluenced.

## Tests

`tests/test_agent_runner.py`:

- Both names join the parametrised refusal cases, annotated with what they do, and the pinned
  `TOOL_CONFIG_ENV_NAMES` list gains them, so dropping one is a deliberate edit in two places.
  The pinning test's docstring carries the third rule.
- `CURL_CA_BUNDLE`, `REQUESTS_CA_BUNDLE`, `PIP_CERT` and `UV_SYSTEM_CERTS` join the negative
  over-reach cases, so what the protection deliberately leaves open is pinned beside it.
- End to end through `workspace_environment` with a real `.issuebot/env`: a file carrying both
  names hands the next turn its `DATABASE_URL`, its `CURL_CA_BUNDLE` and its `UV_SYSTEM_CERTS`
  and neither TLS name, with one `workspace_env_ignored` line per key and never a path.
- And end to end against the real `gh`, shaped like #191's extension proof and #151's `git` one
  (skipped where `gh` or `openssl` is missing): an authority minted in a scratch directory signs
  a certificate for a loopback listener, and `gh api` accepts it under the mapping the file
  itself carries and refuses it under the environment `workspace_environment` built from that
  same file. Two-sided, so it fails if the protection is removed rather than only asserting a
  key is absent, and run for each name, since they are not one variable. Loopback only: no
  network, and the image's own trust store is neither read nor written.
