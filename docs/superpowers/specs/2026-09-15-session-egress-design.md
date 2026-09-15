# The session's network egress is an allow-list, not the container's routing table

Date: 2026-09-15
Status: implemented
Issue: #126 (split out of #109)

## Problem

#109 named three properties of a session's authority -- its tool set, the reach of its
credentials, and its network egress -- and fixed two of them. `claude.disallowed_tools` ships
`WebFetch` and `WebSearch` denied, `--strict-mcp-config` is unconditional, and `validate` warns
about a token whose reach is the whole account's. The third was left where it was, because a
compose network cannot filter by name: the session's egress was the container's, and the
container's was the host's.

So the model's own network tools were denied and a `curl` under `Bash` was not. A session reads
an issue anybody can open, and it holds `GH_TOKEN` and, under a pool, a Claude credential. It
could send either to a host of its choosing, or fetch the next page of its own instructions
from one -- which is the failure mode the `<github-text>` envelope is explicitly *not* the
boundary for (#109). Every other confinement in the tree -- the uid split (#75), the workspace
seal (#121), the home sweep (#101), the MCP flag (#119) -- is about what one session can reach
*inside* the deployment. Nothing said anything about what it could reach outside it.

## Invariant

> A session can open a network connection to the hosts the deployment named and to no others,
> and that is a property of the network it is on rather than of a setting it could rewrite or a
> rule it is asked to observe.

Two clauses, and the second is what makes the first worth stating. An allow-list a session can
route around is documentation.

## Design

Two halves, in two layers, and neither is sufficient alone.

### The network: the worker has no route off the host

Under compose the `worker` service joins `internal` networks only:

- `issuebot-internal`, shared across checkouts and created `docker network create --internal
  issuebot-internal`, carries the hub's database.
- `egress`, this project's own and declared `internal: true`, carries the proxy.

Docker gives a container whose every network is internal no default route, so there is nothing
to filter: the proxy is not a policy the session is asked to observe, it is the only way out.

A *second* shared network rather than making `issuebot` internal, which was the sketch in the
issue. `issuebot` is where `db` and `web` publish their host ports, and whether a published port
survives its network being internal is a Docker implementation detail this project would then
depend on. Adding a network changes nothing for either service: `db` joins both and publishes on
`issuebot` exactly as before, `web` is untouched. The cost is one more `docker network create`
at install and one more at upgrade, which the README states in both places.

`egress` is project-local and not shared for a reason that only shows up with more than one
checkout: compose aliases a service by its name on every network it joins, so two projects'
proxies on one shared network would both answer to `egress` and each worker would round-robin
between them.

### The proxy: `CONNECT` and an allow-list

`issuebot.egress`, served by `issuebot egress`, built from the same image and the same package
as the worker -- so there is no third-party proxy to pin, patch or configure, and the code that
decides is the code the tests exercise.

It speaks exactly one method. `CONNECT api.github.com:443` carries the host name in the request
line, so the filter reads a name and then relays bytes it never looks at: nothing here holds a
certificate authority, and a filter that cannot read the traffic cannot be blamed for what it
failed to notice in it. The cost is deliberate and stated: plain `http://` is answered `405`
rather than forwarded, so egress is HTTPS only.

The list is the operator's, over a default that is every host issuebot's *own* tools reach and
no other. The test of what belongs in the default is not "is it useful" but "would an operator
have had to discover it": `platform.claude.com` is where `claude` exchanges and refreshes the
OAuth credential it runs with -- the `CLAUDE_CODE_OAUTH_TOKEN` a container session is handed,
or the host route's own login -- so a list without it works until an access token expires and then
fails every session, blaming an allow-list for a credential; `hooks.slack.com` is worse, since
`urllib_post` never raises and a deployment would lose every notification with only a log line
to say so. The *target* repository's registries are the other side of that test: they differ per
deployment, and a default that carried `pypi.org` would be a default nobody had chosen.
`ISSUEBOT_EGRESS_ALLOW` in `.env` extends it, never replaces it. No telemetry host is in it --
the shipped `claude` names none, and one it named would be a name a session could post to.

Parsing is total. An entry that is not a host name costs its own entry and a `WARNING`, and the
proxy serves the rest: refusing to start would be a worker with no egress at all, which fails
every session rather than the one request the typo was about.

The service runs as `egress` (uid 1003, `nologin`, in neither group the sudo binary nor the sudo
rule names), on the reasoning that gave the dashboard its own account (#102) and one reason
sharper: it is the one process in the deployment with a leg on the open network.

### The seam: `agent_environment`

`PROXY_ENV_NAMES` joins `PASSTHROUGH_NAMES`, so the address reaches `claude -p`, every hook and
the clone; `claude`, `gh`, `git`, `uv`, `pip`, `npm` and `curl` all honour it. Both cases of all
three variables, because they are not interchangeable -- curl deliberately ignores an upper-case
`HTTP_PROXY`, since a CGI script's environment carries the request's `Proxy:` header under that
name, while other clients read only the upper-case spelling.

Passed through rather than fixed in the code: the address is the deployment's, compose sets it,
and the host route has none, where the *absence* is what `validate` warns about rather than
something the allow-list could supply.

They are also in `PROTECTED_ENV_NAMES`, so a `.issuebot/env` line cannot take them out from
under a running turn -- for the reason `PATH` is there and no stronger one. What bounds egress
is the missing route, not the variable. A hook that emptied them would take `gh`, `git` and the
next turn's `claude` off the network without admitting anything off the list.

## Evidence

Three levels, because the property spans a module, a compose file and a kernel routing table.

- **Hermetic** (`tests/test_egress.py`): the allow-list's grammar, and the proxy itself against
  loopback sockets the test started -- a tunnel byte for byte, a refusal that names the host and
  the variable, a plain `GET` refused, a request head that never ends, an allowed name that
  cannot be reached.
- **Shape** (`tests/test_image_layout.py`): the worker joins internal networks alone, the proxy
  is the only service with a leg on each side, `db` keeps `issuebot`, the proxy runs as its own
  account in no privileged group, the worker carries all six variables.
- **Live** (the CI `docker` job): the `hub,worker` profile brought up as an operator would, and
  a real session account asked both questions -- `https://example.com` fails through the proxy
  *and* fails with `--noproxy '*'`, while `gh api` and `https://api.github.com` succeed and the
  hub's database answers on the internal network. The dashboard's published port is asserted in
  the same step, since `db`'s second network is this change's doing.

And in a live deployment, `validate`'s `egress` check: the proxy's contract asked through the
same `probe_proxy` the compose healthcheck uses, and then `reachable_directly`, which is the
question the proxy cannot answer. A route round it is a warning rather than a failure -- under
compose it means the shared network was created without `--internal` and the line says so, but a
host running without compose, behind an operator's own proxy, is entitled to reach the internet
and should not be told it has a fault.

## What this does not do

- **It does not bound the *worker*'s egress separately from the session's.** Both are in one
  container and both go through the proxy, so the default has to carry the worker's own hosts
  (`hooks.slack.com`, `www.githubstatus.com`) as well as the session's.
- **It does not filter inside a connection.** The proxy sees a host name. Where a session may
  reach it may also post, which the README says in as many words: the list is a reach, not a
  read.
- **It does not confine the host route.** No compose, no internal network, no proxy, and
  `validate` says so -- as it says that the session shares the operator's uid.
- **It does not bound `web`'s egress.** The dashboard takes HTTP from a browser and runs no
  session; it stays on `issuebot`, unchanged.
