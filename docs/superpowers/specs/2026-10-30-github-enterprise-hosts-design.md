# GitHub Enterprise hosts: a configurable `github.host`, built through GHE.com

Date: 2026-10-30
Status: draft, awaiting review
Issues: #299 (this spec, the operator step of #280); #300 plans from it.

## Why, and why now

issuebot assumes its repository is on github.com (#280 lists every place). The maintainer has
now been given an account on a company enterprise hosted on **GHE.com** -- Enterprise Cloud with
data residency, `SUBDOMAIN.ghe.com`, which is always Enterprise Managed Users (EMU) -- and will
move some of the projects that issuebot works on today from a personal github.com account into
an organisation there over the coming weeks. The tenant's setup is not finished yet, but will be
soon. That is the first real use for anything in #280, and it decides the weighting here: GHE.com
has to work, end to end, on a real tenant; the rest of #280 is designed so that it fits, and only
built where GHE.com needs it.

There is no coordinated corporate migration. Each user moves their own repositories, and the
maintainer will do it with a plain `git push` into a new repository in the organisation: the
code moves, the issues stay behind on github.com, and the new repository numbers its issues and
pull requests from #1. The hub will run the github.com worker and the GHE.com worker for the same
project side by side for about 30 days while the old one's open work drains, and the hub's other
deployments stay on github.com indefinitely.

**This repository is public.** The company, its subdomain, its organisation and its region are
never named in a commit, an issue, a pull request, a test or a doc: they are `SUBDOMAIN.ghe.com`,
`ORG` and `REGION` everywhere, including in this file.

## Decisions

Taken by the maintainer in the brainstorm for #299:

1. **GHE.com is built and proven; GHES is specified and not built.** #280's four steps keep their
   order. Steps 0 (the host setting), 1 (enterprise accounts) and 2 (GHE.com) are built. Step 3
   (Enterprise Server) is designed in this document so that nothing in steps 0--2 has to be undone
   for it, and is not built: no issue asks for it, and the maintainer files one by hand only if
   an appliance becomes available to prove it against. #300's plan stops after step 2.
2. **No history carries over a move.** A project that moves starts afresh under its new key. The
   old repository stays in the dashboard's picker, with its runs, transcripts and links to
   github.com, until the maintainer is done with it (about 30 days). Merging the two was ruled out
   because a plain push renumbers issues: the old closed #5 and a new, unrelated #5 would share a
   row.
3. **A GHE.com worker can still reach github.com.** Its egress allow-list is today's default plus
   the tenant's names, not the tenant's names instead, so a session on company work can still
   fetch public dependencies from github.com.
4. **Both account arrangements are supported**: a dedicated managed account for the worker, which
   the maintainer will ask the company's IT for, and the maintainer's own managed account's token
   if that request does not succeed.
5. **The Actions-minutes tile follows the repository's host.** A tenant repository's page never
   shows the personal account's minutes. It shows the company organisation's minutes when an
   account is configured for that host and the token can read them, and no minutes window at all
   otherwise.
6. **Colleagues in the organisation steer issuebot as the maintainer does.** The organisation
   will have other members, and the maintainer wants them to be able to comment on issues and
   approve them with `issuebot/todo` just as the maintainer can: moving into a company
   organisation is the chance for issuebot to reach a wider audience. So the trust rules stay
   exactly as they are, and no narrowing setting is built.

## What was established

From a spike against the real tenant on 2026-10-30, with the maintainer's own `gh` login:

- **`/meta` needs a token on GHE.com.** `curl https://api.SUBDOMAIN.ghe.com/meta` answers 404
  (a real GitHub API error whose `documentation_url` names the meta endpoint), while
  `gh api /meta --hostname SUBDOMAIN.ghe.com` answers. So the API host is `api.SUBDOMAIN.ghe.com`,
  and anything that reads the tenant's `/meta` must hold a credential.
- **`/meta`'s `domains`** has the keys `actions`, `actions_inbound` (`full_domains`,
  `wildcard_domains`), `artifact_attestations` and `storage`. The tenant-specific names in
  `actions_inbound.full_domains` include `SUBDOMAIN.ghe.com`, `api.`, `codeload.`,
  `objects-origin.`, `github-raw.`, `results-receiver.actions.`, `broker.actions.`,
  `launch-receiver.actions.`, `pkg.actions.` and the package registries (`npm.`, `pypi.`,
  `containers.` and so on), each under `SUBDOMAIN.ghe.com`.
