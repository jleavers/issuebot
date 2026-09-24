# Operating issuebot

Running a worker once it is up: a second repository, the one credential that has to be rotated
by hand, and what each way of going wrong looks like from the outside.

Getting a first worker running is the [README](../README.md); what the target repository's
suite needs installed is [Toolchains for the target repository](toolchains.md).

## More than one repository

One database and one dashboard serve every repository; each repository still gets its own
worker, in its own checkout, with its own `configs/WORKFLOW.local.md`, workspaces volume
and Claude credential. The checkouts meet on one Docker network.

1. Once per host: `docker network create issuebot` and
   `docker network create --internal issuebot-internal`. The second is where the workers reach
   the hub's database; `--internal` is what leaves them no route off the host except the
   allow-listing proxy ([`docs/security-model.md`, "What a session may
   reach"](security-model.md#what-a-session-may-reach)).
2. The checkout you already run is the **hub**: its `.env` says `COMPOSE_PROFILES=hub,worker`,
   so `docker compose up -d` starts the database, the dashboard and this repository's worker.
3. Every other repository: clone issuebot again: `git clone https://github.com/jleavers/issuebot.git issuebot-myrepo`,
   set `github.repo` in its    `configs/WORKFLOW.local.md`, copy `.env.example` to `.env` with `COMPOSE_PROFILES=worker`
   and the **hub's** `ISSUEBOT_DB_PASSWORD` (the worker authenticates to the hub's database
   with it; compose refuses to start the worker while it is empty), and `docker compose up -d`.
   The worker reaches the hub's database as `db` over the shared network and registers itself;
   it appears in the dashboard's dropdown on its first start.
5. The dashboard is at http://127.0.0.1:8080 (the hub's `ISSUEBOT_WEB_PORT`, and the hub's
   `ISSUEBOT_WEB_PASSWORD` at the prompt). `/` opens the repository you last chose; the header's
   dropdown switches.

`issuebot status`, `stats` and `refresh` act on the repository their workflow names, so run
them from that repository's checkout.

## Rotating the database password

The postgres image reads `POSTGRES_PASSWORD` once, when it creates the cluster in the `pgdata`
volume; after that the password lives in the cluster, and a new value in `.env` changes only
what the worker and the web present, so they would be refused. Change the two together, on the
hub, in this order -- compose will not load the file at all, `exec` included, until `.env`
holds a value:

```bash
# 1. put the new value in ISSUEBOT_DB_PASSWORD in this checkout's .env, and in every other
#    repository's worker checkout
# 2. tell the running cluster (over the container's local socket, which asks no password):
docker compose exec db psql -U issuebot -d issuebot -c "ALTER ROLE issuebot PASSWORD 'the-same-value'"
# 3. recreate what reads it, here and in each of the other checkouts. db is recreated too:
#    an existing cluster ignores POSTGRES_PASSWORD, so the data is safe, but the server
#    restarts and every open connection drops.
docker compose up -d
```

Pick a quiet moment: between steps 2 and 3 the running worker and dashboard are refused on
every new connection, and step 3 recreates the worker, which stops any session in flight under
`stop_grace_period`.

A deployment that predates the variable -- one whose cluster was created with the shipped
default that older versions carried -- is rotated the same way; until it is, that cluster
answers to a password that was public. The `ALTER ROLE` line puts the value on your shell's
command line and in its history; `psql`'s `\password issuebot` prompts for it instead, if
that matters on your host.

## When things go wrong

### Blocked

If the agent hits a true external blocker (a missing tool, credential or
permission), or a run exhausts `agent.max_turns`, `agent.run_timeout_ms` or
`agent.max_attempts`, the worker moves the issue to `issuebot/review` with a Blockers
section in the workpad. Fix the cause, then label it `issuebot/rework` or `issuebot/todo`
to retry.

`agent.run_timeout_ms` escapes like `agent.max_turns` rather than counting against
`agent.max_attempts`: a retry never resumes a session, so it would re-read the repository
from cold and spend the same wall clock over again. The workpad block says `Wall clock
exhausted: N turns in attempt 1 ...`, so an issue that needs longer needs the setting
raised, not another attempt.

