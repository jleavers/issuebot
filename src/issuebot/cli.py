"""Command-line entry point for issuebot."""

import argparse
import asyncio
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

from issuebot import __version__
from issuebot.agent import (
    MIN_CLAUDE_VERSION,
    AgentError,
    ClaudeRunner,
    PromptContext,
    PromptRenderer,
    RunResult,
    WorkspaceManager,
    parse_claude_version,
    run_session,
)
from issuebot.config import (
    ConfigError,
    GitHubLabels,
    GitHubSettings,
    Settings,
    Workflow,
    load_workflow,
)
from issuebot.config.resolve import ENV_REF
from issuebot.events import EventBus, LogSink, StateChanged
from issuebot.github import GhCliAdapter, GitHubAdapter, GitHubError, Issue, StateLabel
from issuebot.github.normalise import repo_short_name
from issuebot.log import LOG_LEVELS, configure_logging

DEFAULT_WORKFLOW = "WORKFLOW.md"

# Module-level references so tests can substitute the executable lookup and the adapter.
_which = shutil.which
_adapter_factory: Callable[[GitHubSettings], GitHubAdapter] = GhCliAdapter

_VERSION_PROBE_TIMEOUT_S = 10


def _claude_version_output(command: str) -> str | None:
    """Run ``<command> --version`` and return its stdout, or None when it cannot run."""
    try:
        completed = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_PROBE_TIMEOUT_S,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return completed.stdout or None


_claude_version = _claude_version_output
_run_session = run_session

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

    run_once = subparsers.add_parser(
        "run-once", help="run one worker session for an issue in the foreground"
    )
    run_once.add_argument("number", type=int, help="issue number")
    _add_workflow_option(run_once)
    run_once.add_argument(
        "--show-prompt",
        action="store_true",
        help="print the rendered first-turn prompt and exit without running anything",
    )
    run_once.set_defaults(func=cmd_run_once)
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
        _claude_check(cfg.claude.command),
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
    checks.append(_prompt_check(workflow))
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


def _claude_check(command: str) -> Check:
    found = _which(command)
    if not found:
        return Check("claude.command", "fail", f"{command!r} not found on PATH")
    output = _claude_version(found)
    version = parse_claude_version(output)
    if version is None:
        reason = "no output" if not output else f"unparseable output {output.strip()[:40]!r}"
        return Check("claude.command", "warn", f"{found} (version unknown: {reason})")
    text = _version_text(version)
    if version < MIN_CLAUDE_VERSION:
        needed = _version_text(MIN_CLAUDE_VERSION)
        detail = f"{found} is {text}; issuebot needs {needed} or newer"
        return Check("claude.command", "fail", detail)
    return Check("claude.command", "ok", f"{found} ({text})")


def _version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _prompt_check(workflow: Workflow) -> Check:
    body = workflow.prompt_template
    if not body:
        return Check("prompt", "warn", "body is empty")
    try:
        PromptRenderer(body).render(_sample_context(workflow.config))
    except AgentError as exc:
        return Check("prompt", "fail", exc.message)
    return Check("prompt", "ok", f"{len(body)} characters, renders")


def _sample_context(settings: Settings) -> PromptContext:
    """A plausible in-progress issue so validate can render the template end to end."""
    now = datetime.now(UTC)
    label = settings.github.labels.in_progress.lower()
    issue = Issue(
        id="1",
        identifier=f"{repo_short_name(settings.github.repo)}-1",
        number=1,
        title="Sample issue",
        body="Sample description.",
        github_state="open",
        state=StateLabel.IN_PROGRESS,
        state_labels=(label,),
        labels=(label,),
        url=f"https://github.com/{settings.github.repo}/issues/1",
        assignees=(),
        created_at=now,
        updated_at=now,
        closed_at=None,
        linked_pr=None,
        dispatchable=True,
    )
    return PromptContext(
        issue=issue,
        repo=settings.github.repo,
        labels=settings.github.labels,
        attempt=1,
        turn_number=1,
        max_turns=settings.agent.max_turns,
        rework=False,
        self_review=settings.agent.self_review,
    )


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


# --- run-once --------------------------------------------------------------------------


