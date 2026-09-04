# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python 3.14 with `uv`; `src` layout; package `issuebot`.

```bash
uv sync                              # create .venv and install (uses uv.lock)
uv run pytest                        # tests (hermetic; no network, no Docker)
uv run pytest tests/test_cli.py -k validate   # one file / one pattern
uv run ruff check . && uv run ruff format --check .
uv run pre-commit run --all-files    # whitespace, yaml, ruff (same as CI lint job)
uv run issuebot validate             # load ./WORKFLOW.md and check the environment
uv run issuebot validate --slack-probe   # same, plus one test message to the Slack webhook
uv run issuebot labels ensure        # create/update the five state labels in github.repo
uv run issuebot issues list          # table of open issues carrying a state label
uv run issuebot run-once <number>    # one worker session in the foreground (--show-prompt renders only)
uv run issuebot worker               # the long-running orchestrator; SIGTERM or Ctrl-C stops it
docker compose build                 # image: git, gh, claude, app venv
docker compose up                    # db (postgres:18) + worker (issuebot worker)
```

CI (`.github/workflows/ci.yml`) runs lint, tests (with a postgres:18 service) and
a Docker build on every PR. Dependabot covers uv, Docker and Actions weekly.

## Package layout

- `issuebot.config`: `load_workflow(path)` → `Workflow(config: Settings, prompt_template,
  raw_config, path, source_mtime_ns)`. Front matter → `$VAR`/`~`/relative-path
  resolution (`resolve.py`, designated fields only) → pydantic `Settings`
  (`settings.py`, `extra="forbid"`). Errors are `ConfigError` subclasses with a `code`.
- `issuebot.log`: `configure_logging()` (structlog, JSON to stderr by default),
  `get_logger()`, `bind_issue_context()`, `bind_session_context()`, `clear_context()`.
- `issuebot.events`: frozen dataclass events (`EVENT_KINDS`), `EventBus.publish()`
  (synchronous, sink failures isolated and counted), `LogSink`.
- `issuebot.github`: `StateLabel` roles and the transition table (`state.py`); frozen
  `Issue`/`LinkedPr`/`Comment` records (`models.py`); `GitHubAdapter` protocol (async);
  `GhCliAdapter` (GraphQL reads via `gh api graphql`, writes via `gh issue edit`,
  `gh label create`, `gh api`; `GhRunner` is the only subprocess boundary); `FakeGitHub`
  for tests (same normaliser, GitHub-like semantics, `fail_next`, `calls`).
- `issuebot.agent`: `WorkspaceManager` (sanitised keys, containment, `gh repo clone --depth 1`,
  `bash -lc` hooks with timeout, `.issuebot/session.json`); `PromptRenderer` (Jinja2
  `StrictUndefined`; variables `issue`, `repo`, `labels`, `workpad_marker`, `attempt`,
  `turn_number`, `max_turns`, `rework`, `self_review`); `ClaudeRunner` (`claude -p
  --output-format stream-json --permission-prompts none`, prompt on stdin, minimal
  environment, silence timeout, SIGTERM then SIGKILL, per-turn logs under
  `.issuebot/runs/<run_id>/`); `run_session` (turns, refresh between turns, `RunResult`,
  publishes `RunStarted`/`RunEnded`). Runtime turn events go to a `TurnObserver`, not the bus.
  Tests use `tests/fakes/claude` (replays `tests/fixtures/claude/*.jsonl`).
- `issuebot.orchestrator`: one asyncio task owns the schedule. `state.py` (pure): `RunningEntry`,
  `RetryEntry`, `RuntimeSnapshot`, `backoff_ms` (`min(10000 * 2^(attempt-1), max_retry_backoff_ms)`,
  attempt being the one about to run), `sort_candidates` (orphaned `in_progress`, then `rework`,
  then `todo`, oldest first), `observe_transition` (agent for `in_progress`→`review`, human
  otherwise, plus `PrOpened`). `actions.py`: `claim`, `blocked_escape` (workpad block then
  `review`, idempotent per run id), `finish_terminal` (complete or cancelled, workspace removed).
  `orchestrator.py`: `Orchestrator.run()` = `startup()` (preflight, `auth_status`,
  `missing_labels`), then `tick()` (reconcile: stalls, running refresh with a one-tick grace for
  `review`, terminal sweep on the first and every tenth tick; mtime reload; preflight; fetch
  `in_progress`/`rework`/`todo`; dispatch while slots remain; snapshot) and a queue wait that
  fires retries (continuation 1 s; failure backoff; `escape`; `slots`) and handles worker exits
  (the session's final transition is published before any release; `max_turns` while
  `in_progress` or `max_attempts` failures → the blocked escape).
  `request_refresh()`, `request_stop()`, `snapshot()`; SIGTERM shutdown waits for `after_run`.
  Orphans resume from `session.json` when its `last_outcome` is `null` or `cancelled`; retries
  never resume. Tests drive `tick()`, `handle_worker_exit()` and `fire_due_retries()` directly
  with a fake clock and a scripted `run_session`.
- `issuebot.notifications`: the Slack sink, imported by `cli` only. `messages.py` (pure):
  `format_event(event, repo=, labels=)` → one line of mrkdwn per kind (issue link, `from → to`
  by actor, PR link, blocker reason, run cost) or `None`. `slack.py`: `urllib_post` (stdlib
  `urllib` in `asyncio.to_thread`, never raises, errors pass through `redact`), `PostResult`,
  `subscribed_kinds` (the allow-list minus `notification_sent`), `SlackSink` (`handle` formats
  and enqueues, cap 100; one drain task started by `start(bus)` posts with three attempts,
  `Retry-After` on 429 capped at 30 s, backoff 1 s then 4 s on 5xx and network errors, other
  4xx dropped; publishes `NotificationSent` after each delivery; `close()` drains for up to
  10 s). Constants, not settings. A webhook or allow-list change needs a worker restart.
- `issuebot.cli`: argparse; `validate` (twelve checks: three network probes through the
  adapter, a `claude --version` floor of 2.1.259, a `notifications.slack` check that warns when
  `SLACK_WEBHOOK_URL` is unset, requires `https`, and with `--slack-probe` posts one test
  message, and a prompt render against a sample issue), `labels ensure`, `issues list`,
  `run-once <number> [--show-prompt]` (claims `in-progress`, runs one session, never sets
  `review`), `worker [--workflow PATH]` (the orchestrator until SIGTERM/SIGINT; `[FAIL]
  startup:` lines and exit 1 when the startup probes fail); `run-once` and `worker` start the
  Slack sink before and close it after; exit codes 0/1/2 (ok / failed / workflow unloadable).
  Tests substitute `_which`, `_claude_version`, `_adapter_factory`, `_run_session`,
  `_orchestrator_factory` and `_slack_post`.

Design documents: `docs/superpowers/specs/` (phased design and one spec per phase),
`docs/superpowers/plans/` (one implementation plan per phase).

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
| `issuebot/review` | agent, when PR opened |
| `issuebot/rework` | human, if the PR needs more work |
| `issuebot/complete` | automatically, when the issue closes via linked-PR merge |

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
  review. Never merge or close PRs — that is a human action.
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

## Creating PRs

Per the user's global instructions, set PR title/body via the REST API rather
than `gh pr create`/`gh pr edit` (the CLI hits a deprecated `projectCards`
GraphQL field and aborts here); a PreToolUse hook enforces this. Write the body
to a temp `.md` file in a **separate** Bash call from the `gh api` call, and pass
it with capital `-F body=@file.md`.
