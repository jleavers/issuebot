"""Command-line entry point for issuebot."""

import argparse
import asyncio
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Literal

import yaml

from issuebot import __version__
from issuebot.config import ConfigError, GitHubSettings, Settings, Workflow, load_workflow
from issuebot.config.resolve import ENV_REF
from issuebot.github import GhCliAdapter, GitHubAdapter, GitHubError, Issue, StateLabel
from issuebot.log import LOG_LEVELS, configure_logging

DEFAULT_WORKFLOW = "WORKFLOW.md"

# Module-level references so tests can substitute the executable lookup and the adapter.
_which = shutil.which
_adapter_factory: Callable[[GitHubSettings], GitHubAdapter] = GhCliAdapter

CheckStatus = Literal["ok", "warn", "fail"]
_TAGS: dict[CheckStatus, str] = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
_NETWORK_SUBJECTS = ("gh auth", "github.repo access", "github.labels")
_ROLE_ORDER: dict[StateLabel | None, int] = {role: index for index, role in enumerate(StateLabel)}


@dataclass(frozen=True)
class Check:
    subject: str
    status: CheckStatus
    detail: str

    def line(self) -> str:
        return f"{_TAGS[self.status]} {self.subject}: {self.detail}"


def _add_workflow_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workflow",
        type=Path,
        default=None,
        help="path to WORKFLOW.md (default: $ISSUEBOT_WORKFLOW or ./WORKFLOW.md)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issuebot",
        description="Issue-to-PR agent orchestrator for GitHub and Claude.",
    )
    parser.add_argument("--version", action="version", version=f"issuebot {__version__}")
    parser.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        type=str.upper,
        default=None,
        help="DEBUG, INFO, WARNING or ERROR (default: $ISSUEBOT_LOG_LEVEL or INFO)",
    )
    parser.add_argument(
        "--log-format",
        choices=["json", "console"],
        default=None,
        help="log line format (default: $ISSUEBOT_LOG_FORMAT or json)",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    validate = subparsers.add_parser(
        "validate", help="load WORKFLOW.md and check the runtime environment"
    )
    _add_workflow_option(validate)
    validate.add_argument(
        "--show-config", action="store_true", help="print the effective configuration as YAML"
    )
    validate.set_defaults(func=cmd_validate)

    labels = subparsers.add_parser("labels", help="manage the issuebot state labels")
    labels_sub = labels.add_subparsers(dest="labels_command", metavar="<subcommand>", required=True)
    ensure = labels_sub.add_parser(
        "ensure", help="create or update the five state labels in the repository"
    )
    _add_workflow_option(ensure)
    ensure.set_defaults(func=cmd_labels_ensure)

    issues = subparsers.add_parser("issues", help="inspect tracked issues")
    issues_sub = issues.add_subparsers(dest="issues_command", metavar="<subcommand>", required=True)
    issues_list = issues_sub.add_parser("list", help="list open issues carrying a state label")
    _add_workflow_option(issues_list)
    issues_list.add_argument(
        "--state",
        choices=[role.value for role in StateLabel],
        default=None,
        help="only issues in this state",
    )
    issues_list.set_defaults(func=cmd_issues_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        configure_logging(
            level=args.log_level or os.environ.get("ISSUEBOT_LOG_LEVEL", "INFO"),
            fmt=args.log_format or os.environ.get("ISSUEBOT_LOG_FORMAT", "json"),
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.command is None:
        parser.print_help()
        return 2
    return int(args.func(args))


def workflow_path(explicit: Path | None, environ: Mapping[str, str]) -> Path:
    if explicit is not None:
        return explicit
    return Path(environ.get("ISSUEBOT_WORKFLOW") or DEFAULT_WORKFLOW)


def _load_or_report(args: argparse.Namespace) -> Workflow | None:
    try:
        return load_workflow(workflow_path(args.workflow, os.environ))
    except ConfigError as exc:
        print(f"[FAIL] workflow: {exc}")
        return None


# --- validate ------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    adapter = _adapter_factory(workflow.config.github) if _which("gh") else None
    checks = run_checks(workflow, adapter=adapter)
    for check in checks:
        print(check.line())
    failed = sum(check.status == "fail" for check in checks)
    warned = sum(check.status == "warn" for check in checks)
    print(f"{len(checks)} checks: {failed} failed, {warned} warnings")
    if args.show_config:
        print(render_config(workflow.config), end="")
    return 1 if failed else 0


def run_checks(workflow: Workflow, *, adapter: GitHubAdapter | None = None) -> list[Check]:
    cfg = workflow.config
    checks = [
        Check("workflow", "ok", str(workflow.path)),
        Check("github.repo", "ok", cfg.github.repo),
        _token_check(workflow),
        _workspace_check(cfg.workspace.root),
        _executable_check("claude.command", cfg.claude.command),
        _executable_check("gh", "gh"),
    ]
    checks.extend(_github_checks(adapter))
    checks.append(
        Check(
            "database.url",
            "ok",
            "configured" if cfg.database.url else "not configured (history and dashboard disabled)",
        )
    )
    checks.append(
        Check(
            "notifications.slack",
            "ok",
            "configured" if cfg.notifications.slack.webhook_url else "not configured",
        )
    )
    body = workflow.prompt_template
    if body:
        checks.append(Check("prompt", "ok", f"{len(body)} characters"))
    else:
        checks.append(Check("prompt", "warn", "body is empty"))
    return checks


def _github_checks(adapter: GitHubAdapter | None) -> list[Check]:
    if adapter is None:
        return [Check(subject, "warn", "skipped (gh not found)") for subject in _NETWORK_SUBJECTS]
    return asyncio.run(_probe_github(adapter))


async def _probe_github(adapter: GitHubAdapter) -> list[Check]:
    checks: list[Check] = []
    try:
        auth = await adapter.auth_status()
        checks.append(Check("gh auth", "ok", f"logged in as {auth.login}"))
    except GitHubError as exc:
        checks.append(Check("gh auth", "fail", f"{exc.message}; run gh auth login or set GH_TOKEN"))
    try:
        info = await adapter.repo_info()
        detail = f"{info.full_name} (default branch {info.default_branch})"
        checks.append(Check("github.repo access", "ok", detail))
    except GitHubError as exc:
        checks.append(Check("github.repo access", "fail", str(exc)))
    try:
        missing = await adapter.missing_labels()
    except GitHubError as exc:
        checks.append(Check("github.labels", "fail", str(exc)))
    else:
        if missing:
            detail = f"missing: {', '.join(missing)}; run issuebot labels ensure"
            checks.append(Check("github.labels", "warn", detail))
        else:
            checks.append(Check("github.labels", "ok", "5 labels present"))
    return checks


def _token_check(workflow: Workflow) -> Check:
    if workflow.config.github.token is None:
        return Check("github.token", "fail", "not set; export GH_TOKEN or set github.token: $VAR")
    raw_github = workflow.raw_config.get("github")
    raw_token = raw_github.get("token") if isinstance(raw_github, dict) else None
    if raw_token is None:
        return Check("github.token", "ok", "set (from GH_TOKEN)")
    if isinstance(raw_token, str) and ENV_REF.match(raw_token):
        return Check("github.token", "ok", f"set (from {raw_token})")
    return Check("github.token", "warn", "literal value in WORKFLOW.md; prefer $VAR")


def _workspace_check(root: Path) -> Check:
    if root.parent.is_dir():
        return Check("workspace.root", "ok", str(root))
    return Check("workspace.root", "warn", f"{root} (parent directory does not exist)")


def _executable_check(subject: str, command: str) -> Check:
    found = _which(command)
    if found:
        return Check(subject, "ok", found)
    return Check(subject, "fail", f"{command!r} not found on PATH")


def render_config(settings: Settings) -> str:
    return yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False)


# --- labels ----------------------------------------------------------------------------


def cmd_labels_ensure(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    adapter = _adapter_factory(workflow.config.github)
    try:
        results = asyncio.run(adapter.ensure_labels())
    except GitHubError as exc:
        print(f"[FAIL] labels: {exc}")
        return 1
    for result in results:
        print(f"[ OK ] {result.name}: {result.outcome}")
    return 0


# --- issues ----------------------------------------------------------------------------


def cmd_issues_list(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    adapter = _adapter_factory(workflow.config.github)
    roles = [StateLabel(args.state)] if args.state else list(StateLabel)
    try:
        issues = asyncio.run(adapter.fetch_issues_by_states(roles))
    except GitHubError as exc:
        print(f"[FAIL] issues: {exc}")
        return 1
    print(render_issue_table(issues), end="")
    return 0


def render_issue_table(issues: Sequence[Issue]) -> str:
    if not issues:
        return "no tracked issues\n"
    rows: list[tuple[str, str, str, str, str]] = [("NUMBER", "STATE", "PR", "UPDATED", "TITLE")]
    ordered = sorted(
        issues, key=lambda issue: (_ROLE_ORDER.get(issue.state, len(StateLabel)), issue.number)
    )
    for issue in ordered:
        pr = f"#{issue.linked_pr.number} {issue.linked_pr.state}" if issue.linked_pr else "-"
        state = issue.state.value if issue.state else "conflict"
        updated = issue.updated_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append((str(issue.number), state, pr, updated, issue.title))
    widths = [max(len(row[column]) for row in rows) for column in range(4)]
    lines = []
    for row in rows:
        cells = [row[column].ljust(widths[column]) for column in range(4)]
        lines.append("  ".join([*cells, row[4]]).rstrip())
    return "\n".join(lines) + "\n"