- **The Actions results storage is regional and few.** Four accounts,
  `prodREGION01resultssa0` to `prodREGION01resultssa3.blob.core.windows.net`, appear in both
  `actions_inbound.full_domains` and `storage`. github.com's equivalent is twenty
  `productionresultssa` accounts (`egress.ACTIONS_RESULTS_HOSTS`). The names carry the region, so
  they cannot be derived from the host name.
- **GitHub's own GHE.com network page lists wildcards only** (`*.SUBDOMAIN.ghe.com`,
  `*.actions.SUBDOMAIN.ghe.com`, `*.githubusercontent.com`, `*.blob.core.windows.net`) and points
  at `gh api /meta --hostname SUBDOMAIN.ghe.com --jq '.domains.storage[]'` for the storage list.
  This design uses none of the wildcards.
- **The maintainer is `admin` of the target organisation** (`user/memberships/orgs`).
- **The organisation billing summary answered 404.**
  `GET /organizations/ORG/settings/billing/usage/summary?product=actions` returned a 404 whose
  `documentation_url` names that endpoint, so the route exists. That settles nothing: the
  organisation is new and its Actions billing may not be live, and `gh`'s login token carries no
  billing scope. The actions-minutes spec recorded that a token without the accepted scope gets a
  404, not a 403 (`2026-10-02-actions-minutes-design.md`, "What GitHub provides").
- From #280: the EU region's status page, `eu.githubstatus.com`, served no Statuspage
  `api/v2/summary.json` when checked. The tenant is in the EU region.

From the code, as of `main` at 13f1dac:

- **Every `gh` call names the repository as `-R owner/name` or `repos/owner/name`**
  (`github/ghcli.py`), so `GH_HOST` alone re-points all of them, and `gh repo clone` with them.
