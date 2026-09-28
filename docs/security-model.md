# How a session is bounded

Three things fix what one unattended session may do, and none of them is the prompt: the
network the container can reach, the account the session runs as, and the credential it
authenticates with — and, for the GitHub credential, whose it is. Each is reported by a line
of `issuebot validate`, and each is set outside anything an issue or the agent can write.

What counts as a vulnerability in each of them, and how to report one, is
[`SECURITY.md`](../SECURITY.md).

## What a session may reach

`validate` reports this as its `egress` line. It bounds what a session can do alongside the
token it holds and the tools it holds it with. Under Compose the worker's networks are all
`internal`, so the container that runs `claude -p`, every hook and the clone **has no route off
the host at all**; its one way out is the `egress` service, a forward proxy that speaks
`CONNECT` alone and answers it only for a host on an allow-list. Anyone can open an issue, and
a session reads what
they wrote; without this, a `curl` under `Bash` could send `GH_TOKEN` anywhere, or fetch the
next page of its own instructions from a host of its choosing.

The shipped list is what the workflow itself needs and nothing else:

| Host | Reached by |
|---|---|
| `api.anthropic.com` | `claude -p`, every turn |
| `platform.claude.com` | where `claude` exchanges and *refreshes* the OAuth credential it runs with: the `CLAUDE_CODE_OAUTH_TOKEN` a container session is handed, or the host route's own login |
| `claude.ai` | that same login's origin |
| `github.com` | `gh repo clone`, and every `git fetch`/`push` in a workspace |
| `api.github.com` | every `gh api`, `gh issue` and `gh pr` call, the worker's polls included |
| `objects.githubusercontent.com` | release assets and raw objects `gh` redirects to |
| `www.githubstatus.com` | the status page the worker reads to annotate a dispatch hold |
| `hooks.slack.com` | the worker's own notifications, if `SLACK_WEBHOOK_URL` is set |

A Slack-compatible webhook on another host is yours to add, and so is a `claude` that a future
release points at a host not listed here: a refused webhook is silent apart from a log line,
and a refused token refresh fails every session with a 403 about an allow-list rather than
about a credential.

**The target repository's toolchain is yours to add**, because it differs per deployment: the
registries a `uv sync`, `pip install`, `npm ci` or `go mod download` reaches, in a hook or in a
session's own shell. Set `ISSUEBOT_EGRESS_ALLOW` in this checkout's `.env` and
`docker compose up -d egress`:

```bash
# in this checkout's .env -- commas or spaces; extends the list above, never replaces it
ISSUEBOT_EGRESS_ALLOW=pypi.org,files.pythonhosted.org,registry.npmjs.org
```

An entry is a host name on port 443 (`pypi.org`), a host and a port (`registry.internal:8443`),
or a leading dot for a domain and everything under it (`.githubusercontent.com`). Two things to
know before you widen it:

- **Egress is HTTPS only.** The proxy speaks `CONNECT` and nothing else, so it never sees a URL,
  a header or a body, and never has to hold a certificate authority — but a plain `http://`
  registry cannot be reached from a session whatever the list says.
- **A name on the list is a name a session can post to.** The list is a reach, not a read: it
  bounds where the token can go as much as where the tools can fetch from. Add the registry,
  not the domain it happens to live under.

A refused request fails with `403` from the proxy, which names the host and this variable; the
proxy logs `egress_denied` with the host and port, which is the line to grep for when a hook
starts failing after an upgrade.

`validate` asks the proxy both of its questions — a name reserved by RFC 2606 must come back
`403`, and `api.github.com` must come back `200` — and then asks the *network* whether there is
a route round it at all. That last one is a warning rather than a failure, because a host
running without Compose is entitled to reach the internet; under Compose it means
`issuebot-internal` was created without `--internal`, and the line says so.

**On the host route** (`uv run issuebot worker`, no Compose) there is no proxy and no internal
network, so a session's egress is your machine's. `validate` warns — `[WARN] egress: no proxy
configured` — the way it warns that the session shares your uid. Setting `HTTPS_PROXY` in the
worker's environment points sessions at a proxy of your own; issuebot passes the six proxy
variables through to `claude`, every hook and the clone, and a hook's `.issuebot/env` cannot
overwrite them.

