"""Command-line entry point for issuebot."""

import argparse
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from issuebot import __version__
from issuebot.config import ConfigError, Settings, Workflow, load_workflow
from issuebot.config.resolve import ENV_REF
from issuebot.log import configure_logging

DEFAULT_WORKFLOW = "WORKFLOW.md"

# Module-level reference so tests can substitute the executable lookup.
_which = shutil.which

CheckStatus = Literal["ok", "warn", "fail"]
_TAGS: dict[CheckStatus, str] = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}


@dataclass(frozen=True)
class Check:
    subject: str
    status: CheckStatus
    detail: str

    def line(self) -> str:
        return f"{_TAGS[self.status]} {self.subject}: {self.detail}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issuebot",
        description="Issue-to-PR agent orchestrator for GitHub and Claude.",
    )
    parser.add_argument("--version", action="version", version=f"issuebot {__version__}")
    parser.add_argument(
        "--log-level",
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
    validate.add_argument(
        "--workflow",
        type=Path,
        default=None,
        help="path to WORKFLOW.md (default: $ISSUEBOT_WORKFLOW or ./WORKFLOW.md)",
    )
    validate.add_argument(
        "--show-config", action="store_true", help="print the effective configuration as YAML"
    )
    validate.set_defaults(func=cmd_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(
        level=args.log_level or os.environ.get("ISSUEBOT_LOG_LEVEL", "INFO"),
        fmt=args.log_format or os.environ.get("ISSUEBOT_LOG_FORMAT", "json"),
    )
    if args.command is None:
        parser.print_help()
        return 2
    return int(args.func(args))


def workflow_path(explicit: Path | None, environ: Mapping[str, str]) -> Path:
    if explicit is not None:
        return explicit
    return Path(environ.get("ISSUEBOT_WORKFLOW") or DEFAULT_WORKFLOW)


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        workflow = load_workflow(workflow_path(args.workflow, os.environ))
    except ConfigError as exc:
        print(f"[FAIL] workflow: {exc}")
        return 2

    checks = run_checks(workflow)
    for check in checks:
        print(check.line())
    failed = sum(check.status == "fail" for check in checks)
    warned = sum(check.status == "warn" for check in checks)
    print(f"{len(checks)} checks: {failed} failed, {warned} warnings")
    if args.show_config:
        print(render_config(workflow.config), end="")
    return 1 if failed else 0


def run_checks(workflow: Workflow) -> list[Check]:
    cfg = workflow.config
    checks = [
        Check("workflow", "ok", str(workflow.path)),
        Check("github.repo", "ok", cfg.github.repo),
        _token_check(workflow),
        _workspace_check(cfg.workspace.root),
        _executable_check("claude.command", cfg.claude.command),
        _executable_check("gh", "gh"),
        Check(
            "database.url",
            "ok",
            "configured" if cfg.database.url else "not configured (history and dashboard disabled)",
        ),
        Check(
            "notifications.slack",
            "ok",
            "configured" if cfg.notifications.slack.webhook_url else "not configured",
        ),
    ]
    body = workflow.prompt_template
    if body:
        checks.append(Check("prompt", "ok", f"{len(body)} characters"))
    else:
        checks.append(Check("prompt", "warn", "body is empty"))
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