- **The repository key is `owner/name` text** in every table (`repos`, `issues`, `runs`,
  `run_turns`, `events`, `runtime_snapshot`), in the routes (`/r/{owner}/{name}/`,
  `/api/v1/repos/{owner}/{name}`), in the NOTIFY payload check (`db/listen.py`'s `REPO_PAYLOAD`)
  and in the refresh channel name, which is a digest of the key and so takes any text.
- **The dashboard already links to the stored issue URL** (`issue.html`, through `safe_href`,
  which accepts any `https://` URL). Only the Slack link (`notifications/messages.py`'s
  `issue_link`) builds `https://github.com/{repo}/issues/{n}`, and `IssueEvent` carries
  `pr_url` but no issue URL.
- **The session boundary is already host-agnostic.** `GH_` is a `TOOL_CONFIG_ENV_PREFIXES` entry
  and so a `PROTECTED_ENV_PREFIXES` one (`agent/runner.py`), so `.issuebot/env` can set neither
  `GH_HOST` nor `GH_ENTERPRISE_TOKEN`. `runas._sweep_gh_hosts` strips the steering keys under
  every host's block in `hosts.yml` (`{host: _stripped_host(entry) for host, entry in ...}`),
  not only `github.com`'s. Both need a pinning test, not a change.
- **The billing poller runs in the hub's `web` process**, which reads no workflow, is configured
  by `ISSUEBOT_GITHUB_BILLING_TOKEN` and `ISSUEBOT_ACTIONS_INCLUDED_MINUTES`, and sits on the
  `issuebot` network rather than behind the egress proxy. `actions_minutes` is keyed by the
  account's login alone.

## Goals, non-goals, anti-goals

Goals:

- A worker with `github.host: SUBDOMAIN.ghe.com` claims, works and completes an issue in a
  tenant repository, end to end, under either account arrangement.
- Its egress allow-list names the tenant's hosts exactly, alongside today's default.
- A github.com repository and a tenant repository coexist on one hub without colliding.
- `validate` explains the refusals an enterprise produces, rather than reporting a bare 403.

Non-goals:

- Carrying history across a move (decision 2).
- Several hosts per worker. One worker targets one host; the hub already runs one worker per
  repository.
- Building GHES (decision 1).
- GitHub App installation tokens. #280 already sets them aside as a separate issue.
- Removing a repository from the dashboard's picker. Nothing can do that today, for github.com
  either. It is a follow-up of its own (see "Follow-ups"); until then it is a hand-written
  `DELETE`.

Anti-goals, which would be failures even if everything else worked:

- **A github.com deployment that changes in any observable way.** That covers the allow-list,
  the environment `gh` and the session get, the credential helper, Slack links, dashboard URLs,
  the stored keys and `validate`'s output.
- **A wildcard on the allow-list.** `gh` sends `GH_TOKEN` to github.com and to any `ghe.com`
  subdomain, so `*.ghe.com`, or anything that admits a tenant other than the configured one,
  turns the allow-list into a list of places a session could hand the token to. The same goes
  for `.blob.core.windows.net`, which is every Azure storage account anyone can create.
- **The company's name anywhere in the repository.**

## Design

### Step 0: the host as a setting

**The setting.** `github.host`, a DNS name, defaulting to `github.com`. It is validated, not
just typed, because it is interpolated into a shell script (the credential helper below) and
into URLs:

- lower-case letters, digits, hyphens and dots only, with no scheme, port, path or trailing dot;
- `api.github.com` and `www.github.com` refused, with a message saying the setting is the web
  host (an easy mistake to make);
- `ghe.com` itself refused, and a `ghe.com` name accepted only exactly one label deep
  (`SUBDOMAIN.ghe.com`, not `a.b.ghe.com`);
- **any other host refused**, with a message saying that GHES is not supported. A GHES host
  that half-works is worse than one refused at load. *(My call.)*

`github.repo` stays `owner/name`.

**One module derives everything from it** (`issuebot.github.hosts`; the name is the builder's).
It holds a small value for the configured host with:

- its kind: `dotcom`, `ghe_com` or, once step 3 is built, `ghes`;
- the web and git host;
- the API base: `https://api.github.com`, `https://api.SUBDOMAIN.ghe.com`, or
  `https://HOST/api/v3` for GHES;
- the `GH_HOST` value, and which variable carries the token: `GH_TOKEN` for `dotcom` and
  `ghe_com`, `GH_ENTERPRISE_TOKEN` for `ghes`;
- the egress names (below);
- the Statuspage summary URL, or `None` (below).

Nothing else in `src/issuebot` names a GitHub host.

**A test pins that.** It scans `src/issuebot/**/*.py`, and the prompt files under
`src/issuebot/config/` and `configs/`, for `github.com`, `githubusercontent.com` and
`githubstatus.com`, and fails on any occurrence outside the hosts module. There are a few
justified exceptions: the fixture URLs `cli.py` uses for `--show-prompt` and `FakeGitHub`'s
URLs, which should be built from the hosts module anyway, and comments explaining behaviour. The
test carries an explicit allow-list of `(file, line text)` pairs rather than a pattern, so a new
literal fails until someone has looked at it. The `Dockerfile`'s download URLs are build-time
and out of scope.

**The environment `gh` and the session get.** `GhRunner.child_environment` and
`agent_environment` put the token in the host's token variable. They set `GH_HOST` **only when
the host is not github.com**, so a deployment with no `github.host` produces exactly today's
environment. *(My call: #280's acceptance asks for the same environment, and an extra
`GH_HOST=github.com` would be a difference without a purpose.)* `GH_HOST` and
`GH_ENTERPRISE_TOKEN` fall under the existing `GH_` protected prefix. A test pins that
`.issuebot/env` is refused both, and that `PROTECTED_ENV_PREFIXES` still contains `GH_`.

**The credential helper.** `POST_CLONE_SCRIPT` registers the helper on
`credential.https://{host}`. The host has passed the setting's validation, which is what makes
the interpolation safe, and a test feeds the script builder a hostile value to prove the
validator stands in front of it.

**`hosts.yml`.** No change. Add a test that plants `api_host` and the other steering keys under
a `SUBDOMAIN.ghe.com` block and under a GHES-shaped host's block, and asserts the sweep strips
them while keeping each block's `oauth_token`.

**The repository key.** `github.repo` is unchanged, and the key the rest of issuebot uses is
derived from it:

| Host | Key | Pages | JSON |
|---|---|---|---|
| github.com | `owner/name` (unchanged) | `/r/owner/name/` (unchanged) | `/api/v1/repos/owner/name` (unchanged) |
| any other | `HOST/owner/name` | `/r/HOST/owner/name/` | `/api/v1/repos/HOST/owner/name` |

A host always contains a dot and a GitHub owner never does, so the two forms cannot collide and
nothing has to guess which one it is holding. In a key the number of slashes tells them apart. In
a URL, where a suffix such as `/issues/12` follows, a first segment containing a dot is a host.
The router matches the host segment with a pattern that requires a dot and the owner segment
with one that forbids it, so the two route shapes are disjoint by construction. The builder
chooses how to express that, whether as Starlette path converters or by registering the host
routes first.

Everything that takes the key today takes the derived key instead of `github.repo`:

- the registration in `repos`;
- every scoped query;
- the NOTIFY payload pattern in `db/listen.py`, widened to the host form;
- `refresh`, `status` and `stats`;
- the web's cookie for the last-picked repository, and `repo_base` and `api_base`.

The picker shows the key, so a tenant repository is visibly a different entry from its
github.com predecessor.

**No database migration for the key.** Every existing row is a github.com row and already holds
the right key. A tenant repository registers a new key on its first start.

Issue identifiers (`<short-name>-<number>`) can repeat across hosts during the 30-day overlap,
`foo-12` on both. That is cosmetic: a Slack line carries the real link, and the dashboard scopes
by key. *(My call: leave it.)*

**Links.** Slack's issue link uses the issue's URL as GitHub reported it, not one built from
`github.com`. That means carrying the URL on `IssueEvent`: every such event is built from an
`Issue`, which has its `url`. If the builder finds an event built without one, the link is built
through the hosts module instead, never from a literal. #279's step 0 makes the same change, and
whichever lands first does it; the other rebases onto it. The dashboard already uses the stored
URL.

**Egress.** The proxy reads no workflow, so it learns the host from `ISSUEBOT_GITHUB_HOST` in the
checkout's `.env`. Compose passes that to the `egress` service; unset means github.com. The
allow-list becomes:

- github.com, or unset: `DEFAULT_ALLOW`, **unchanged**;
- `SUBDOMAIN.ghe.com`: `DEFAULT_ALLOW` plus `SUBDOMAIN.ghe.com`, `api.SUBDOMAIN.ghe.com`,
  `objects-origin.SUBDOMAIN.ghe.com` and `results-receiver.actions.SUBDOMAIN.ghe.com`.

`objects-origin` is the counterpart of github.com's `objects.githubusercontent.com` going by the
names `/meta` lists. It is confirmed, or replaced (`github-raw.`, `codeload.`), by measuring what
`gh` actually dials in step 2's manual run, the way github.com's entries were established.
`ISSUEBOT_GITHUB_HOST` goes through the same validation as `github.host`, so a value the setting
would refuse is refused by the proxy at start too.

**The proxy and the worker can disagree, and `validate` catches it.** Its existing egress probe
asks the proxy about `PROBE_REQUIRED_HOST`, which becomes the configured host's API host. A proxy
built for a different host refuses it. The failure says that `ISSUEBOT_GITHUB_HOST` and
`github.host` must name the same host. *(My call: two settings with a check, rather than making
the workflow read the environment variable implicitly. The builder may instead document
`github.host: $ISSUEBOT_GITHUB_HOST` in the workflow overlay, which the resolver already
supports for `github.token`. The check stays either way.)*

**The tenant's results storage** cannot be derived from the host (see "What was established"). It
goes in `ISSUEBOT_EGRESS_ALLOW`, the way an operator's registries do today. `validate`, which
holds the token, reads the tenant's `/meta`. It takes the `.blob.core.windows.net` entries of
`domains.actions_inbound.full_domains`, which is the rule `ACTIONS_RESULTS_HOSTS` was built by
for github.com, and compares them with the proxy's effective list. If any are missing, it
**warns** and prints the exact value to add. A worker without them works, but a session reading a
failed job's log gets the bare `Forbidden` `egress.py` already describes. *(My call: a warning,
not a failure.)* This check runs only for `ghe_com`.

**The status annotation.** The hosts module gives the Statuspage summary URL: today's for
github.com, and `None` for any other host unless an optional `github.status_url` names one. When
it is `None`, the orchestrator skips the annotation and `validate`'s `github.status` line reads
"skipped: no status page covers HOST". A configured `status_url`'s host is the operator's to add
to `ISSUEBOT_EGRESS_ALLOW`.

**Docs.** `docs/security-model.md`'s egress table is tested against the derived list for
github.com and for a sample `SUBDOMAIN.ghe.com`: every derived name appears in the table, and
every GitHub-shaped name in the table is derived. The table states the wildcard rule and why
github.com stays reachable from a tenant worker (next paragraph).

**The security consequence of decision 3.** `gh` sends `GH_TOKEN` to github.com as well as to
`ghe.com` subdomains. A tenant session that deliberately pointed `gh` at github.com would
therefore send the company token to GitHub's own API, where it does not authenticate. No third
party is reachable, because no other `ghe.com` name is on the list. `docs/security-model.md`
says so.

### Step 1: enterprise accounts

**EMU usernames.** `billing._LOGIN` refuses an underscore, and every EMU username has one
(`handle_SHORTCODE`). The pattern accepts that shape. The builder also checks any other place that
validates a login against GitHub's alphabet.

**The account arrangement.**

- **A dedicated managed account** is provisioned by the company's identity provider and takes a
  seat. Everything works as the README's machine-account setup does. A person's
  `issuebot/todo` approves an issue and the bot's own never does, and commits are attributed to
  the bot.
- **The maintainer's own managed account** works only when it **administers** the repository.
  The worker's own `issuebot/todo` counts as approval only for an admin
  (`docs/operations.md`, "Blocked"). With write or maintain access, every issue is blocked with
  "no account other than LOGIN has applied `issuebot/todo`". A maintainer who creates the
  repository in the organisation is normally its admin; one an org owner created for them may
  not be. `validate`'s `github.token account` line already reports admin. The docs name this
  case for EMU.

**Who steers, in an organisation (decision 6).** Nothing changes in the code. The docs say what
it means when a repository moves from a personal account into an organisation:

- **Comments.** A session acts on comments whose `author_association` is `OWNER`, `MEMBER` or
  `COLLABORATOR` (`MAINTAINER_ASSOCIATIONS`). `MEMBER` is every member of the owning
  organisation, whether or not they have any access to that repository. On a personal repository
  that was the maintainer alone. In the organisation it is every colleague.
- **Approval.** Any person's `issuebot/todo` approves (`orchestrator/approval.py`), so anyone with
  triage access or above can approve.

This is intended, and `docs/security-model.md`'s "narrow the organisation, not the filter"
already covers a deployment that wants otherwise. `docs/operations.md`'s move runbook says it
in a sentence, so that a maintainer moving a repository knows who they are opening it to.

**The token's reach matters more in an organisation**, because colleagues' issue text now steers
a session that holds it. Under either account arrangement, the docs recommend a **fine-grained
token limited to the one repository**, with the permissions the README already lists. A classic
token from an organisation admin reaches every repository in the company organisation. `validate`
warns when the token is classic (a `ghp_` prefix) and the host is not github.com, and the warning
names the alternative. *(My call: a warning, because enterprise policy may forbid fine-grained
tokens or leave approval of them pending, and the maintainer may have no other choice.)*

**`validate` names enterprise refusals.** When `github.repo access`, or any GitHub check after
it, is refused, `validate` says which refusal it was rather than repeating a bare 403 or 404.
It distinguishes:

- a token not authorised for the organisation's SAML SSO, which GitHub signals with an
  `X-GitHub-SSO` header;
- a fine-grained token still awaiting the organisation's approval;
- the worker's egress address missing from an organisation or enterprise IP allow list, which
  GitHub names in the error message;
- an expired token.

The builder establishes each signal from GitHub's documentation and from `gh api -i` output, and
records it in a test fixture. Separately, the `github.token` line shows the token's expiry when
GitHub reports one in `GitHub-Authentication-Token-Expiration`, and warns within 14 days of it.
*(My call: enterprise policy commonly caps token lifetimes, and an expired token otherwise
surfaces as a blocked worker.)*

**The Actions-minutes tile, per host.**

- *Accounts.* The hub's `web` reads a list of billing accounts, each made of a host, a kind
  (`user`, `organization` or `enterprise`), an account name (for `user`, learned from `/user` as
  today), a token and its included minutes.
  - Today's two variables keep their exact meaning: a github.com `user` account.
  - A further account is added with further variables, without changing those two. The builder
    chooses the encoding, under three constraints: tokens live only in the hub's environment, a
    malformed entry refuses `web`'s start and names itself (as a malformed allowance does
    today), and `.env.example` and `docs/dashboard.md` show a worked example.
