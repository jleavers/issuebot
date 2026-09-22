# Security policy

## Reporting a vulnerability

**Please report privately, through [GitHub's private vulnerability
reporting](https://github.com/jleavers/issuebot/security/advisories/new).** It is enabled on
this repository: the report becomes a draft advisory only you and the maintainers can see, and
it is the route to use rather than a public issue.

Please do not open a public issue for a vulnerability. Everything in the issue tracker is
readable by anyone, and this project's issues are read by an agent that acts on them.

Include what you would want if you were fixing it: the version or commit, what an attacker can
reach, and the smallest sequence that shows it. A proof of concept is welcome but not required
— a clear description of the mechanism is worth more than a working exploit.

There is no bounty, and no guaranteed response time: this is a personal project. Expect an
acknowledgement within a few days.

## What is in scope

issuebot runs a coding agent, unattended, on issues that anyone may have opened, with a GitHub
token in its environment. The interesting boundaries are:

- **The session's authority.** `claude -p`, every hook and the clone run as a session account
  that is not the worker's, cannot invoke `sudo`, and cannot read the worker's home, its code
  or the rest of its environment. A way for a session to reach any of those, or to become the
  worker or another session's account, is in scope.
- **The session's network.** Under Compose the worker's container has no route off the host
  except an allow-listing `CONNECT` proxy. A way to reach a host that is not on the allow list,
  or to send `GH_TOKEN` somewhere it should not go, is in scope.
- **Prompt injection that crosses into authority.** Issue and comment text is data, wrapped in
  a `<github-text>` envelope, and the envelope is a hint to the model rather than a boundary —
  so an agent persuaded to *misbehave within* its authority is expected and is not a
  vulnerability. An input that widens that authority — turning a tool back on, reaching an MCP
  server, changing which credential the next turn runs with, or planting configuration a later
  session loads — is in scope.
- **Secrets in output.** Tokens, keys, webhooks and the DSN's password are scrubbed before
  anything reaches the database, the dashboard, Slack or a log. A shape that gets through is in
  scope.
- **The dashboard.** Every page, JSON route and raw turn part requires the password;
  `/healthz` deliberately answers an anonymous probe with liveness alone. A read that reaches
  repository names, issue titles or transcripts without the credential is in scope.

## What is not

- The agent writing bad code, or a pull request that should not be merged. Human review of the
  pull request is a designed part of the loop, not a control that failed.
- Anything that needs the operator's own credentials, or an attacker who already has the host.
- The host route (`agent.run_as` unset, no container), where the session runs as your own user
  with none of the boundaries above. `validate` warns about it; it is a development
  convenience, and the container is the supported deployment.
- Denial of service against your own worker by opening many issues, which is bounded by
  `agent.max_concurrent_agents`, the budgets, and the fact that you choose what to label.

[`docs/superpowers/specs/`](docs/superpowers/specs/) has the design documents behind each of
those boundaries, which are the fastest way to see what was intended before deciding whether
something is a bug.
