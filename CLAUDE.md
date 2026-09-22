# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python 3.14 with `uv`; `src` layout; package `issuebot`.

```bash
uv sync                              # create .venv and install (uses uv.lock)
uv run pytest                        # tests (hermetic; no network, no Docker; DB tests skip;
                                     #   conftest pins PYTHON_COLORS=0, since 3.14 argparse
                                     #   colourises help and the shell would otherwise decide)
uv run pytest tests/test_cli.py -k validate   # one file / one pattern
docker compose --profile test up -d --wait test-db   # a throwaway postgres:18 on an ephemeral port
                                     #   (needs ISSUEBOT_DB_PASSWORD set in .env, any value: see below)
DATABASE_URL=postgresql://issuebot@$(docker compose port test-db 5432)/issuebot uv run pytest
docker compose rm -sf test-db        # throw it away (not `compose down`: that is project-wide)
docker compose up -d db              # the long-lived db instead, on ISSUEBOT_DB_PORT (5434 here)
uv run ruff check . && uv run ruff format --check .
uv run pre-commit run --all-files    # whitespace, yaml, ruff (same as CI lint job)
uv run issuebot validate             # load ./configs/WORKFLOW.md and check the environment
                                     #   (the container runs the session as a session account --
                                     #    by default the built pool, `agent-1` .. `agent-N` at
                                     #    uids 1011 up, else `agent` at 1001 -- and the
                                     #    worker as uid 1000 `issuebot`; #75, #121, agent.run_as; and
                                     #    compose runs the dashboard as uid 1002 `web`, which
                                     #    cannot invoke sudo at all; #102)
uv run issuebot validate --slack-probe   # same, plus one test message to the Slack webhook
uv run issuebot labels ensure        # create/update the state labels and markers in github.repo
uv run issuebot issues list          # table of open issues carrying a state label
uv run issuebot run-once <number>    # one worker session in the foreground (--show-prompt renders only)
uv run issuebot worker               # the long-running orchestrator; SIGTERM or Ctrl-C stops it
uv run issuebot migrate              # apply pending .sql migrations (worker and run-once do it too)
uv run issuebot status               # the worker's last runtime snapshot, read from the database
uv run issuebot stats [--days N]     # issues closed and runs started: 1d, 7d and per day
uv run issuebot refresh              # NOTIFY the repository's channel: its worker polls at once
                                     #   (at most one refresh-driven tick every 5 s)
uv run issuebot web [--port N] [--bind HOST]   # the dashboard and the JSON API (needs DATABASE_URL and
                                     #   ISSUEBOT_WEB_PASSWORD, reads no workflow; binds 127.0.0.1 by default)
uv run issuebot egress [--port N] [--bind HOST]   # the allow-listing CONNECT proxy the worker's egress
                                     #   goes through (#126; reads ISSUEBOT_EGRESS_ALLOW, no workflow,
                                     #   no credential; compose runs it as the `egress` service)
docker compose build                 # image: git, gh, claude, app venv
                                     #   (+ a PostgreSQL server when ISSUEBOT_POSTGRES_VERSION is set,
                                     #    + node and npm when ISSUEBOT_NODE_VERSION is set,
                                     #    + uv when ISSUEBOT_UV_VERSION is set,
                                     #    + pwsh when ISSUEBOT_PWSH_VERSION is set)
docker compose up                    # db + web (profile hub) + worker + egress (profile worker),
                                     #   COMPOSE_PROFILES in .env
                                     #   (http://127.0.0.1:${ISSUEBOT_WEB_PORT:-8080})
docker network create issuebot && docker network create --internal issuebot-internal
                                     # once per host, before any of the above (#126): the worker
                                     #   joins internal networks alone, so its only route off the
                                     #   host is the egress proxy
```

**Run the DB tests against `test-db`, not against the long-lived `db`.** It sits behind a
`test` profile, so a plain `docker compose up` never starts it; its cluster is tmpfs, so
nothing survives the container; and it publishes an *ephemeral* host port, so it cannot
collide with `db` or with the other projects on this host. `docker compose port test-db
5432` reads back the port Docker chose -- and that 5432 is the port *inside* the container,
where postgres listens whatever the host publishes. Throw it away with `docker compose rm
-sf test-db`, **not** `docker compose down`: `down` is project-wide and would stop the live
`db`, `worker` and `web` too.