- *Reads.* Each kind has its own endpoint:
  - `user`: `/users/{login}/settings/billing/usage/summary?product=actions`, as today;
  - `organization`: `/organizations/{org}/settings/billing/usage/summary?product=actions`;
  - `enterprise`: `/enterprises/{enterprise}/settings/billing/usage/summary?product=actions`.

  Each read is made with that account's host, through `GH_HOST`. The same parser serves all
  three. The builder confirms from GitHub's documentation that the organisation and enterprise
  responses have the same shape, and which token scope or permission each kind accepts. The first
  real read records the response's `X-Accepted-OAuth-Scopes`, as the actions-minutes spec did.
- *Storage.* The `actions_minutes` row gains a host. Migration `0009`:
  `ADD COLUMN host text NOT NULL DEFAULT 'github.com'`, with the primary key becoming
  `(host, account)`. Existing rows become github.com rows, which they are.
- *The tile, and which account a repository matches.* Today the window is drawn only on
  repositories the account owns (`RepoQueries.actions_minutes` compares the account with the
  key's owner segment; `docs/dashboard.md`). That stays the rule, with the host added. *(My
  call; it is what keeps a github.com organisation's repository from showing a personal
  figure.)*
  - A `user` or `organization` account matches the repositories on its host whose owner it is.
  - An `enterprise` account on a `ghe_com` host matches every repository on that host, because a
    GHE.com tenant *is* one enterprise. An owner match wins over it, so an organisation that is
    configured shows its own figure.
  - An `enterprise` account on github.com would have to name the organisations it covers,
    because the `web` reads no workflow and cannot learn that for itself. Nobody needs that yet,
    so the kind is refused for github.com at start, with a message saying so.
  - Several accounts may share a host, but at most one per owner, and at most one `enterprise` per
    `ghe_com` host. A list that breaks either rule is the "malformed entry" that refuses `web`'s
    start.
  - With no matching account, the minutes window is hidden and the Claude windows remain.

  A tenant repository therefore never shows personal minutes (decision 5), and a github.com
  repository shows exactly what it shows today. The maintainer is an admin of the target
  organisation, so the `organization` kind is the one expected to be readable. `enterprise` is
  built for a tenant whose organisations bill through the enterprise, and is proven only if the
  maintainer can get a token that reads it.
