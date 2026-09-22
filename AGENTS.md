# AGENTS.md

This file defines operational rules for AI coding agents working in this repository.

## Environment

The repository is used from both **Windows** and **Linux** hosts. Detect the OS before running shell commands:

- **Windows** (PowerShell): `$env:OS` contains `"Windows_NT"`, or `uname` is unavailable.
- **Linux / macOS** (Bash): `uname -s` returns `"Linux"` or `"Darwin"`.

Adapt behaviour accordingly:

| | Windows (PowerShell) | Linux / macOS (Bash) |
|---|---|---|
| Shell / tool | PowerShell | Bash |
| Script extension | `.ps1` | `.sh` |
| Line continuation | `` ` `` (backtick) | `\` (backslash) |
| Command chaining | `;` | `&&` |

Never generate `.sh` scripts when on Windows; never generate `.ps1` scripts when on Linux/macOS.

## Command Chaining

Chain commands using the separator appropriate to the detected host OS:

- **Windows (PowerShell):** `command1 ; command2`
- **Linux / macOS (Bash):** `command1 && command2`

## Git Workflow

Allowed commands:
- git add
- git commit
- git push (feature branches only — see rules below)
- gh pr create (to open a PR for human review after pushing a feature branch)

Rules:
- Agents **must never** push directly to `main`.
- `main` also carries a repository ruleset that refuses a direct push, a force push and a
  deletion, with no bypass for admins. An agent that pushes to `main` anyway gets a rejection
  from the server, not a merge — treat that rejection as this rule working, not as an
  obstacle to route around. The fix is always the same: push a feature branch and open a PR.
- Agents **may** push a feature branch (`git push -u origin <branch>`) and then immediately open a GitHub PR for human review using `gh pr create`.
- All agent-initiated PRs must target `main` and include a clear summary of changes.
- Agents must not merge or close PRs — that is a human action.

## Pre-Commit Configuration

Ensure `.pre-commit-config.yaml` exists.

Required base hooks:
https://github.com/pre-commit/pre-commit-hooks

Hooks required:
- trailing-whitespace
- end-of-file-fixer

## Terraform

If `.tf` files exist, add hooks from:

https://github.com/antonbabenko/pre-commit-terraform

Required:
- terraform_fmt
- terraform_validate
- terraform_tflint

## Destructive Commands

Agents must never execute destructive commands including:

- rm -rf
- git reset --hard
- git clean -fd