`agent.max_attempts` counts *the issue's* failed runs, not one unbroken chain of them: the
worker keeps the count itself, so a label move between a failure and the retry that follows
it — by the session, by a collaborator, or by the issue reaching `issuebot/review` a second
later — does not hand the issue a fresh budget. Two things clear the count, and only two: a
run that succeeded, and the escape above, which is what makes relabelling a blocked issue
work the way this bullet says it does. The count survives a restart: the worker reads it
back out of the database on the way up, from the last 90 days and the 500 most recently run
issues, one short of the ceiling at most — a reading it did not take itself never refuses an
issue outright, so every issue always gets a run that can either succeed or escalate it.

What that leaves unbounded is an issue relabelled again and again, each cycle worth
`agent.max_attempts` runs. `agent.max_issue_cost_usd` is the ceiling for it: cumulative per
issue, never reset, `0` (the default) off. It is off by default because what a run is worth
depends on your plan — an agent on a subscription reports no cost at all, and there
`agent.max_attempts` is the ceiling that bites. Like the seed, it is read from the last 90
days. A worker that refuses an issue on either budget logs `dispatch_refused`, writes an
`### Issuebot budget limit` block on the workpad naming the setting, and moves the issue to
`issuebot/review`: a ceiling nobody can see would be worse than no ceiling, so the board
never just stops for an issue without saying so on it.

### GitHub itself

The worker reads and writes its whole state machine through `gh`, so an
outage stops the board. Three failed polls in a row hold dispatch: `issuebot status` prints
`dispatch: held (github) since ...`, the dashboard's worker line reads `worker held`,
`/healthz` reports that repository's entry in `workers` with `"status": "held"`, and
`docker compose logs worker` shows `dispatch_github_held` — ERROR the first time and each
time the error changes, WARNING in between, since an idle worker says nothing else. The
first poll that answers lifts it and dispatch resumes, with no restart. A single failed poll
does not hold anything: `gh` retries a transport error of its own, and an `HTTP 5xx` or a
timeout is classified `transport` and retried. The count is three consecutive *polls*, and
it is the third failure that holds, so at the default `polling.interval_ms` that is a minute
after the first one — less when an `issuebot refresh` has brought ticks closer together, which
errs towards holding. Holding is the safe side — a worker
that cannot read the board has no business claiming from it — and a due retry waits with it
rather than spending an attempt on a claim that is going to fail.

The poll is what makes this hold safe on its own: a poll that failed offers nothing to
claim, so the hold you see is always derived from a poll that failed on this very tick
rather than from a remembered verdict. What it changes is the retry queue, which would
otherwise write to the board without reading it first — and every claim, from the poll or
from the queue, goes through one admission gate that asks the holds, the free slots and the
issue's own budget in that order, so a hold the worker reports is a hold at every door.