- *The Slack alert* is per account, as now. Its text names the host when it is not github.com.
- The `web` service is not behind the egress proxy (`networks: [issuebot]`), so this needs no
  egress change.

**Docs** (README's token advice, `docs/security-model.md`'s "The account a session acts as"):

- On EMU, the machine account is provisioned by the identity provider.
- Enterprise policy can disable classic tokens, require organisation approval for fine-grained
  ones, and cap their lifetime. The docs say which token kind works under which policy.
- The commit email advice covers the tenant's own noreply domain. The builder verifies its form
  on the tenant.
- The own-account case needs admin on the repository, and the docs say what happens without it.

### Step 2: GHE.com

Most of it is step 0's derivation turned on for `ghe_com`:

- `github.host: SUBDOMAIN.ghe.com` is accepted. `GH_TOKEN` and `GH_HOST=SUBDOMAIN.ghe.com` go to
  `gh` and the session.
- Egress uses the derived list, plus the results storage that `validate` reads from `/meta`.
- The status annotation is skipped (the EU page has no summary).
- The minutes tile shows the organisation account, if one is configured for the tenant.

**A runbook in `docs/operations.md`: moving a project from github.com to GHE.com.**

1. Create the repository in the organisation and push the code to it.
2. Make a new checkout with `github.host`, `ISSUEBOT_GITHUB_HOST` and the tenant token.
3. Run `labels ensure`.
4. Run `validate`, and add the storage line it prints.
5. Start the worker.
6. Leave the old checkout running until its open work drains, then stop it.
7. Remove the old repository from the picker. This is a hand-written `DELETE` until the follow-up
   lands.