**The store's password is `ISSUEBOT_DB_PASSWORD` in `.env`, and nothing in the tree is a
working credential (#78).** `db`'s `POSTGRES_PASSWORD` and the `DATABASE_URL` of `worker` and
`web` are `${ISSUEBOT_DB_PASSWORD:?...}`, a required substitution with no default, so compose
refuses all three, by name, while it is unset or empty; `.env.example` ships the key empty. The
`docker` CI job proves both directions under every profile, and
`tests/test_compose_credentials.py` pins the shape without Docker. Compose interpolates the
whole file before it filters by profile, so even `--profile test` needs the variable *set*,
though `test-db` never reads it: that cluster is a throwaway on tmpfs behind a loopback port and
authenticates with `trust` (#62's choice for the workspace cluster), which is why its DSN above
names no password, as does the CI `test` job's service. No operator-facing file holds a working
one either, for the same reason: a DSN in the prose is the `${ISSUEBOT_DB_PASSWORD}`
placeholder over the variable, as the hub's is in `README.md`, or it carries no password at all,
as the `trust` throwaway's does in `CONTRIBUTING.md` and `docs/toolchains.md`. That is the rule
`tests/test_compose_credentials.py` checks, and it checks it over the whole of
`OPERATOR_FACING` -- those three prose files, `docs/operations.md`, which documents the rotation
without spelling a DSN, and `compose.yaml`, `.env.example`, this file, the `Dockerfile` and the
workflows beside them -- so a claim made about the README alone is narrower than what is
enforced, and it is the narrow claim an editor would act on. The image applies the password at
initdb only; a cluster that already exists is rotated with `ALTER ROLE`
([`docs/operations.md`, "Rotating the database
password"](docs/operations.md#rotating-the-database-password)).

**Do not pass `ISSUEBOT_DB_PORT=...` inline to `docker compose`.** That is the long-lived
`db`'s port, and it belongs to the project's env file (5432 in `.env.example`, 5434 on this
host) where compose reads it on its own. An inline value that disagrees with the configured
one is a different *published* port, so compose recreates the `db` container -- which may be
live and serving the worker and the web. 5434 is neither arbitrary nor a collision: this
host runs a database per project, and `docker ps` shows 5432, 5433 and 5435 held by three of
the others. Leave it there.

CI (`.github/workflows/ci.yml`) runs lint, tests (with a postgres:18 service) and, in the
`docker` job, a "compose config under each profile" step -- `docker compose config --quiet`
under `COMPOSE_PROFILES=hub`, `worker` and `hub,worker`, so a profile typo fails a PR --
before the Docker build, on every PR. That job builds the image twice (#62, #64): the default
one, which must carry no `initdb`, `node`, `npm`, `uv` or `pwsh`, and a second,
`issuebot:ci-toolchain`, with `POSTGRES_VERSION=18`, `NODE_VERSION=24`, `UV_VERSION` and
`PWSH_VERSION` in its own `type=gha` cache scope (one build,
not two: the checks are about what is on `PATH` and under which uid, not about the arguments
interacting), which must answer `initdb --version`, `node --version`, `npm --version`,
`uv --version` and `pwsh --version` on its own `PATH` and in a login shell, still run as
`issuebot`, run one `npm ci` over a
dependency-free fixture as `issuebot` with the registry pointed at a dead port (so the writable
`$HOME/.npm` and the wrapper's own shebang are what is proved, not the network), run one `.ps1`
as `agent` whose assertion is a culture-formatted number (so the ICU the PowerShell arm
installs beside the runtime is proved, not just that the binary answers), and survive
the documented cluster recipe -- the three hook scripts are parsed out of
`docs/toolchains.md`, where #196 moved them from the README, and run
inside the image under `bash -lc`, so a recipe that stops working fails a PR rather than a
session. Both builds must also report `LANG=C.UTF-8` under `sh -c` and under `bash -lc` with
`LC_ALL` unset, and a bare `initdb` in the opt-in one must land on `UTF8` (#66): the base
image sets no locale, on `C` a cluster comes out `SQL_ASCII`, and pinning the encoding
catches that rather than the variable that happens to produce it.

A red check on a pull request here is not always this repository's code: when every failed
job reports zero steps, Actions declined to run the job at all, and the fix is the account's
rather than the diff's -- [`docs/operations.md`, "Checks that never
ran"](docs/operations.md#checks-that-never-ran) is how to tell the two apart and what it
means for an issue that is otherwise finished.

Dependabot covers uv, Docker and Actions weekly.
Every `uses:` in the workflows and every `rev:` in `.pre-commit-config.yaml` is a commit
digest with its tag beside it (#111; `tests/test_pins.py` refuses a tag): a tag is a name its
owner can repoint, and these run in CI and on any host that runs `pre-commit`, with `GH_TOKEN`
and the store's DSN ambient. Dependabot's `github-actions` ecosystem moves the digest pins
(it rewrites the digest and the comment); `pre-commit-version.yml` is the same weekly bump
job for the hook pins that `claude-code-version.yml` is for the claude pin -- `pre-commit
autoupdate --freeze`, the hooks over the tree as the proof, one PR from a branch named for
the config's blob hash so a rerun finds its own. Both bump jobs recognise their own pull
request by provenance and never by the branch name, which any fork can carry: REST
`pulls?head=<owner>:<branch>`, kept only when `head.repo.full_name` is this repository and
`user.login` is `github-actions[bot]`. Both are also two jobs rather than one (#129 for the
hooks, #138 for the claude pin), because a bump job has to *execute* the referent it is
proposing -- the hooks at their new digests, the claude release at its new version -- and
that is third-party code nobody has reviewed yet, which is the whole reason the digests
exist. So the half that executes holds read scopes only and checks out with
`persist-credentials: false`, leaving no pushable `GITHUB_TOKEN` in `.git/config` for it to
read out; it hands its result over as an artefact, and the half holding `contents: write`
re-checks that artefact's shape, commits, pushes and opens the PR while building and running
none of it. `tests/test_pins.py` pins that split for both.
`claude-code-version.yml` covers what Dependabot cannot see: weekly, its `build` job compares
the Dockerfile's `CLAUDE_CODE_VERSION` with npm's `dist-tags.latest`, writes the new pin into
the file and builds *that* file (no `--build-arg`, which would prove itself instead), then
runs `claude --version` out of the image as the proof; `open-pr` refuses to open anything
unless the tree it proposes carries the resolved pin -- the downloaded `Dockerfile` for a new
branch, where the copy must also move that one line alone, and the branch's own for one it
reuses. `MIN_CLAUDE_VERSION` (`agent/runner.py`) is a compatibility floor, not the shipped
version, and moves by hand.

## Package layout

Python 3.14, `src` layout, package `issuebot`. Every module, what it holds and why it holds
that shape, is [`docs/package-layout.md`](docs/package-layout.md) -- `issuebot.config`,
`log`, `pipes`, `invocation`, `dsn`, `egress`, `events`, `github`, `agent`, `orchestrator`,
`notifications`, `db`, `web` and `cli`, in that order, with the design documents it draws on.

Each module is a `##` heading of its own, so read *one* rather than the file: ~134 KB is
roughly 33k tokens, and one entry is a fraction of that (#221). The heading is the module's
dotted name in backticks under a `##`, which is what you search the file for from the working
tree, and the anchor is that name with the dot dropped, as GitHub slugs it:
[`docs/package-layout.md#issuebotegress`](docs/package-layout.md#issuebotegress),
`#issuebotorchestrator`, and so on for all fourteen.

It is not in this file because it would not reach you here: issuebot cuts each instruction
file at `INSTRUCTION_FILE.limit` (128 KiB, `src/issuebot/agent/boundary.py`) before handing it
to a session, and at 120 KB the layout took the file past that cap, so what was silently
dropped was everything after it -- the doc map below, the operational rules and the PR
convention (#211). Read it from the working tree when you need a module's design; do not
copy it back here.

## The files beside this one

Prose in this repository has one home each, and the homes have moved (#196, #199, #200, #201).
A change to operator documentation therefore starts by choosing the file, not by opening the
README and writing there: the README is the front door and most of what used to sit behind it
now does not, so an edit made in the wrong file either lands where nobody reads it or becomes a
second copy of a section that has already moved, and the two then drift.

This file has a *budget* as well as a scope, and the budget is what nearly cost the map its
own readers (#211). issuebot reads `CLAUDE.md` and `AGENTS.md` out of the clone and carries
them to the session as enveloped data, cut at `INSTRUCTION_FILE.limit` -- 128 KiB, declared in
`src/issuebot/agent/boundary.py` -- and a cut is never a failure: the file renders whole on
GitHub and in an editor, and only a session sees the end missing. At 143,136 bytes this file
was 12 KB over, and the cut fell 1,384 bytes *before* this section's own heading, inside the
package layout above it -- so the whole map went, every entry below and the two paragraphs
introducing them, along with "What issuebot is", the operational rules, the security-sweep
note and "Creating PRs", which is where a session is told to open its pull request through
the REST API rather than `gh pr create`. Where the cut lands moves with the file rather than
staying put: 3 KB earlier in its growth, at 140,452 bytes, it fell inside the *first* entry
below, taking the ten after it and leaving the map's heading -- which is the point, since
nothing tells a reader which of the two they are looking at.
Adding prose here therefore spends a budget, and
`tests/test_instruction_bounds.py` is what says so out loud: it fails while there is still
16 KiB of headroom, so the warning arrives in CI, before the cut, rather than in a prompt
nobody reads. Reference detail belongs in a file beside this one; what stays here is what a
session needs *before* it knows which file to open.

- `README.md`: the front door -- what issuebot is, the label state machine, the quick start,
  the five-step setting-up guide, the configuration reference (every setting, the prompt and
  its variables, model labels) and Development. Four of its passages are pinned by
  `tests/test_readme_bounds.py`, which pins *structure* rather than behaviour (#74). Each of the four is a choice point that widens the session's
  reach and cannot be defaulted shut -- Workflows write on the token, the classic `repo`
  token, the host route, and the Claude credential -- so the note saying which boundary the
  choice removes *is* the enforcement, and it has to be legible where the reader acts rather
  than in a Safety bullet they reach later or not at all. The test holds each consequence
  within `POINT_OF_USE` (700 characters) *after* its incentive, and each qualifier in the
  incentive's own block; matching is over whitespace-collapsed text, so rewrapping one of
  those paragraphs is free and rewording one is not. It is recorded here for the same reason
  `test_pins.py`, `test_web_vendor.py` and `test_compose_credentials.py` are: a session
  tightening the prose in Prerequisites or Development will fail it, and the failure names a
  phrase rather than a rule.
- `docs/package-layout.md`: every module of `issuebot` -- what it holds and the reasoning
  behind the shape it has -- which #211 moved out of this file's `## Package layout` section,
  where it was 120 KB of a 143 KB whole and so the reason nothing after it reached a session.
  A module's design is reference a session reads once it knows it needs that module, which is
  what makes it the half that moves; the pointer above is what stays. Add to it there, not
  here -- and note that it has a budget of its own in the same test, not because anything cuts
  it but because the growth that took this file over moved into it, and the destination of an
  overflow is the last file anyone thinks to measure. When it fails, split it per module.
- `docs/toolchains.md`: the *target* repository's toolchains, which #196 moved out of the
  README -- PostgreSQL, Node, uv and PowerShell as the opt-in image build arguments, the hook
  recipes the CI `docker` job parses out and runs, and `.issuebot/env`, what a hook hands the
  agent. Anything about what a session's own tests need to run belongs here.
- `docs/operations.md`: running a deployment -- more than one repository against one store,
  "Rotating the database password", and "When things go wrong" (Blocked, GitHub itself, Checks
  that never ran, Cost, Restarts, How long a workspace lives, Configuration changes, Upgrades,
  Safety). The recovery an operator performs on a blocked or over-budget issue is documented
  there and nowhere else, as is the difference between a check that failed and one Actions
  never ran.
- `docs/dashboard.md`: the web surface -- what it serves, who may read it, the Basic gate and
  a browser that will not speak it, the hero's six tiles, and what "issues closed" counts.
- `docs/security-model.md`: how a session is bounded, for a reader deciding whether to trust
  one -- what it may reach (the egress allow-list and `ISSUEBOT_EGRESS_ALLOW`), one account
  per concurrent session, and checking that the credential took. The reasoning behind those
  bounds is in this file; that one is the operator's view of the same line.
- `docs/BLUEPRINT.md`: the full requirements, and `docs/superpowers/` the designs and plans
  behind them -- `specs/`, the phased design and one spec per phase, and `plans/`, one
  implementation plan per phase. That pair used to be named at the end of the package layout
  in this file, so "above" was where this entry pointed; #211 moved that section out, and the
  pointer now sits at the top of [`docs/package-layout.md`](docs/package-layout.md) beside
  the modules it belongs to.
- `CONTRIBUTING.md`: how a change gets in -- getting set up, pull requests, the two
  conventions a contributor trips over (digest pins; the README's images are generated), how
  to report a security issue instead, and the licence contributions are accepted under. Since
  #201 it also says how each kind of contributor reaches `main`: from a fork without write
  access, and from a branch here with it, where the ruleset below is what makes the pull
  request the only route.
- `SECURITY.md`: the vulnerability policy -- private reporting rather than a public issue,
  particularly because this repository's issues are read by an agent that acts on them, and
  what is in scope and what is not.
- `LICENSE`: the repository is Apache-2.0 since #196, declared in `pyproject.toml` as
  `license = "Apache-2.0"` with `license-files = ["LICENSE"]` (PEP 639, so the metadata and
  the file cannot drift apart). No source file in the tree carries a header, so that one file
  is the whole declaration, and a session adding a dependency or vendoring a file is deciding
  whether its licence can sit under this one. The two vendored front-end libraries are the
  standing exception and keep their own -- htmx 0BSD, Chart.js MIT -- each with its licence
  file beside it under `src/issuebot/web/static/vendor/`, which that directory's
  `README.md` records and `tests/test_web_vendor.py` checks.
- `.github/ISSUE_TEMPLATE/`: `bug_report.yml` and `feature_request.yml`, the forms a reporter
  fills in, part of the community health files #199 added. They are not cosmetic here: the
  body they produce is the body a session is handed as `issue.body`, so a field added or
  reworded changes what every prompt carries. `bug_report.yml` opens by saying that issues
  here are public and an agent reads and acts on them, and sending a vulnerability to
  `SECURITY.md` instead; `feature_request.yml` opens by saying the issue is a brief for
  whoever picks the work up, which may be issuebot itself.
- `tools/screenshots/` and `docs/images/`: the README's two images,
  `docs/images/dashboard.png` and `docs/images/issue-journey.gif`, are *generated* --
  `tools/screenshots/capture.py` drives a real dashboard serving the fabricated data
  `seed.py` invents (the placeholder `acme/frontend`, invented issues, runs, costs and
  tokens), so nobody's repository names or issue titles reach a public file. They are the one
  artefact here that nothing holds to the code: change the web templates, the board's layout
  or `app.css`'s `--bg`/`--ink`/`--muted`/`--line` tokens, and the images quietly describe a
  dashboard that is gone while every test still passes. That is why `CONTRIBUTING.md` makes
  regenerating them part of such a change rather than a follow-up, and
  `tools/screenshots/README.md` is the recipe (Playwright's
  chromium once per machine; the committed images are the *dark* theme, and `PALETTES` in
  `capture.py` carries those same tokens for the caption strip it draws itself; each file must
  stay under 500 KB, which `check-added-large-files` enforces and the capture checks first so
  the rejection comes before the commit). #199 and #200 are where the images and their dark
  theme came in.

## What issuebot is

A bespoke reimplementation of [openai/symphony](https://github.com/openai/symphony)
built on Claude + GitHub instead of Codex + Linear. Full requirements live in
`docs/BLUEPRINT.md`; the essentials:

A service watches for new GitHub issues, picks them up, works on them
autonomously via `claude -p` in auto mode, opens a PR for human review, and files
follow-up issues where needed.

**Issue lifecycle is driven by labels**, and the label is the state machine —
anything reading or writing issue state goes through these:

| Label | Set by |
|---|---|
| `issuebot/todo` | human |
| `issuebot/in-progress` | agent, when work starts |
| `issuebot/review` | agent, when PR opened or no fault found |
| `issuebot/rework` | human, if the PR needs more work; or issuebot, when the PR conflicts with the default branch (bounded by `agent.max_conflict_reworks`) |
| `issuebot/no-fault` | agent, beside `review`, when it found no fault (a marker, not a state) |
| `issuebot/complete` | automatically, when the issue closes via linked-PR merge or with `issuebot/no-fault` |

GitHub is reached through the `gh` CLI, not a REST/GraphQL client library.

Two surfaces beyond the worker: a **web dashboard** (Kanban of the label columns
above, plus hero stats — issues closed in 1d/7d, agents spun up, as both
point-in-time numbers and time series) and **Slack notifications** on status
change. Persisting the time-series history is what PostgreSQL is for.

Planned infrastructure: Docker, Python 3.14, PostgreSQL, GitHub Actions CI
(lint, tests, docker build) and Dependabot.

## Operational rules (from AGENTS.md)

`AGENTS.md` is binding for agents in this repo. Read it, and note in particular:

- **Never push to `main`.** Push a feature branch and open a PR for human
  review. Never merge or close PRs — that is a human action. Since #201 this is
  no longer only a convention: `main` carries a repository ruleset that refuses
  a direct push, a force push and a deletion, with no bypass for admins, so the
  push fails at the server rather than landing and being noticed afterwards.
  `AGENTS.md` records it and says how to read the rejection — the rule working,
  not an obstacle to route around — and the remedy is the same one this bullet
  already gives, a feature branch and a PR. A contributor without write access
  reaches `main` the same way, through a fork and a PR (`CONTRIBUTING.md`).
- **Never run destructive commands**: `rm -rf`, `git reset --hard`,
  `git clean -fd`.
- **The repo is used from both Windows and Linux hosts.** Detect the OS before
  emitting shell commands or scripts: Bash/`.sh`/`&&` on Linux/macOS,
  PowerShell/`.ps1`/`;` on Windows. Never generate `.ps1` on Linux or `.sh` on
  Windows.
- `.pre-commit-config.yaml` must exist, with `trailing-whitespace` and
  `end-of-file-fixer` from pre-commit-hooks. If any `.tf` files are added, also
  wire up `terraform_fmt`, `terraform_validate` and `terraform_tflint` from
  antonbabenko/pre-commit-terraform.

## Security sweeps

`/security-sweep` audits `origin/main` — not the local checkout — in a throwaway detached
worktree, with four threat-model lanes (`copycat`, `secrets`, `hostile-issue`, `services`)
behind independent refuters, clusters what survives by root cause, and files only the clusters
a human approves. Run artefacts land in `.claude/security-sweeps/<UTC stamp>/` and are
git-ignored: a report names weaknesses that are not fixed yet, so the public record is the
issues the approval gate files, not the report. The skill is
`.claude/skills/security-sweep/SKILL.md`, the fan-out is
`.claude/workflows/security-sweep.js`, and the design is
`docs/superpowers/specs/2026-09-13-security-sweep-design.md`.

A session that has just edited the workflow cannot invoke it by name — the registry is read
once at session start — so use `Workflow({scriptPath: ...})` there.

## Creating PRs

Per the user's global instructions, set PR title/body via the REST API rather
than `gh pr create`/`gh pr edit` (the CLI hits a deprecated `projectCards`
GraphQL field and aborts here); a PreToolUse hook enforces this. Write the body
to a temp `.md` file in a **separate** Bash call from the `gh api` call, and pass
it with capital `-F body=@file.md`.
