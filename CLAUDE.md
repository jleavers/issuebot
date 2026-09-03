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
uv run issuebot labels ensure        # create/update the five state labels in github.repo
uv run issuebot issues list          # table of open issues carrying a state label
uv run issuebot run-once <number>    # one worker session in the foreground (--show-prompt renders only)
docker compose build                 # image: git, gh, claude, app venv
docker compose up                    # db (postgres:18) + worker (runs validate until Phase 4)
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
- `issuebot.cli`: argparse; `validate` (twelve checks: three network probes through the
  adapter, a `claude --version` floor of 2.1.259, and a prompt render against a sample
  issue), `labels ensure`, `issues list`, `run-once <number> [--show-prompt]` (claims
  `in-progress`, runs one session, never sets `review`); exit codes 0/1/2 (ok / failed /
  workflow unloadable). Tests substitute `_which`, `_claude_version`, `_adapter_factory`
  and `_run_session`.

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