def cmd_run_once(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    return asyncio.run(_run_once(workflow, args.number, show_prompt=args.show_prompt))


async def _run_once(workflow: Workflow, number: int, *, show_prompt: bool) -> int:
    settings = workflow.config
    adapter = _adapter_factory(settings.github)
    try:
        issues = await adapter.fetch_issues_by_ids([str(number)])
    except GitHubError as exc:
        print(f"[FAIL] issue: {exc}")
        return 1
    if not issues:
        print(f"[FAIL] issue: #{number} not found")
        return 1
    issue = issues[0]
    problem = not_runnable(issue, settings.github.labels)
    if problem is not None:
        print(f"[FAIL] issue: #{number} {problem}")
        return 1
    rework = issue.state is StateLabel.REWORK
    workspaces = WorkspaceManager(settings)
    try:
        attempt = _next_attempt(workspaces, issue)
    except AgentError as exc:
        print(f"[FAIL] workspace: {exc.message}")
        return 1
    if show_prompt:
        context = PromptContext(
            issue=issue,
            repo=settings.github.repo,
            labels=settings.github.labels,
            attempt=attempt,
            turn_number=1,
            max_turns=settings.agent.max_turns,
            rework=rework,
            self_review=settings.agent.self_review,
        )
        try:
            print(PromptRenderer(workflow.prompt_template).render(context).rstrip("\n"))
        except AgentError as exc:
            print(f"[FAIL] prompt: {exc.message}")
            return 1
        return 0
    bus = EventBus([LogSink()])
    if issue.state is not StateLabel.IN_PROGRESS:
        from_label = issue.state_labels[0] if issue.state_labels else None
        try:
            await adapter.set_state(number, StateLabel.IN_PROGRESS)
        except GitHubError as exc:
            print(f"[FAIL] claim: {exc}")
            return 1
        bus.publish(
            StateChanged(
                issue_number=number,
                issue_identifier=issue.identifier,
                from_label=from_label,
                to_label=settings.github.labels.in_progress,
                actor="issuebot",
            )
        )
        try:
            refreshed = await adapter.fetch_issues_by_ids([str(number)])
        except GitHubError as exc:
            print(f"[FAIL] claim: {exc}")
            return 1
        if refreshed:
            issue = refreshed[0]
    result = await _run_session(
        issue,
        workflow,
        adapter,
        bus,
        workspaces=workspaces,
        runner=ClaudeRunner(settings),
        attempt=attempt,
        rework=rework,
    )
    print(render_run_summary(result), end="")
    return 0 if result.outcome == "succeeded" else 1


def not_runnable(issue: Issue, labels: GitHubLabels) -> str | None:
    """Why run-once refuses this issue, or None when it may run."""
    hint = f"; label it {labels.todo} or {labels.rework} first"
    if issue.github_state == "closed":
        return "is closed"
    if issue.state is None:
        if issue.state_labels:
            return "carries more than one state label" + hint
        return "is unlabelled" + hint
    if issue.state not in (StateLabel.TODO, StateLabel.REWORK, StateLabel.IN_PROGRESS):
        return f"is {issue.state.value}" + hint
    return None


def _next_attempt(workspaces: WorkspaceManager, issue: Issue) -> int:
    record = workspaces.read_session(workspaces.path_for(issue.identifier))
    if record is not None and record.issue_number == issue.number:
        return record.attempt + 1
    return 1


def render_run_summary(result: RunResult) -> str:
    turns = "turn" if result.turns == 1 else "turns"
    state = result.final_state.value if result.final_state is not None else "unlabelled"
    lines = [
        f"run {result.run_id}: {result.outcome} ({result.stop_reason}) after {result.turns} "
        f"{turns} in {_duration(result.duration_s)}, ${result.cost_usd:.2f}, "
        f"{result.input_tokens} in / {result.output_tokens} out"
    ]
    if result.stop_reason == "max_turns":
        lines.append(
            f"turn budget exhausted; issue #{result.issue_number} remains {state} "
            "(the blocked escape is Phase 4)"
        )
    elif result.error_category is not None:
        lines.append(f"error: {result.error_category}: {result.error}")
    else:
        lines.append(f"issue #{result.issue_number} is now {state}")
    if result.log_dir is not None:
        lines.append(f"logs: {result.log_dir}")
    return "\n".join(lines) + "\n"


def _duration(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}m{total % 60:02d}s"
