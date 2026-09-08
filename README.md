# issuebot

[![CI](https://github.com/jleavers/issuebot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/jleavers/issuebot/actions/workflows/ci.yml?query=branch%3Amain)

Bespoke version of https://github.com/openai/symphony using Claude instead of Codex, GitHub instead of Linear, and with the addition of Slack messaging and a web dashboard.

## How To Get Started

issuebot is a worker you run somewhere (a laptop, a server, Docker) that watches one GitHub
repository. You put the `issuebot/todo` label on an issue; the worker clones the repository,
runs Claude Code on the issue unattended, pushes a branch, opens a pull request and moves the
issue to `issuebot/review`. You review the PR like any other. Merging it closes the issue and
the worker marks it `issuebot/complete`; labelling it `issuebot/rework` sends it back to the
agent with your review comments.

### What is configured where

- **One `WORKFLOW.md` is one worker watching one repository.** Nothing is installed in the
  target repository: it only needs the five `issuebot/*` labels, which `issuebot labels ensure`
  creates. To work on several repositories, run one self-contained stack per repository
  (see "More than one repository" below); there is no shared dashboard.
- `WORKFLOW.md` has two parts. The YAML front matter is the configuration; everything after
  it is the prompt the agent receives, a Jinja2 template that works unchanged for any
  repository. Secrets never go in the file: a field is either omitted (and the well-known
  variable is used) or set to `$VAR`.
- `.env` (copied from `.env.example`, git-ignored) holds the secrets and the identity the
  agent commits with. Docker Compose loads it for the worker; on the host you export the
  variables yourself.
- The agent follows the target repository's own `CLAUDE.md` and `AGENTS.md` for how to run
  tools, commit and open PRs, and with `claude.setting_sources: [project]` it also loads that
  repository's `.claude/settings.json`. So the target repository shapes the agent's behaviour;
  `WORKFLOW.md` owns the labels and the process.

### Prerequisites

1. **A GitHub token** for the account the agent will act as. Every commit, PR and comment
   appears under that account, so a dedicated bot account is a good idea. Create a fine-grained
   personal access token restricted to the target repository with Contents, Issues and
   Pull requests set to read and write, plus Commit statuses read (for CI that posts commit
   statuses rather than Actions check runs); Metadata read is mandatory and the UI adds it for
   you. Do not go looking for a Checks permission: fine-grained tokens
   [cannot call the Checks API](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#limitations-of-fine-grained-personal-access-tokens),
   so it is not in the list. Contents read is what lets `gh pr checks --watch` read a PR's check
   rollup, so it works with the permissions above. If the agent may edit files under
   `.github/workflows/`, also grant Workflows — read and write is its only level, and without it
   any push touching those files is rejected. A classic token with the `repo` scope works too;
   it needs `workflow` adding for the same reason. The account needs permission to push branches
   and open PRs in the target repository.
2. **Claude access**: an Anthropic API key (`ANTHROPIC_API_KEY`), or a Claude Code login
   (see step 2 below for the container).
3. **Docker with Compose** for the container stack (recommended: the image bundles `git`, `gh`
   and `claude`, and Compose brings PostgreSQL for history and the dashboard). To run on the
   host instead you need [uv](https://docs.astral.sh/uv/), `git`, the
   [GitHub CLI](https://cli.github.com/) and [Claude Code](https://claude.ai/code) 2.1.259 or
   newer on `PATH`.
4. **The target repository's toolchain**, wherever the agent runs, so it can run the tests.
   The image has Python 3.14, `git`, `gh` and `claude` and nothing else; for another stack
   build an image `FROM` it and add the tools, or install them in `hooks.after_create`.

The commands below are Bash. On Windows the Compose route works as-is under Docker Desktop;
for the host route use WSL.

### Step 1: clone and configure

```bash
git clone https://github.com/jleavers/issuebot.git
cd issuebot
cp .env.example .env
```

Fill in `.env`: `GH_TOKEN`, `ANTHROPIC_API_KEY` (or leave it empty and log in once, step 2),
the four `GIT_AUTHOR_*`/`GIT_COMMITTER_*` values, and optionally `SLACK_WEBHOOK_URL`.
`ISSUEBOT_DB_PORT` and `ISSUEBOT_WEB_PORT` only matter if 5432 or 8080 is taken on your host.

Leave the two email addresses as something that is not your own. If you set them to your real
address and your account has **Keep my email addresses private** turned on, its *Block command
line pushes that expose my email* option makes GitHub reject the agent's push with
`GH007: Your push would publish a private email address` — mid-run, so the agent burns turns
retrying and the issue escalates to `issuebot/review` with an obscure cause. Your own
`ID+user@users.noreply.github.com` address pushes cleanly but attributes every agent commit to
you. For linked commits under a separate identity, use a
[machine account](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#3-account-requirements)
— GitHub's terms allow one free machine account alongside a free personal account — and its
noreply address.

Then edit the front matter of `WORKFLOW.md`. The one required change is `github.repo`; the
checked-in file points at this repository. Unknown keys are rejected, so a typo fails at
`validate` rather than being silently ignored.

| Key | What it does | Default |
|---|---|---|
| `github.repo` | `owner/name` of the repository to watch. **Required.** | |
| `github.token` | `$VAR` naming the token variable | `GH_TOKEN` |
| `github.labels.*` | the five state label names | `issuebot/todo`, `issuebot/in-progress`, `issuebot/review`, `issuebot/rework`, `issuebot/complete` |
| `polling.interval_ms` | how often GitHub is polled | `30000` |
| `workspace.root` | where per-issue clones live; `~` and paths relative to `WORKFLOW.md` are resolved | `/workspaces` (the Compose volume) |
| `hooks.after_create`, `hooks.before_run`, `hooks.after_run`, `hooks.before_remove` | Bash run inside the workspace at those moments (`after_create` is where the target repository's dependencies get installed); `hooks.timeout_ms` bounds each | none; `60000` |
| `agent.max_concurrent_agents` | issues worked on in parallel | `3` |
| `agent.max_turns` | `claude -p` invocations per run before the issue is escalated | `5` |
| `agent.max_attempts` | failed runs before the issue is escalated | `3` |
| `agent.self_review` | the agent reviews its own diff before opening the PR | `true` |
| `claude.model` | `opus`, `sonnet` or a full model id; omit for Claude Code's default | none |
| `claude.permission_mode` | how Claude Code decides what it may do; nobody can answer a prompt, so `auto` | `auto` |
| `claude.max_budget_usd` | spend cap per turn, so a run can spend it up to `agent.max_turns` times; what it should be depends on your plan (see "Cost" below) | `5.0` |
| `claude.turn_timeout_ms`, `claude.stall_timeout_ms` | a turn is killed after this long, or after this long without output | 1 hour; 5 minutes |
| `claude.setting_sources` | which Claude Code settings the agent loads (`user`, `project`, `local`) | Claude Code's default |
| `claude.allowed_tools`, `claude.disallowed_tools`, `claude.append_system_prompt` | passed straight to `claude` | none |
| `database.url` | `$VAR` naming the PostgreSQL URL; unset disables history and the dashboard | `DATABASE_URL` |
| `notifications.slack.events` | event kinds posted to Slack; `[]` silences it | `[state_changed, blocked]` |
| `server.port`, `server.bind` | where `issuebot web` listens | `8080`, `0.0.0.0` |

Leave the prompt below the front matter as it is for your first runs. It tells the agent about
the labels, the single "workpad" comment it keeps on the issue, the `issuebot/<number>-<slug>`
branch, the PR with `Closes #<number>`, the self-review and the sweep of PR comments and
checks it must clear before handing the issue to review. The variables it can use are
`issue`, `repo`, `labels`, `workpad_marker`, `attempt`, `turn_number`, `max_turns`, `rework`
and `self_review`. `validate` renders it against a sample issue;
`run-once <number> --show-prompt` renders it against a real one without running anything.

### Step 2: validate and create the labels

```bash
docker compose run --rm worker validate         # on the host: uv run issuebot validate
docker compose run --rm worker labels ensure    # on the host: uv run issuebot labels ensure
```

`validate` prints one line per check and exits non-zero on any `[FAIL]`:

```
[ OK ] workflow: /app/WORKFLOW.md
[ OK ] github.repo: your-org/your-repo
[ OK ] github.token: set (from GH_TOKEN)
[ OK ] workspace.root: /workspaces
[ OK ] claude.command: /home/issuebot/.local/bin/claude (2.1.259)
[ OK ] claude auth: logged in (claude.ai, max)
[ OK ] gh: /usr/bin/gh
[ OK ] gh auth: logged in as your-bot
[ OK ] github.repo access: your-org/your-repo (default branch main)
[WARN] github.labels: missing: issuebot/todo, ...; run issuebot labels ensure
[ OK ] database.url: connected (PostgreSQL 18.1); schema version 2
[WARN] notifications.slack: not configured; export SLACK_WEBHOOK_URL to notify on blocked, state_changed, or set notifications.slack.events: [] to silence this
[ OK ] prompt: 11314 characters, renders
13 checks: 0 failed, 2 warnings
```

`labels ensure` creates (or recolours) the five labels in the target repository; run it once
per repository. The labels warning disappears on the next `validate`.

To use a Claude Code login instead of an API key, log in once inside the container: run
`docker compose run --rm --entrypoint claude worker`, complete the login, then exit. The
login is kept in the `claude-home` volume and survives restarts and rebuilds. Alternatively
run `claude setup-token` on a machine with a browser and put the result in `.env` as
`CLAUDE_CODE_OAUTH_TOKEN`. On the host, `claude` uses whatever login you already have.

#### Checking that the login took

The `claude auth` line above is the answer: `validate` asks `claude` which credential it would
use, under the same trimmed environment the agent gets, so it reports what the *agent* will
authenticate with rather than what your shell can reach. It names the route, so you can tell
the three apart at a glance:

| Line | What it means |
|---|---|
| `logged in (claude.ai, max)` | the login in the `claude-home` volume, on a Max subscription |
| `logged in (CLAUDE_CODE_OAUTH_TOKEN)` | the token from `claude setup-token` |
| `logged in (API key from ANTHROPIC_API_KEY)` | an Anthropic API key |
| `not logged in` | nothing usable — a `[FAIL]`, because the agent cannot run |

Setting both a login and `ANTHROPIC_API_KEY` is a warning rather than an error: it works, but
which credential gets billed is not obvious from the outside, so unset one. An empty
`ANTHROPIC_API_KEY=` counts as unset, which is what you want when you have logged in.

The worker runs the same probe at startup, so a worker with no usable credential prints
`[FAIL] startup: claude auth: not logged in; ...` and exits rather than claiming issues it
cannot work on. Under Compose that means `docker compose ps` shows the worker restarting until
the login is in place; `docker compose logs worker` has the line. Only a definite "not logged
in" stops it: a `claude` that does not answer in time is logged as a warning and the worker
starts anyway.

A credential that stops working *after* startup — an expired `CLAUDE_CODE_OAUTH_TOKEN`, a
revoked API key — is caught by the run that hits it. That issue is moved to `issuebot/review`
at once with a workpad block naming authentication, rather than after `agent.max_attempts`
opaque failures, and the worker stops claiming anything else. A worker holding dispatch says
so wherever you look: `issuebot status` prints a `dispatch: held (auth) since ...` line, the
dashboard's worker line reads `worker held` with the reason, `/healthz` reports
`"worker": "held"` with the same reason under `dispatch_hold`, and `docker compose logs
worker` shows `dispatch_auth_held`. The board keeps updating while the hold lasts, since the
hold stops `claude`, not `gh`. It re-checks the credential every poll and
picks up where it left off once `claude auth status` reports a login again, so fixing the
credential is enough and no restart is needed. A `claude` that cannot answer the probe at all
holds it up for ten polls at most, and then the worker goes back to failing one issue at a
time rather than sitting idle for good.

To ask `claude` directly, without going through issuebot:

```bash
docker compose run --rm --entrypoint claude worker auth status
```

It prints JSON — `"loggedIn": true` with an `authMethod` of `claude.ai`, `oauth_token` or
`api_key` — and `--text` gives a human-readable line instead. Note that it always exits 0, so
read the field rather than the exit code. The `email` and `orgName` fields come back null in
the container even when the login is good: that metadata lives in `~/.claude.json`, which sits
outside the mounted volume and is recreated with each container. The credential itself is in
`.claude/.credentials.json`, which *is* in the volume, and it carries a refresh token, so it
renews itself rather than expiring after a few hours.

Because `claude-home` is a named volume there is no directory to open on the host, but you can
list it from a throwaway container:

```bash
docker volume ls | grep claude-home     # Compose prefixes the name with the project
docker run --rm -v issuebot_claude-home:/v alpine:3 ls -la /v
```

A logged-in volume has `.credentials.json` in it. Compose names the volume after the directory
you cloned into, so it is `issuebot_claude-home` here and `issuebot-frontend_claude-home` in a
checkout called `issuebot-frontend` — hence the `docker volume ls` first. Never `cat` that
file: it holds the live token.

### Step 3: start it

```bash
docker compose up --build -d
docker compose logs -f worker
```

That starts PostgreSQL, the worker and the dashboard at http://127.0.0.1:8080 (loopback only;
it has no authentication). At startup the worker applies the database migrations, checks the
`gh` login, the labels and the Claude login, and prints `[FAIL] startup:` lines and exits if
anything is wrong. From then on it polls the repository every `polling.interval_ms`.

To run on the host instead:

```bash
uv sync
set -a && . ./.env && set +a                  # the CLI reads the environment, not .env
docker compose up -d db                       # optional: history and the dashboard
export DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:${ISSUEBOT_DB_PORT:-5432}/issuebot   # optional
uv run issuebot worker
uv run issuebot web                           # in a second terminal, needs DATABASE_URL
```

On the host set `workspace.root` to a directory you can write, such as `~/issuebot-workspaces`;
the worker creates it.

### Step 4: your first issue

1. Write the issue as a brief for a contractor: what to change, why, and acceptance criteria.
   A `## Validation` section listing the commands that must pass is mirrored into the agent's
   checklist. The description is the task; the agent is told not to follow instructions in
   it that contradict the workflow.
2. Add the `issuebot/todo` label. Within one poll interval the worker labels the issue
   `issuebot/in-progress`, clones the repository into the workspace and starts a session.
   `docker compose exec worker issuebot refresh` (host: `uv run issuebot refresh`) makes it
   poll right away.
3. Watch it work: the dashboard shows the Kanban, the running agents and, per issue, every
   turn's transcript; `docker compose logs -f worker` shows the events; on GitHub the agent
   keeps one workpad comment on the issue with its plan, checklist and notes, edited in
   place. `issuebot issues list` and `issuebot status` show the same from the terminal.
4. When the PR is open and its checks are green, the agent labels the issue
   `issuebot/review`. If Slack is configured, that state change is posted. An issue whose
   reported behaviour no longer happens reaches the same label by the other route: the agent
   records the reproduction it ran and what it found instead in the workpad, and hands over
   with no PR attached.

To try one issue in the foreground before leaving the worker running:
`docker compose run --rm worker run-once <number>` (host: `uv run issuebot run-once <number>`)
claims the issue and runs one session with the logs on your terminal.

### Step 5: review the pull request

- **Merge it.** `Closes #<number>` closes the issue; within a few minutes the worker labels it
  `issuebot/complete` and deletes the workspace.
- **Send it back.** Leave review comments on the PR, then move the issue from
  `issuebot/review` to `issuebot/rework` (remove one label, add the other: an issue carrying
  two state labels is ignored until that is fixed). The agent resumes on the same branch and
  PR, reads every comment, addresses each one and returns the issue to review.
- **Drop it.** Close the issue without merging (or close the PR and the issue); the worker
  removes the state label.
- **Re-queue it.** Moving `issuebot/in-progress` or `issuebot/review` back to `issuebot/todo`
  is also allowed; the issue is picked up again from its existing workspace.

### Choosing the model for an issue

`claude.model` is the default for every session. Its value is passed straight to
`claude --model`, so anything that flag accepts works: an alias such as `opus` or `sonnet`, or
a full model id such as `claude-fable-5-1`.

A spec-and-plan issue may deserve a bigger model than a one-line fix, so a label can override
the default for one issue. Map the labels to models in the front matter:

```yaml
claude:
  model: opus
  model_labels:
    issuebot/model/sonnet: sonnet
    issuebot/model/fable: claude-fable-5-1
```

`issuebot labels ensure` creates those labels alongside the five state labels, so they appear
in the issue's label menu. Put one on an issue and its next session runs with that model;
issues without one use `claude.model`. `validate` warns while a label named here is missing
from the repository — until it exists nobody can apply it, so the override is configured but
unusable. The rules:

- Model labels are not state labels. They never affect the lifecycle, an issue may carry one
  at any point, and the agent neither adds nor removes them.
- Exactly one model wins. An issue carrying two labels that name different models runs with
  `claude.model`, and the worker logs `model_labels_ambiguous`.
- The label is read when the session is dispatched, so re-labelling an issue between attempts
  changes the model the next attempt runs with.
- `claude.max_budget_usd` is per turn and does not vary by model: a cheaper model is not a
  smaller cap, and a run of `agent.max_turns` turns can spend it once per turn.

The model each turn actually ran with is recorded and shown on the dashboard's issue page,
which is worth checking after the first run with a new label.

For one session without touching labels, `issuebot run-once <number> --model <name>` overrides
both the label and the default.

### More than one repository

Each worker is self-contained: one `WORKFLOW.md`, one workspace root, one database and one
dashboard, and nothing is shared between workers. The database has no repository column
(issues are stored by number, the worker's runtime snapshot is a single row, and
`issuebot refresh` wakes every worker listening on that database), so two workers must not
share one. The dashboard reads one database and shows exactly the repository its
`WORKFLOW.md` names, so each repository gets its own dashboard on its own port. A combined
view across repositories is on the "later" list in the
[phased design](docs/superpowers/specs/2026-09-02-issuebot-phased-design.md) and would need
schema changes.

With Compose the simplest setup is one checkout per repository:

```bash
git clone https://github.com/jleavers/issuebot.git issuebot-frontend
git clone https://github.com/jleavers/issuebot.git issuebot-backend
```

Compose names the project after the directory, so each checkout gets its own `db`, `worker`
and `web` containers and its own `pgdata`, `workspaces` and `claude-home` volumes. In each
directory set `github.repo` in `WORKFLOW.md`, and give its `.env` distinct `ISSUEBOT_DB_PORT`
and `ISSUEBOT_WEB_PORT` values (say 5432 and 8080 for one, 5433 and 8081 for the other).
Everything else can be identical, including `GH_TOKEN` when one token covers both
repositories. The dashboards are then at http://127.0.0.1:8080 and http://127.0.0.1:8081.
Because `claude-home` is per project, a Claude Code login has to be repeated for each stack;
an API key or `CLAUDE_CODE_OAUTH_TOKEN` in each `.env` avoids that. A single checkout can
drive several stacks with `docker compose -p <name>` and an override file that swaps the
`WORKFLOW.md` mount and the `env_file`, but one directory each is easier to reason about.

On the host, run one `issuebot worker` and one `issuebot web` per repository, each with its
own `--workflow` file (or `ISSUEBOT_WORKFLOW`), its own `workspace.root`, its own database
and its own `server.port` (or `web --port`). A second database on the same PostgreSQL server
is fine: `docker compose exec db createdb -U issuebot issuebot_backend`, then point that
worker's `DATABASE_URL` at `.../issuebot_backend`; it creates the tables on first start.

### When things go wrong

- **Blocked.** If the agent hits a true external blocker (a missing tool, credential or
  permission), or a run exhausts `agent.max_turns` or `agent.max_attempts`, the worker moves
  the issue to `issuebot/review` with a Blockers section in the workpad. Fix the cause, then
  label it `issuebot/rework` or `issuebot/todo` to retry.
- **Cost.** Every turn is capped by `claude.max_budget_usd`, so one run's ceiling is that
  times `agent.max_turns` — `5.0` and `5` mean up to $25 before the issue is escalated. The
  right value is yours to pick and the checked-in `5.0` is only a starting point: on an API
  key it is real money and a tight cap is a real guard, while on a Claude subscription there
  is no per-token charge and the cap acts as a cheap-and-cheerful effort limit instead, so a
  larger number costs nothing but a longer leash. A turn that hits the cap ends as
  `budget_exceeded` and counts as a failed attempt. The dashboard and the `run_ended` Slack
  line (opt in via `notifications.slack.events`) show each run's cost.
- **Restarts.** Workspaces persist in the `workspaces` volume; on startup the worker resumes
  issues that were `issuebot/in-progress` from where they stopped.
- **Configuration changes.** A running worker re-reads `WORKFLOW.md` when it changes.
  `database.url`, the Slack webhook and its event list are read once at start, so those need
  `docker compose restart worker`.
- **Upgrades.** `WORKFLOW.md` is mounted into the container, but the code is baked into the
  image: after pulling a new version of issuebot, run `docker compose build` (or
  `docker compose up --build -d`) before anything else. A setting that a newer `WORKFLOW.md`
  introduces fails against a stale image at `validate`, as
  `<key>: Extra inputs are not permitted`.
- **Safety.** The agent runs with no permission prompts and may run anything inside its
  workspace. Keep it in the container, give it a repository-scoped token, and keep the
  dashboard on loopback. The agent's environment is minimal: `PATH`, `HOME`, the
  `ANTHROPIC_*`, `CLAUDE_*` and `GIT_AUTHOR_*`/`GIT_COMMITTER_*` variables and `GH_TOKEN`;
  nothing else from `.env` reaches it.

## Development

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.14 for you) and,
for the container stack, Docker with Compose.

```bash
uv sync
uv run pytest
uv run issuebot validate          # checks ./WORKFLOW.md and the environment
uv run issuebot validate --slack-probe   # same, plus one test message to the Slack webhook
uv run issuebot labels ensure     # once per repository: creates the issuebot/* labels
uv run issuebot run-once 42       # one agent session for issue #42, in the foreground
uv run issuebot worker            # the long-running orchestrator; Ctrl-C stops it
uv run issuebot migrate           # apply the database migrations (worker does this at start)
uv run issuebot status            # what the worker was doing at its last tick
uv run issuebot stats             # issues closed and agents run: last day, week, per day
uv run issuebot refresh           # make a running worker poll GitHub now
uv run issuebot web               # the dashboard and its JSON API (needs DATABASE_URL)
cp .env.example .env              # then fill in GH_TOKEN and Claude auth
docker compose up --build         # postgres:18 + worker + web (http://127.0.0.1:8080)
```

History is optional: with `DATABASE_URL` set (compose sets it for the worker; on the host
export `postgresql://issuebot:issuebot@127.0.0.1:${ISSUEBOT_DB_PORT:-5432}/issuebot` after
`docker compose up -d db`) the worker records every event, run and issue snapshot in
PostgreSQL and `status`, `stats` and `refresh` work; without it the worker runs exactly as
before. The worker applies pending migrations when it starts and fails fast if the database
is configured but unreachable; `validate` reports the schema version. The tests that need a
database read `DATABASE_URL` and are skipped when it is unset.

The dashboard (`issuebot web`; the compose `web` service publishes it on the host's loopback
at `ISSUEBOT_WEB_PORT`, default 8080) shows the Kanban of the five label columns, the hero
stats, two 30-day charts, the running agents and, per issue, its runs with the transcript of
every captured turn; `/api/v1/state`, `/api/v1/issues/<n>`, `/api/v1/stats?window=7d` and
`POST /api/v1/refresh` serve the same as JSON and `/healthz` reports the database and the age
of the worker's last report. It needs `DATABASE_URL` and nothing else, reads `WORKFLOW.md`
once at start, and has no authentication: keep it on loopback (`server.bind: 127.0.0.1` outside
Docker) or behind a reverse proxy. Turn logs are captured into the database when a run ends,
so they outlive the workspace.

Slack notifications are optional: export `SLACK_WEBHOOK_URL` (an incoming webhook,
`https://hooks.slack.com/services/...`) and choose the event kinds in `WORKFLOW.md` under
`notifications.slack.events` (default `state_changed` and `blocked`; add `run_ended` for a
line per run with its cost). The worker reads both at start, so changing either needs a
restart; `validate` warns while the variable is unset.

The design lives in [`docs/superpowers/specs/`](docs/superpowers/specs/); start with
the phased design, then the per-phase specs and plans.

Contributors and AI agents must follow the rules in [`AGENTS.md`](AGENTS.md).