This also states decision 2: the new repository starts fresh.

**Proven by hand on the tenant** (`MANUAL` stages in #300's plan):

1. `validate` is clean. That includes the egress probe against `api.SUBDOMAIN.ghe.com` and the
   `/meta` storage check.
2. One issue is claimed, worked, its pull request opened and merged, and the issue completes,
   under whichever account arrangement exists at the time.
3. A deliberately failing CI job's log is read by a session through `gh run view --log`, which
   proves the storage accounts.
4. A release-asset download and a raw-file fetch through `gh` show which of `objects-origin.`,
   `github-raw.` and `codeload.` the derived list needs. Every `egress_denied` line from that run
   is read.
5. A session fetches a public dependency from github.com (decision 3).
6. The dashboard serves `/r/SUBDOMAIN.ghe.com/ORG/REPO/`, its github.com predecessor still serves
   at its old URL, and a Slack line links to the tenant issue.
7. The minutes tile reads the organisation's figure, once the tenant's Actions billing is live
   and a token with the right scope exists. If the organisation endpoint stays a 404, the tile
   stays unconfigured for the tenant, and the plan records why.

**If the tenant is not ready when the code is.** The tenant's setup is unfinished, and Actions
may not run on it yet. Proofs 3 and 7 need Actions running, and so does proof 2 if the tenant
repository's ruleset requires checks. The code stages merge on CI's proof as every stage does.
The manual proofs are a checklist in #300's plan, tracked by an `issuebot/operator-step` issue
of their own that closes when the last one passes. A proof that needs Actions waits for Actions
and holds nothing else up. *(My call.)*

### Step 3: Enterprise Server (specified, not built)

**Nothing in this section is built.** No stage of #300's plan builds it, and no issuebot session
works towards it. It is recorded so that steps 0--2 leave room for it, and so that a GHES issue,
if the maintainer ever files one by hand, starts from settled answers.

- **Host and token.** The setting's refusal of other hosts is lifted for `ghes`. `gh` gets
  `GH_HOST` and `GH_ENTERPRISE_TOKEN`. `gh` treats every host other than github.com and the
  `ghe.com` subdomains as Enterprise Server, so `GH_ENTERPRISE_TOKEN` is what it offers to any
  such host a session names. The allow-list naming the appliance exactly is what bounds that, and
  `docs/security-model.md` says so.
- **API and egress.** The API is at `https://HOST/api/v3` and `https://HOST/api/graphql`. The
  allow-list is today's default plus `HOST`. With subdomain isolation on, it also needs the
  subdomains `gh` dials for raw content and uploads, which a GHES issue would measure; whether it
  reads them from `/meta` or from a setting is that issue's choice. Actions logs live in
  whatever blob storage the appliance is configured with, so that host is the operator's
  `ISSUEBOT_EGRESS_ALLOW` entry, and the docs say that without it `gh run view --log` fails with
  the bare `Forbidden`.
- **The version floor, and how it is established without an appliance.** GitHub publishes the
  per-release schemas:
  - REST in `github/rest-api-description`'s `descriptions/ghes-3.N`;
  - GraphQL in the docs repository's per-release `schema.docs-enterprise.graphql`.

  The floor is the oldest release, inside GitHub's support window, that has every one of these:
  - `closedByPullRequestsReferences(includeClosedPrs:)`;
  - `timelineItems` with `LABELED_EVENT` and `RENAMED_TITLE_EVENT`;
  - `userContentEdits`, `bodyHTML` and `authorAssociation`;
  - the `rules/branches/{branch}` and `rulesets/{id}` endpoints, with
    `require_code_owner_review`, `require_last_push_approval`, `dismiss_stale_reviews_on_push`
    and `current_user_can_bypass`.

  A GHES issue should also add anything issuebot has started using since #280 was written,
  found by grepping the GraphQL documents. `validate` reads `installed_version` from `/meta` and
  fails below the floor, naming it, so a missing field fails `validate` and never the first poll.
- **Certificates.**
  - An optional CA bundle is mounted and installed into the image's system trust store when the
    container starts, so `gh`, `git` and Python's `ssl` trust it. `claude` talks only to
    Anthropic and needs no change.
  - A GHES issue has to settle how a container whose processes run unprivileged gets the install
    done, whether by an entrypoint step before it drops privileges or by something else.
  - Two constraints bind it. A container started without a bundle is byte-for-byte unchanged,
    which the `docker` CI job asserts. Verification is never disabled.
  - Per-tool variables (`SSL_CERT_FILE`, `GIT_SSL_CAINFO`, `NODE_EXTRA_CA_CERTS`) are rejected,
    because each would have to be added to the session's environment and then protected there.
- **Rulesets** are checked exactly as on github.com. The floor guarantees the endpoints.
- **Hidden or skipped.** The minutes tile is not offered for a `ghes` host, since self-hosted
  runners bill no minutes. The status annotation is skipped.
- **Scrubber.** Current GHES issues the same `ghp_` and `github_pat_` token formats. An older
  40-hex token is caught by the scrubber's masking of every `*TOKEN` variable's value. A test
  covers a `GH_ENTERPRISE_TOKEN` value in a turn log.

## What CI proves, and what needs a person

| Claim | Where it is proven |
|---|---|
| No `github.host` gives today's allow-list, environment, credential helper, Slack links, keys and URLs | CI: snapshot tests against the current values, plus the existing suite unchanged |
| No literal GitHub host outside the hosts module | CI: the scanning test |
| `GH_HOST` and `GH_ENTERPRISE_TOKEN` are refused from `.issuebot/env`; `hosts.yml` steering keys are stripped under any host | CI |
| Setting validation, including hostile values and the GHES refusal | CI |
| The key and route forms, their disjointness, NOTIFY payloads | CI |
| The derived allow-list per host, no wildcard, the docs table matching | CI |
| `validate`'s `/meta` storage check | CI against a recorded `/meta` shape (sanitised: `SUBDOMAIN`, `REGION`) |
| `validate`'s named refusals and token expiry | CI against recorded responses |
| Billing for each kind, the EMU login, migration `0009`, the per-host tile | CI with fakes; the database tests on `test-db` |
| End to end on GHE.com, storage, `objects-origin` versus `github-raw`, the github.com dependency fetch, the dashboard and Slack | **Manual**, on the tenant (step 2's list) |
| The organisation minutes figure | **Manual**, once the tenant bills Actions |
| Anything GHES | Not built (step 3) |

Every fixture recorded from the tenant is sanitised before it is committed, and a reviewer
greps the diff for the real subdomain.

## Left to the builder

- The hosts module's name and shape, and how the router makes the two route shapes disjoint.
- The encoding of further billing accounts in the hub's environment, under the stated
  constraints.
- The exact signals for each named refusal, and the scope each billing kind accepts, taken from
  GitHub's documentation and recorded responses.
- Whether the workflow overlay documents `github.host: $ISSUEBOT_GITHUB_HOST` as well as the
  `validate` agreement check.
- The staging within each step. #300 asks for stages small enough for one unattended run, each
  leaving `main` green, with step 2's manual checks marked.

## Follow-ups

- **Removing a repository from the dashboard's picker**, and its rows if wanted. It is needed for
  a move, and for a github.com deployment that is retired. It is filed only with the
  maintainer's approval.
- **GHES is deliberately not filed.** `triage.auto` is on for this repository, so a new issue
  would be triaged and worked. The maintainer files one by hand, labelled
  `issuebot/operator-step`, if an appliance ever becomes available.
- **#280, #299 and #300 were edited on 2026-10-30 to match decision 1.**
  - #280 drops step 3 and its acceptance criteria, and says GHES is not to be built.
  - #300's stages stop after GHE.com.
  - #299 carries a note saying its GHES points are answered on paper.

  #280 is what issuebot's parent review measures the finished work against, so it must not ask
  for GHES.

## #280's acceptance criteria, mapped

- **Step 0.** The unchanged default, the literal scan, `GH_HOST` and `GH_ENTERPRISE_TOKEN`, and
  the any-host sweep are all in step 0 above, and CI proves them.
- **Step 1.**
  - The tile works for user, organisation and enterprise kinds and accepts an EMU login: step 1,
    proven in CI. The organisation kind is also proven by hand on the tenant.
  - Named refusals: step 1, proven in CI.
- **Step 2.**
  - End to end: proven by hand.
  - No wildcard: proven in CI.
  - The egress probe against `api.SUBDOMAIN.ghe.com`: proven by hand, and the probe's target host
    in CI.
- **Step 3.** #280 no longer has any step 3 criteria. They were removed under decision 1.
- **Docs.** README, `docs/security-model.md`, `docs/operations.md` (including the move runbook)
  and `docs/dashboard.md` describe github.com and GHE.com. GHES is described as specified and not
  yet supported. The egress table is tested against the derived list.