## One account per concurrent session

`agent.run_as` names the accounts a session runs as, and the image's default is the pool
its own build created: the account loop writes the names it made to
`/etc/issuebot/session-accounts`, and `agent.run_as` falls back to that list when neither
`WORKFLOW.md` nor `ISSUEBOT_AGENT_USER` names anything. `ISSUEBOT_AGENT_POOL_SIZE` in this
checkout's `.env` says how many, three by default and at build time: `docker compose build
worker` picks a change up, and `validate` warns when the number in `.env` and the accounts in
the image disagree.

The worker binds one member of the pool to each running slot, so each workspace directory
belongs to the worker and to its bound account's group alone (`1770`): a sibling session cannot
enter it, and a workspace keeps its account for as long as it exists, which is what lets a
rework session write the clone the first one made. Dispatch is capped by the pool as well as by
`agent.max_concurrent_agents`, and `validate` says so when the pool is smaller.

To name the accounts yourself — a subset of the built ones, or one account for the whole
deployment — set `ISSUEBOT_AGENT_USER=agent-1,agent-2,agent-3` in this checkout's `.env`, or
`agent: {run_as: [agent-1, agent-2, agent-3]}` in `WORKFLOW.md`, which wins over both. One
account for the deployment is one account for *every* concurrent session, so with
`agent.max_concurrent_agents` above 1 a session working one issue can write the workspace of a
session working another -- which is why `validate` warns about it. Anyone may open an issue, so
that is a boundary worth having.

Turning a pool on over a `/workspaces` volume that already holds clones needs nothing of you:
a workspace whose clone belongs to another account is re-cloned rather than handed to a
session git would refuse, and the removal runs as whichever account owns what is there. The
setting is re-checked on a reload, and on every poll while the hold lasts -- a pool named in
`WORKFLOW.md` with an account that does not exist, a missing group membership or no credential
holds dispatch and says so in `issuebot status` and on the dashboard, rather than failing every
session it claims for. A `useradd` lifts it on the next poll; a `usermod --append`, or a
credential added to `.env`, needs the worker restarted, because a process's supplementary
groups and environment are fixed when it starts -- and the message says so.

**A session account takes its credential from the environment.** Its home is one nobody logs
into, so there is no login in it for `claude` to read — and a pool shares none between its
accounts on purpose, since two accounts refreshing one OAuth credential is a race nobody has
established is safe (`docs/superpowers/specs/2026-09-14-session-account-pool-design.md`). So
run `claude setup-token` on a machine with a browser and put the value in this checkout's
`.env` as `CLAUDE_CODE_OAUTH_TOKEN`, or set `ANTHROPIC_API_KEY` there instead. The worker
refuses to start without one rather than claim issues every session would fail to
authenticate, and `validate` says the same.

## The text a session acts on

A session is handed the issue's title and description and told to run the steps any
`Validation` or `Test Plan` section asks for. What makes that safe to do unattended is that
a human read that text and applied `issuebot/todo` to it -- so the label has to approve the
text *as it stood*, and nothing an author writes afterwards may ride on it. Before every
dispatch the worker reads GitHub's own record: the latest `issuebot/todo` a person applied
is the approval, and any title or body edit at or after it by an account other than the
approver un-approves the issue, which loses its label and gets a `### Issuebot unapproved
edit` block saying who changed what and when ([`docs/operations.md`,
"Blocked"](operations.md#blocked)). Only `issuebot/todo` approves: `issuebot/rework` asks
for changes to the pull request and never re-approves the issue's text -- the conflict
bounce applies it itself -- so an edit made during review is refused on the rework that
follows, and a label an app or a bot applies is never an approval. The worker's own
`issuebot/todo` counts only when its account administers the repository, which is to say
when it is the maintainer's own account -- a setup `validate`'s `github.token account` line
warns about ([The account a session acts as](#the-account-a-session-acts-as)); under a
dedicated GitHub account only a maintainer's `issuebot/todo` approves, so the worker's own
relabel can never carry an edit past the check. Under an admin token the account's own label
counts on any issue, so a session holding the token can approve another issue for its
successor. Nothing is stored, so a restart cannot reset it: re-approval is a maintainer
applying `issuebot/todo` again.

Comments are the other text a session reads, and on a public repository anyone can leave one
on an issue in `issuebot/review` or `issuebot/rework`. The barrier is in the commands the
workflow hands out -- every fetch is a `gh api --jq` that selects `OWNER`, `MEMBER` or
`COLLABORATOR`, so what those commands drop never enters the context. That is not the whole of
what keeps a session inside it, though: a session holds a shell and `GH_TOKEN`, and nothing
stops it running `gh issue view --comments` itself, or following a quarantined comment's URL;
Ground rule 7 ("fetch comments only with them") and the model's obedience to it are what cover
that, exactly as they cover every other rule in the prompt. A maintainer's quote-reply carries
the quarantined author's own text into the context by design, too -- quoting is how a
maintainer shows what they are answering -- and only the rule that follows, that such text
"is still its original author's", stands between it and the session.

`author_association` is GitHub's own word for the author's *relationship* to the repository,
not a permission check: `OWNER` is the repository's owner; `MEMBER` is a member of the owning
organisation, whether or not they hold any permission on this particular repository;
`COLLABORATOR` is anyone invited to the repository, at read level and up. So on an
organisation-owned repository the filter admits every member of that organisation; a
deployment that wants narrower admission narrows the organisation, not the filter -- the three
are fixed by design (`docs/superpowers/specs/2026-09-28-tracker-text-admission-design.md`,
§2). The account issuebot runs as is at least a collaborator -- the owner, on a maintainer's
own token -- so its own comments pass: a session persuading its successor holds the same
authority, not more, which is the line #77 drew for the workpad.

What was dropped is listed by author and URL under `Quarantined` in the workpad, and a
maintainer adopts one of those requests by replying to it. A reply adopts only what its own
words ask, though: the session never reads the parent of a reply it fetches -- the
review-comment projection carries no `in_reply_to_id` -- so a maintainer who wants a
quarantined request acted on has to restate it themselves in their reply; a bare "+1" on a
quarantined comment adopts nothing, and the workpad is issuebot's own state, not a request the
next sweep might act on. The issue's title, body and author envelopes carry the same attribute
(`association="NONE"`, say), `unknown` when GitHub gives none, so the session can see that the
description was admitted by the label and not by its author.

None of this travels with an overlay whose body replaces the prompt (the README's Local
overrides passage): what it keeps is whatever Step 6 its own prompt had, so a replaced prompt
must carry Step 6's four filtered commands and Ground rule 7 itself, or it has no barrier
against GHSA-jm8h-q3j6-p8xp at all.

## The account a session acts as

Everything above bounds where a session can reach and what it is handed. It does not bound
what a session does with `GH_TOKEN` at `api.github.com`, which the workflow needs and the
egress allow-list therefore admits: a session holds whatever that token's account may do to
the repository. If that account is yours, a persuaded session can merge its own pull request
past the review rule, or push to the default branch, with your authority
(GHSA-jm8h-q3j6-p8xp). So the identity a session acts as must be one that cannot merge what
it wrote -- and `validate` reads two things about yours, both from the repository's
*rulesets* and not from classic branch protection, which is readable by an admin alone and
so cannot be what a check for the account it recommends relies on: `github.token account`
warns when the token's account administers the repository, since an admin can bypass or
rewrite the rule below; `github.branch rules` warns when no ruleset requires an approving
review of the latest push on the default branch, since a review that does not cover the
latest push survives a later one -- a session can push to its own already-approved pull
request and merge it, which is what issuebot's own conflict bounce does. Both are warnings,
because `run-once` against a personal scratch repository is a legitimate use. Rulesets are
also a paid feature on a private repository -- GitHub Free reads them on a public repository
only, Pro, Team and Enterprise on a private one too -- and an organisation ruleset needs a
paid organisation plan the same way.

**A solo operator**, which is who this repository expects. Create a second personal account
for issuebot and add it as a collaborator with **write** -- never admin. Give it a classic
`repo` token rather than a fine-grained one: a fine-grained token can only reach
repositories owned by its own resource owner, and cannot be used to contribute to a
repository where the account is an outside or repository collaborator rather than the owner
or an organisation member, so a collaborator account has no fine-grained token to give it.
`validate`'s `github.token` line will warn about the classic token's account-wide reach, and
that warning means less here than it does on a maintainer's own token: this account's whole
reach *is* the one repository it collaborates on, which is the point of a dedicated account,
not a gap in it. (The organisation recipe below keeps a fine-grained token, if that matters
to you.)

Put a ruleset on the default branch requiring one approving review of the latest push --
"Require approval of the most recent reviewable push" in the UI
(`require_last_push_approval` in the API), turned on beside the review count itself, or an
approval survives a push made after it and a session can push to its own already-approved
pull request and merge it, which is exactly what issuebot's own conflict bounce does. Add no
bypass for the bot's account; you approve the bot's pull requests from your own. Your own
pull requests need an approver too, and GitHub does not let an author approve their own, so
add *yourself*, never the bot, as a bypass actor for the `Repository admin` role -- a role,
not a named account, so anyone who holds Admin on the repository gets the bypass, which is
exactly why the bot must never be admin -- in `pull_request` mode ("For pull requests only"
in the UI), not `always` ("Always allow" in the UI): your pull requests merge without a
second account. A direct push to the branch is refused by the `pull_request` rule itself,
which requires a pull request rather than a push; add the deletion and non-fast-forward
rules too and keep them on, since those are what stop the branch being deleted or
force-pushed instead -- the UI ticks both by default when you create a ruleset, the API does
not. The session's identity cannot merge what it wrote and cannot change the rule that says
so.

**An organisation** has the same shape with its own tools, and is the route that keeps a
fine-grained token: make the dedicated account a *member* of the organisation rather than an
outside collaborator, since a fine-grained token can be scoped to an organisation its own
account belongs to but not to a repository it only collaborates on from outside -- an
outside collaborator is back to the classic token the solo recipe above uses. The dedicated
account is a machine user -- GitHub's name for a personal account an organisation creates
for automation -- with **write** on the repository, no seat on any team that carries admin
or maintain, no organisation ownership, and no organisation-wide Admin base permission
either: `validate`'s `github.token account` line reads the token's *effective* permission on
the repository, so it catches all three the same way, belt and braces. The ruleset is an
*organisation* ruleset targeting the repository's default branch rather than a repository
one, with the same "Require approval of the most recent reviewable push" turned on for the
same reason as the solo recipe: a repository admin cannot remove an organisation ruleset
either, so the guarantee holds against the repository's own admins too, and it covers every
repository the organisation points a deployment at. Its bypass actors are teams of humans,
never the machine user, and its approvers are whoever reviews there already, so no admin
bypass is needed. `CODEOWNERS` with "require review from Code Owners" narrows who can
approve a session's change to a path; that is the organisation's choice. A GitHub App
installation token is not the recommended credential, though `gh` accepts one: it expires
after an hour, and a session can run longer than that.

Whichever recipe you use, also leave Settings → Actions → General → Workflow permissions →
"Allow GitHub Actions to create and approve pull requests" off: turned on, a workflow can
approve a pull request as `github-actions[bot]`, a second route past the review rule that
has nothing to do with which account issuebot runs as -- and if the session holds Workflows
write (README's Prerequisites), that workflow is the session's own to write. Leave the
setting off, or make sure nothing a session's own workflow edit can trigger uses that
approval.

## Checking that the credential took

`validate`'s `claude auth` line is the answer: it asks `claude` which credential it would
use, under the same trimmed environment the agent gets, so it reports what the *agent* will
authenticate with rather than what your shell can reach. It names the credential, so you can
tell them apart at a glance:

| Line | Route | What it means |
|---|---|---|
| `logged in (CLAUDE_CODE_OAUTH_TOKEN)` | container | the token from `claude setup-token` |
| `logged in (API key from ANTHROPIC_API_KEY)` | container | an Anthropic API key |
| `logged in (claude.ai, max)` | host, development | the login in your own `~/.claude` |
| `not logged in` | either | nothing usable — a `[FAIL]`, because the agent cannot run |

Setting both a login and `ANTHROPIC_API_KEY` is a warning rather than an error: it works, but
which credential gets billed is not obvious from the outside, so unset one. An empty
`ANTHROPIC_API_KEY=` counts as unset, which is what you want when `CLAUDE_CODE_OAUTH_TOKEN`
carries the credential.

The worker runs the same probe at startup, so a worker with no usable credential prints
`[FAIL] startup: claude auth: not logged in; ...` and exits rather than claiming issues it
cannot work on. The line names the container's credential first: `not logged in; set
CLAUDE_CODE_OAUTH_TOKEN (claude setup-token), or run claude auth login on the host`. Under
Compose that means `docker compose ps` shows the worker restarting until the credential is in
place; `docker compose logs worker` has the line. Only a definite "not logged in" stops it: a
`claude` that does not answer in time is logged as a warning and the worker starts anyway.

A session that hits a true external blocker (a credential it does not have, a tool it cannot
install, a service it cannot reach) writes the brief to the workpad and puts `BLOCKED: <one
line>` at the top of its final message. The worker moves the issue to `issuebot/review` at the
end of that turn with that line in the workpad block, rather than after `agent.max_turns`
re-checks of the same blocker; the turn budget stays as the fallback for a session that stops
without saying why.

A credential that stops working *after* startup — an expired `CLAUDE_CODE_OAUTH_TOKEN`, a
revoked API key — is caught by the run that hits it. That issue is moved to `issuebot/review`
at once with a workpad block naming authentication, rather than after `agent.max_attempts`
opaque failures, and the worker stops claiming anything else. A worker holding dispatch says
so wherever you look: `issuebot status` prints a `dispatch: held (auth) since ...` line, the
dashboard's worker line reads `worker held` with the reason, `/healthz` reports that
repository's entry in `workers` with `"status": "held"` and the same reason under its
`dispatch_hold`, and `docker compose logs worker` shows `dispatch_auth_held`. The board keeps
updating while the hold lasts, since the hold stops `claude`, not `gh`. It re-checks the
credential every poll and
picks up where it left off once `claude auth status` reports a login again, so fixing the
credential is enough and no restart is needed. A `claude` that cannot answer the probe at all
holds it up for ten polls at most, and then the worker goes back to failing one issue at a
time rather than sitting idle for good.

To ask `claude` directly, without going through issuebot — as `agent`, which every image has
whatever pool it built, and whose answer is the answer for `agent-1` too, since the credential
is the environment's and not any account's file:

```bash
docker compose run --rm --user agent --entrypoint claude worker auth status
```

It prints JSON — `"loggedIn": true` with an `authMethod` of `claude.ai`, `oauth_token` or
`api_key` — and `--text` gives a human-readable line instead. Note that it always exits 0, so
read the field rather than the exit code. The `email` and `orgName` fields come back null in
the container even when the credential is good: that metadata lives in
`/home/agent/.claude.json`, which is recreated with each container. The credential itself is
in no file at all — it is the variable from `.env`, handed to the session in its environment —
so nothing in the image holds it, and a token that eventually lapses is re-minted with `claude
setup-token` and put back in `.env` rather than refreshed in place. The worker's auth hold,
above, is the reminder.

That file is also where `claude` keeps `mcpServers`, and it outlives every session in the
container, so issuebot runs every turn with `--strict-mcp-config`: only servers named
on the command line are loaded, and the command line names what `claude.mcp_config` in the
front matter lists, nothing by default. No MCP server in a session account's `~/.claude.json`,
and no `.mcp.json` in a repository issuebot clones, reaches a session — including one an earlier
session wrote there. The rest of the file is still read: `claude` keeps its account metadata,
its trust state and a `projects` map in it. Adding an MCP server for the agent is therefore
not a matter of `claude mcp add` inside the container; it is a `claude.mcp_config`
entry, a setting the session cannot write.