When the hold engages, the worker reads
[githubstatus.com](https://www.githubstatus.com/) once and appends what it says to the
reason, so the line reads `GitHub is not answering this worker: transport: http 502: Bad
Gateway — githubstatus.com at 12:01Z: Pull Requests, major outage`. The reading is stamped
because it is taken once, when the hold engages, and then stands for the whole outage. That is
annotation and never a gate: an incident is published when a human declares it, which can be twenty minutes after
the first failed write, so a slow, silent or nonsensical answer costs the annotation and
nothing else. `All Systems Operational` is worth reading too — it points you at your own
network rather than at GitHub's. `issuebot validate` reports the same page as a
`github.status` line, which can warn but never fails.

What none of this catches is GitHub answering `200` with stale data: a label write that
reports success and is not visible on the next read leaves the worker acting on a state
GitHub will later contradict, and there is no error to count. So subscribe
[githubstatus.com](https://www.githubstatus.com/) to the same Slack channel the worker posts
to as well, and an incident arrives in the timeline beside the runs it explains.

### Checks that never ran

A pull request whose checks are all red, where every failed job reports *zero steps*, has not
failed CI: Actions declined to run it. The account is out of minutes, or on a billing hold, or
a runner never came — whatever the cause, the job was never scheduled, so there are no steps
for it to have failed at, and the check-run annotation says so in GitHub's own words, usually
naming billing. A job that ran and failed looks nothing like this: it has steps, one of them
red, and a log under it.

The step count is what tells the two apart, and it is worth asking before reading a single
line of the diff:

```bash
gh run list --branch <branch> --limit 1 --json databaseId,conclusion
gh run view <id> --json jobs \
  --jq '.jobs[] | select(.conclusion == "failure") | {name, steps: (.steps | length)}'
```

Every failed job at `"steps": 0` is Actions declining. A job with a step count is CI reporting
on the code, and whatever it found is in the diff.

The agent asks the same question before it hands an issue over, and a run that never executed
does not hold a finished issue. Parking one would spend the run's remaining turns waiting for a
check that is never coming, and end at `agent.max_turns` with the issue escalated anyway —
all of it on a fault that is not in the code and that no session can clear. What that does
not do is lower the bar — the evidence only moves. The same suite, lint and format must be
green *locally* on that commit, and the agent records the run id and those local results
under `Validation` in the workpad before it moves the issue to `issuebot/review`. A job that
ran steps and failed still holds the issue, however much else is red beside it: the
distinction is the step count and nothing softer.

Clearing the cause is yours rather than the agent's. A billing hold is lifted by a human with
the account's settings open, and no session can reach that page; until it is lifted, every pull
request in the repository looks red the same way — issuebot's and your own alike — and
re-running the checks only declines them again. So while it lasts, read the workpad rather than
the check list: the local run recorded there is the evidence that the code is good, and the red
checks are evidence about the account.

### Cost

Every turn is capped by `claude.max_budget_usd`, so one run's ceiling is that
times `agent.max_turns` — `5.0` and `5` mean up to $25 before the issue is escalated. The
right value is yours to pick and the checked-in `5.0` is only a starting point: on an API
key it is real money and a tight cap is a real guard, while on a Claude subscription there
is no per-token charge and the cap acts as a cheap-and-cheerful effort limit instead, so a
larger number costs nothing but a longer leash. The cap ends the turn, not the run: the
turn is recorded as `budget_exceeded`, and the next one resumes the same Claude session
with a fresh cap rather than failing the attempt, which would start a replacement session
from cold and pay the cap again to reach what this one had already pushed. A run whose
every turn hits the cap therefore stops at `agent.max_turns` and is escalated like any
other. The dashboard and the `run_ended` Slack line (opt in via
`notifications.slack.events`) show each run's cost.

### Restarts

Workspaces persist in the `workspaces` volume; on startup the worker resumes
issues that were `issuebot/in-progress` from where they stopped.

### How long a workspace lives, and what a reused one hands the next session

A workspace is created when an issue is first claimed and removed when the issue closes —
whether the worker marks it `issuebot/complete` or reads the close as an abandonment and
clears the label; in between it outlives every one of its runs. That removal is the worker
finding the closed issue still carrying one of its state labels, so an issue closed *and*
stripped of its label is one whose workspace stays on disk until you remove it. An issue
sitting in `issuebot/review` keeps its clone for as long as it waits for you, and a retry, a
rework bounce or a re-queue picks that clone up rather than cloning again — which is the
point, and part of what `agent.max_issue_cost_usd` pays for. No setting shortens this
deliberately: the issue's own lifecycle is the lever, removing the workspace directory by hand
makes the next session clone from cold, and re-binding the pool has the same effect as a side
effect (the clone is re-cloned when its owning account moved — [One account per concurrent
session](security-model.md#one-account-per-concurrent-session)).

**So the next session on that issue inherits the clone whole** — the working tree as the last
one left it, its untracked files, `<workspace>/.venv`, and `.git` with it. That includes the
clone's own `.git/config` and `.git/hooks/`, which name commands git runs (`core.pager`,
`credential.helper`, `core.fsmonitor`, `core.hooksPath`, `[alias] x = !...`). Issuebot does
not reset them between runs, deliberately: the unit of that channel is the clone and not the
file — `include.path` puts the same keys in a second file, `core.hooksPath` puts them in a
directory of scripts, and the venv the session's tests run out of is wider than any of them —
so the only thing that would close it is not reusing the workspace at all. It crosses no
privilege boundary either way: one workspace belongs to one issue, and under a pool it is
bound to one session account (see [One account per concurrent
session](security-model.md#one-account-per-concurrent-session)) and sealed back to the worker
between runs (`docs/superpowers/specs/2026-09-14-session-account-pool-design.md`) — with one
account for the whole deployment, or on the host route, there is no binding and no seal, but
then every session shares a home, which is wider than any one workspace. Either way,
everything a plant could defer to the next session the session holding it can already do
itself, with the same token, on the same branch and the same pull request. This is recorded in
`docs/superpowers/specs/2026-09-22-clone-reuse-residual-design.md`, which also has the
`hooks.before_run` recipe for a deployment that wants the clone's config narrowed each run,
and the four caveats that come with it. What is *not* inherited is the worker's own state in
the workspace, anything in `.issuebot/env` that would re-point `claude`, `git`, `gh`, the
hook shell or the dynamic loader under all but `gh`, or change which certificate authorities
any of them accepts, and anything at all by a session working a **different** issue — see
[Safety](#safety) below for that boundary and the sweeps that hold it.

### Configuration changes

A running worker re-reads `configs/WORKFLOW.md` and
`configs/WORKFLOW.local.md` when either changes, or the overlay appears or disappears,
within one `polling.interval_ms`, and logs `workflow_reloaded` naming the sections
that moved. `database.url`, the Slack webhook and its event list are read once at start, so
those need `docker compose restart worker`.

Compose mounts the **directory** `./configs` at `/configs` and points `ISSUEBOT_WORKFLOW`
at the file inside it, rather than mounting `WORKFLOW.md` itself. A single-file bind mount
resolves to the inode, so an editor that saves by writing a temporary file and renaming it
over the original — most of them, and `sed -i` too — gives the host file a new inode and
leaves the container reading the old one, silently, until it is recreated. Mounting the
directory means the lookup goes through the host's directory entry every time, so an
ordinary save is picked up normally. Keep your own configuration inside `configs/`: a file
mounted individually from anywhere else has the same problem, in Compose or anywhere else
that mounts one file (a Kubernetes `subPath`, say). The overlay is looked for beside
`WORKFLOW.md` and nowhere else for exactly this reason: a sibling inside the mounted
directory reloads on the same terms as the base, and a `WORKFLOW.local.md` mounted on its
own would be pinned the same way, holding the settings you change most often.

If one ever is, the worker says so instead of serving the old settings quietly. It logs
`workflow_reload_failed` at ERROR and carries the reason as a config error into
`issuebot status`, the dashboard's worker line and `/api/v1/repos/<owner>/<name>/state` —
`single-file mount`
when the file is a mount point, which it can tell because a file and its own directory can
only be on different devices if the file is mounted, and `stale mount` once the host has
actually replaced it and the file the worker holds has no directory entry left. It is a
warning, not a guarantee: it never changes what the worker runs, and a platform that
reports neither signal faithfully will stay quiet.

### Upgrades

With your settings in `configs/WORKFLOW.local.md` and the tracked
`configs/WORKFLOW.md` untouched, a clean `git pull` is the expected experience: the prompt
and the defaults update, your overrides stay. If you edited the tracked file before the
overlay existed, move those edits into the overlay and `git checkout configs/WORKFLOW.md`
first. `configs/` is mounted into the container, but the code is baked into the
image: after pulling a new version of issuebot, run `docker compose build` (or
`docker compose up --build -d`) before anything else. If anything in a session reaches a registry, put it
in `ISSUEBOT_EGRESS_ALLOW` in the same pass -- a hook's `uv sync` or `npm ci`, and equally the
`uv run pytest` or `pip install` the session runs in its own shell to validate a change.
Without amending `ISSUEBOT_EGRESS_ALLOW` the session reaches Anthropic and GitHub and nothing else, so an unlisted
registry surfaces as a failing hook or a failing test mid-run rather than as a configuration
error (see [What a session may reach](security-model.md#what-a-session-may-reach)). A setting that a newer
`WORKFLOW.md` introduces fails against a stale image at `validate`, as
`<key>: Extra inputs are not permitted`.

**With more than one checkout, that recipe is not the one to repeat per deployment.** They are
clones of the same repository against one store, and a migration applied by whichever you upgrade
first will stop the others' workers starting until they carry the same code -- so the order is
every checkout through one step, then every checkout through the next, rather than each checkout
to completion in turn. `tools/upgrade/upgrade.py` does that:
`python3 tools/upgrade/upgrade.py --dry-run` reports what is pending in each and which
environment keys a new `.env.example` expects, and without the flag it stops the workers, pulls,
builds, validates against the new image and brings them back up hub first, leaving any checkout
that failed stopped rather than running beside the others on an older image. See
[tools/upgrade/README.md](../tools/upgrade/README.md).

### Safety

The enforced boundary is the container, its **network**, and inside it the uid:
the session (`claude -p`, every hook, the clone) runs as a session account — by default the
pool the image built, `agent-1` .. `agent-N` — a different account from the worker
(`issuebot`, uid 1000) that supervises and credentials it, and -- with a pool, see [One
account per concurrent session](security-model.md#one-account-per-concurrent-session) -- at a different uid from every other session
running beside it. So the session runs
with no permission prompts and may do as it likes at its own uid, but the worker's code
(`/app`, root-owned), the rest of its environment (the database URL, the Slack webhook, and
in a hub checkout the dashboard password), its home and the state it keeps inside a
workspace are all out of the session's reach, and the worker cannot become root or anything
but a session account. `GH_TOKEN` is the one credential the session is given, since it clones
and pushes with it, which is why the token should be scoped to the repository: `validate`
warns when it is a classic or an OAuth token, whose reach is the account's, and says so. The
session's tools are fixed the same way, by the front matter and the argv issuebot builds
from it (`claude.disallowed_tools`, which ships with `WebFetch` and `WebSearch` in it, and
`--strict-mcp-config` on every session), so the prompt's rules about what a reporter wrote
describe what the session may do *within* that authority rather than granting it, and the
`<github-text>` envelope is a hint to the model, never the boundary. Its *network* is
fixed outside the prompt too: under Compose the container's every network is `internal`, so
the session has no route off the host but the allow-listing proxy beside it, and `GH_TOKEN`
can be carried to Anthropic, to GitHub and to whatever else the deployment named, and to
nothing else ([What a session may reach](security-model.md#what-a-session-may-reach)). The session's
home is its own — `/home/<account>/.claude`, `0700` from the image — and holds no credential:
it authenticates from the environment, which is why nobody logs into it. With a pool
the sharing is with the next session bound to that same account rather than with the ones
running beside it; with one account for the deployment every concurrent session shares that
home. Either way, before every turn the worker sweeps the config a prior or concurrent
session could have left there — a user-level `CLAUDE.md`, `rules/`, `skills/`, `commands/`,
`agents/`, `workflows/`, `agent-memory/`, `plugins/`, `output-styles/`, `settings.json`,
`settings.local.json` and each project's auto memory (`projects/<project>/memory/`), the
surfaces a later `claude -p` loads as instructions or behaviour — and, from the home itself,
the account's shell start-up files (`.bash_profile`, `.bash_login`, `.profile`, `.bashrc`,
`.bash_logout`): `/home/<account>` is the account's to write, every hook and the
post-clone setup run under `bash -lc`, a login shell, and `claude` snapshots one for the
session's Bash tool, so a `~/.profile` one session leaves is a script every later session
runs at that uid. That is why the sweep runs before each of those scripts as well as before
each turn — `before_run` would otherwise be the next session's first login shell, and it runs
before turn 1 — and before the *clone*, which opens no shell but is the earliest thing a run
does at that uid, and which reads both `gh`'s and git's config out of the home while holding
the token. The same home holds the config a *tool* the session runs reads, and that is
swept with it: `~/.gitconfig` and `~/.config/git/config` — both, because git reads the
second of them first — and `~/.ssh/config`, each of which can name a command (`core.pager`,
`credential.helper`, `[alias] x = !...`, `ProxyCommand`) for the next session's `git` or `ssh`
to run; and `~/.config/gh/config.yml` (#173), which can name one for `gh` (`aliases:`) and can
also re-point where `gh` sends its requests and its `GH_TOKEN` with them (`http_unix_socket`, a
unix socket rather than a network route, so the egress proxy never sees it) on an ordinary
`gh api` or `gh repo clone`. Nothing a deployment needs goes there: the bot's identity is the
`GIT_AUTHOR_*`/`GIT_COMMITTER_*` values you set in `.env`, the workspace's `safe.directory`
entry is the image's system-wide one, the clone's credential helper is written into the clone,
and global git or ssh config for every session belongs in `/etc/gitconfig` or
`/etc/ssh/ssh_config`, which are root's and which no session can write.

One file is *edited* rather than removed, and it is the only one: `~/.config/gh/hosts.yml`.
It is credential state — it holds the `oauth_token` a session authenticates `gh` with, where a
session has one — so taking it would break authentication for every deployment that relies on
it, and it stays. But `gh config set -h <host> <key> <value>` writes into that file rather than
into `config.yml`, and one of the keys it can carry, `api_host`, re-points `gh` at a host of the
planting session's choosing on an ordinary command: measured against `gh 2.100.0`, a planted
`api_host` sends `gh api`, `gh issue list`, `gh pr list` and the `gh repo clone` issuebot runs
to build the next workspace to that host instead of GitHub's. `git_protocol` is the same channel through another key: set to
`ssh` there, the next session's `gh repo clone` fails outright, since the image ships no ssh
client.

So the sweep removes gh's whole configuration surface from that file — the fourteen keys
`gh config --help` advertises, none of which is credential state — and leaves everything else,
the tokens included. Thirteen of the fourteen are on the list because `gh config set -h <host>`
writes them here rather than into `config.yml`. The fourteenth, `clipboard`, which `gh` 2.101.0
added, is on it for the property those thirteen share rather than for that one: `gh` declines to
write it host-level at all, but a value planted by hand under the host is still resolved from
here ahead of the hostname-less lookup, and residue is planted rather than politely set (#231).
The rule is not "these keys are dangerous" but "a session does not leave *configuration* in a
credential file", so what survives is the credential state: `oauth_token`
and `user`, and the per-account tokens in the `users:` subtree. The keys go from that subtree
too, since `gh config set -h` mirrors the ones it writes there as well as at host level. If the
sweep ever cannot parse the file it leaves it alone and *says so* — the worker logs
`claude_home_sweep_failed` each turn — rather than reporting a success it did not have. A file with none of them in it is not rewritten at all; one that does
carry one is rewritten by a YAML parser, so it comes back normalised rather than
character-for-character, which is what `gh` itself does to this file on an ordinary command. What is left of the
channel is bounded and documented in
`docs/superpowers/specs/2026-09-22-session-gh-hosts-design.md`: `gh` sends no credential to a
substituted host, a forged answer needs a certificate authority in the system trust store, which
is root's, and the egress proxy refuses any host off its allow-list. That note was written while
`~/.config/gh/config.yml` beside it was still a survivor, so it is explicit that the same
`git_protocol` written there with a plain `gh config set` steered the next session's clone
whatever this edit did. #173 closed that position too — the file is swept, above — so both are
shut now. `api_host` never had a second position: top level is inert for it, and this edit
closes it outright.

`gh`'s extension directory is swept on the same schedule (#186), and is the one thing swept
that is a program rather than a setting: `~/.local/share/gh/extensions` is where
`gh extension install` puts a program that `gh <name>` runs, and it needs no install step to
be a plant — a directory and an executable file are dispatched just the same. Here a
deployment *may* have a use for the directory, which is the one thing that made this a
question: where no extension is installed the entry costs nothing, but where one is, the
directory is still the account's to write, so a session can replace the program that
deployment's own sessions run — the plant then fires on an ordinary command rather than an
invented one, and swept the deployment gets `gh`'s own `unknown command` instead. So a
deployment that wants a `gh` extension available to every session installs the program
root-owned on the session's `PATH` — `/usr/local/bin/<name>` in an image built `FROM` this
one — and invokes it under its own name: `gh` hands an extension its argv and its own
environment and no credential of its own, so that is the same program doing the same work,
from a place no session can write. `gh` has no system-wide extension directory, which is why
the answer is `PATH` here and `/etc` for git and ssh.

The sweep leaves the rest of the home alone: the credential (`.credentials.json`, which
rotates its refresh token), the transcripts beside the memory it removes, `~/.claude.json`,
and whatever else claude or a tool the session ran keeps there (`gh`'s state, npm's cache).
The directories the tool config sat in stay too, with whatever else is in them — `gh`'s
credential state (`hosts.yml`) beside the config of its own that goes, `known_hosts` beside
ssh's, `~/.local/state/gh` beside the extension directory — since the sweep names a file or one
directory and never empties the one above it; a credential authenticates the next session
rather than steering it, which is the line `.credentials.json` sits on too. `hosts.yml` is not
purely credential, though — `gh config set -h <host>` writes configuration there as well — which
is why it is the one file the sweep edits rather than keeps whole, above. What survives that
edit is bounded by the egress proxy rather than by the sweep: a substituted host is a real
HTTPS request to a name, so the allow-list sees it, where the `http_unix_socket` in the
`config.yml` beside it would be no route at all. It is a denylist of what is loaded or run, not
an allowlist of what is kept, so a new claude location, a new tool config file or another
tool's plug-in directory has to be added to it by hand. Nothing is swept on the host route
(`agent.run_as` unset), where the home is your own. Auto memory is also switched off for the session
(`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, a fixed entry the workspace env file cannot override), since it is read whatever
`setting_sources` says and keyed by repository, so one issue's notes would be the next
session's prompt on the same repository. So a slash command, skill, memory or profile script a
hostile issue
plants is not waiting for a session working a different issue next week. What remains: the
window between one turn's sweep and its `claude -p` start, in which a session running beside
it at the same uid can still plant -- which a pool closes, since no two concurrent sessions
share a home; and the account's `~/.claude.json`, which sits beside the swept directory rather
than in it, whose `mcpServers` no session loads (`--strict-mcp-config`) while its trust
state persists for the container's lifetime. Its
`hasClaudeMdExternalIncludesApproved` persists too, and #135 measured that `claude` honours
it: a Project or Local `CLAUDE.md` may then read outside the clone. Every turn therefore also
runs with `--settings claudeMdExcludes`, an allow-list of the workspace and the account's own
user memory, so that approval reaches nothing outside them -- except through a symlink in the
clone, which claude resolves after matching the exclusion, and which is the recorded residual.
And the clone's *own* `.git/config` and `.git/hooks/`, which are the other side of the tool
config line above and are not swept: they are the session's files in the session's workspace,
they belong to one issue, and the next session on *that* issue inherits them along with the
rest of the clone ([How long a workspace lives, and what a reused one hands the next
session](#how-long-a-workspace-lives-and-what-a-reused-one-hands-the-next-session) above,
which says why).
The agent's environment is otherwise minimal —
`PATH`, the `ANTHROPIC_*`, `CLAUDE_*` and `GIT_AUTHOR_*`/`GIT_COMMITTER_*` variables and
`GH_TOKEN`, with `HOME`/`USER`/`LOGNAME` the account's own; nothing else from `.env` reaches
it — but that allow-list, the workspace and the protected-key list are conveniences, not the
sandbox: the container and the uid are. On the host route (`agent.run_as` unset, `validate`
warns) the session runs as your own user with none of this, which is why the container is
the supported deployment. Keep it in the container and give it a repository-scoped token.
The dashboard is a third account: compose runs the `web` service as `web` (uid 1002), which
takes HTTP from a browser, needs no privilege transition and so has none — it cannot execute
`sudo` at all, and neither the worker's home nor the session's is readable from it. It
asks for its password on every request, so placement hardens it rather than standing in
for it: keep it on loopback all the same, or put TLS and rate limiting in front of it,
because HTTP Basic sends the password with every request and the app itself limits no
attempts.
