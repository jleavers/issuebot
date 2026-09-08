"""Command-line entry point for issuebot."""

import argparse
import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import uvicorn
import yaml

from issuebot import __version__
from issuebot.agent import (
    CLAUDE_PROBE_TIMEOUT_S,
    MIN_CLAUDE_VERSION,
    AgentError,
    ClaudeAuth,
    ClaudeAuthVerdict,
    ClaudeRunner,
    PromptContext,
    PromptRenderer,
    RunResult,
    TurnRunner,
    WorkspaceManager,
    claude_auth_status,
    describe_claude_auth,
    parse_claude_version,
    run_session,
    settings_for_labels,
    settings_with_model,
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
from issuebot.db import (
    MAX_WINDOW_DAYS,
    Database,
    DatabaseError,
    PostgresSink,
    RefreshListener,
    is_postgres_url,
)
from issuebot.db.queries import DailyPoint, SnapshotRow
from issuebot.events import EventBus, EventSink, LogSink, StateChanged
from issuebot.github import (
    GhCliAdapter,
    GitHubAdapter,
    GitHubError,
    Issue,
    StateLabel,
    model_label_style,
)
from issuebot.github.normalise import repo_short_name
from issuebot.log import LOG_LEVELS, configure_logging, get_logger
from issuebot.notifications import (
    POST_TIMEOUT_S,
    SlackSink,
    slack_payload,
    subscribed_kinds,
    urllib_post,
)
from issuebot.orchestrator import Orchestrator, OrchestratorStartupError
from issuebot.web import create_app, dispatch_hold

DEFAULT_WORKFLOW = "WORKFLOW.md"

# Module-level references so tests can substitute the executable lookup and the adapter.
_which = shutil.which
_adapter_factory: Callable[[GitHubSettings], GitHubAdapter] = GhCliAdapter
_runner_factory: Callable[[Settings], TurnRunner] = ClaudeRunner


def _claude_version_output(command: str) -> str | None:
    """Run ``<command> --version`` and return its stdout, or None when it cannot run."""
    try:
        completed = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            timeout=CLAUDE_PROBE_TIMEOUT_S,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return completed.stdout or None


_claude_version = _claude_version_output
_claude_auth = claude_auth_status
_run_session = run_session
_orchestrator_factory = Orchestrator
_slack_post = urllib_post
_database_factory: Callable[[str], Database] = Database


async def _uvicorn_serve(app: Any, *, host: str, port: int) -> None:
    """Serve ``app`` with uvicorn until SIGTERM or SIGINT, then return so the command exits 0.

    uvicorn installs its own handlers for both signals and, once its server has shut down,
    re-raises the signal that stopped it with the previous handler restored. The no-op
    handlers installed here make that re-raise harmless; uvicorn's own log lines go through
    the root logger (``log_config=None``), so they come out as structlog lines.
    """
    config = uvicorn.Config(app, host=host, port=port, log_config=None)
    server = uvicorn.Server(config)
    previous = {
        signum: signal.signal(signum, lambda signum, frame: None)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        await server.serve()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


_serve = _uvicorn_serve

SLACK_WEBHOOK_HOST = "hooks.slack.com"
SLACK_WEBHOOK_PATH = "/services/"

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
    validate.add_argument(
        "--slack-probe",
        action="store_true",
        help="post one test message to the configured Slack webhook",
    )
    validate.set_defaults(func=cmd_validate)

    labels = subparsers.add_parser("labels", help="manage the issuebot state labels")
    labels_sub = labels.add_subparsers(dest="labels_command", metavar="<subcommand>", required=True)
    ensure = labels_sub.add_parser(
        "ensure", help="create or update the state labels and markers in the repository"
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
        "--model",
        help="run this session with a specific claude model, overriding claude.model_labels",
    )
    run_once.add_argument(
        "--show-prompt",
        action="store_true",
        help="print the rendered first-turn prompt and exit without running anything",
    )
    run_once.set_defaults(func=cmd_run_once)

    worker = subparsers.add_parser(
        "worker", help="run the orchestrator until SIGTERM or SIGINT (the long-running service)"
    )
    _add_workflow_option(worker)
    worker.set_defaults(func=cmd_worker)

    migrate = subparsers.add_parser("migrate", help="apply pending database migrations")
    _add_workflow_option(migrate)
    migrate.set_defaults(func=cmd_migrate)

    status = subparsers.add_parser("status", help="print the worker's last runtime snapshot")
    _add_workflow_option(status)
    status.set_defaults(func=cmd_status)

    stats = subparsers.add_parser("stats", help="issues closed and runs started, by window and day")
    _add_workflow_option(stats)
    stats.add_argument(
        "--days", type=int, default=7, help="length of the daily series (default: 7)"
    )
    stats.set_defaults(func=cmd_stats)

    refresh = subparsers.add_parser("refresh", help="ask a running worker to poll now (NOTIFY)")
    _add_workflow_option(refresh)
    refresh.set_defaults(func=cmd_refresh)

    web = subparsers.add_parser(
        "web", help="serve the dashboard and the JSON API until SIGTERM or SIGINT"
    )
    _add_workflow_option(web)
    web.add_argument("--port", type=int, default=None, help="listen port (default: server.port)")
    web.add_argument("--bind", default=None, help="listen address (default: server.bind)")
    web.set_defaults(func=cmd_web)
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
    checks = run_checks(workflow, adapter=adapter, slack_probe=args.slack_probe)
    for check in checks:
        print(check.line())
    failed = sum(check.status == "fail" for check in checks)
    warned = sum(check.status == "warn" for check in checks)
    print(f"{len(checks)} checks: {failed} failed, {warned} warnings")
    if args.show_config:
        print(render_config(workflow.config), end="")
    return 1 if failed else 0


def run_checks(
    workflow: Workflow, *, adapter: GitHubAdapter | None = None, slack_probe: bool = False
) -> list[Check]:
    cfg = workflow.config
    checks = [
        Check("workflow", "ok", str(workflow.path)),
        Check("github.repo", "ok", cfg.github.repo),
        _token_check(workflow),
        _workspace_check(cfg.workspace.root),
        _claude_check(cfg.claude.command),
        _claude_auth_check(cfg.claude.command),
        _executable_check("gh", "gh"),
    ]
    checks.extend(_github_checks(adapter, tuple(cfg.claude.model_labels)))
    checks.append(_database_check(cfg))
    checks.append(_slack_check(cfg, probe=slack_probe))
    checks.append(_prompt_check(workflow))
    return checks


def _github_checks(adapter: GitHubAdapter | None, model_labels: Sequence[str] = ()) -> list[Check]:
    if adapter is None:
        return [Check(subject, "warn", "skipped (gh not found)") for subject in _NETWORK_SUBJECTS]
    return asyncio.run(_probe_github(adapter, model_labels))


async def _probe_github(adapter: GitHubAdapter, model_labels: Sequence[str] = ()) -> list[Check]:
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
        missing = await adapter.missing_labels(model_labels)
    except GitHubError as exc:
        checks.append(Check("github.labels", "fail", str(exc)))
    else:
        if missing:
            detail = f"missing: {', '.join(missing)}; run issuebot labels ensure"
            checks.append(Check("github.labels", "warn", detail))
        else:
            checks.append(Check("github.labels", "ok", _labels_detail(adapter, model_labels)))
    return checks


def _labels_detail(adapter: GitHubAdapter, model_labels: Sequence[str]) -> str:
    """What `validate` says when every label the workflow names exists."""
    markers = adapter.labels.markers()
    parts = [f"{len(adapter.labels.as_tuple())} state labels"]
    if markers:
        parts.append(_plural(len(markers), "marker label"))
    if model_labels:
        parts.append(_plural(len(model_labels), "model label"))
    if len(parts) == 1:
        return f"{parts[0]} present"
    return f"{', '.join(parts[:-1])} and {parts[-1]} present"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


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


_AUTH_LEVELS: dict[ClaudeAuthVerdict, CheckStatus] = {
    "ok": "ok",
    "ambiguous": "warn",
    "unreadable": "warn",
    "logged_out": "fail",
}


def _claude_auth_check(command: str) -> Check:
    """The claude auth line: which credential the agent will use, or that it has none."""
    subject = "claude auth"
    found = _which(command)
    if not found:
        return Check(subject, "warn", f"skipped ({command} not found)")
    auth: ClaudeAuth = describe_claude_auth(_claude_auth(found, os.environ))
    return Check(subject, _AUTH_LEVELS[auth.verdict], auth.detail)


def _version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _database_check(settings: Settings) -> Check:
    """The database.url line: presence, URL scheme, then a connect and the schema version."""
    subject = "database.url"
    if settings.database.url is None:
        return Check(subject, "ok", "not configured (history and dashboard disabled)")
    url = settings.database.url.get_secret_value()
    if not is_postgres_url(url):
        return Check(subject, "fail", "not a postgresql:// URL")
    try:
        probe = asyncio.run(_database_factory(url).probe())
    except DatabaseError as exc:
        return Check(subject, "fail", exc.message)
    detail = f"connected ({probe.server_version}); schema version {probe.schema_version}"
    if probe.ahead:
        detail += f" is newer than this issuebot knows ({probe.latest_version})"
        return Check(subject, "fail", detail)
    if probe.behind:
        detail += f" of {probe.latest_version}; run issuebot migrate"
        return Check(subject, "warn", detail)
    return Check(subject, "ok", detail)


def _slack_check(settings: Settings, *, probe: bool) -> Check:
    """The notifications.slack line: presence, URL shape (never the URL itself), optional probe."""
    subject = "notifications.slack"
    slack = settings.notifications.slack
    kinds = ", ".join(sorted(subscribed_kinds(slack)))
    if slack.webhook_url is None:
        if not kinds:
            return Check(subject, "ok", "not configured (events: [])")
        detail = (
            f"not configured; export SLACK_WEBHOOK_URL to notify on {kinds}, "
            "or set notifications.slack.events: [] to silence this"
        )
        return Check(subject, "warn", detail)
    url = slack.webhook_url.get_secret_value()
    try:
        parts = urlsplit(url)
    except ValueError:
        return Check(subject, "fail", "webhook_url is not an https URL")
    if parts.scheme != "https" or not parts.hostname:
        return Check(subject, "fail", "webhook_url is not an https URL")
    if not kinds:
        return Check(subject, "warn", "configured but events is empty; nothing will be sent")
    status: CheckStatus = "ok"
    detail = f"configured ({kinds})"
    if parts.hostname != SLACK_WEBHOOK_HOST or not parts.path.startswith(SLACK_WEBHOOK_PATH):
        status = "warn"
        detail += (
            "; the URL is not a hooks.slack.com/services/ webhook (a compatible endpoint is fine)"
        )
    if probe:
        text = (
            f":wave: issuebot validate: Slack notifications are configured for {kinds} "
            f"({settings.github.repo})"
        )
        result = asyncio.run(_slack_post(url, slack_payload(text), timeout_s=POST_TIMEOUT_S))
        if not result.ok:
            reason = f"HTTP {result.status}" if result.status else result.error or "no response"
            return Check(subject, "fail", f"test message not delivered: {reason}")
        detail += "; test message delivered"
    return Check(subject, status, detail)


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
    extra = {
        name: model_label_style(model)
        for name, model in workflow.config.claude.model_labels.items()
    }
    try:
        results = asyncio.run(adapter.ensure_labels(extra))
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


# --- the event bus ---------------------------------------------------------------------


def _slack_sink(settings: Settings) -> SlackSink | None:
    """A Slack sink when a webhook is set, is https, and at least one kind is subscribed."""
    slack = settings.notifications.slack
    if slack.webhook_url is None or not subscribed_kinds(slack):
        return None
    url = slack.webhook_url.get_secret_value()
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if parts is None or parts.scheme != "https" or not parts.hostname:
        get_logger(__name__).warning(
            "slack_sink_disabled", reason="webhook_url is not an https URL"
        )
        return None
    return SlackSink(
        slack, repo=settings.github.repo, labels=settings.github.labels, post=_slack_post
    )


@dataclass
class _Sinks:
    """The bus and the sinks whose lifetime the CLI owns (started before, closed after)."""

    bus: EventBus
    slack: SlackSink | None
    postgres: PostgresSink | None
    database: Database | None

    def start(self) -> None:
        if self.slack is not None:
            self.slack.start(self.bus)
        if self.postgres is not None:
            self.postgres.start()

    async def close(self) -> None:
        try:
            if self.slack is not None:
                await self.slack.close()
        finally:
            if self.postgres is not None:
                await self.postgres.close()

    def record_issues(self, issues: Sequence[Issue]) -> None:
        if self.postgres is not None:
            self.postgres.record_issues(issues)


async def _open_database(settings: Settings) -> Database | None:
    """Migrate at start when database.url is set; None when it is not; raises DatabaseError."""
    if settings.database.url is None:
        return None
    database = _database_factory(settings.database.url.get_secret_value())
    result = await database.migrate()
    get_logger(__name__).info(
        "db_migrated",
        database=database.description,
        applied=list(result.applied),
        version=result.version,
    )
    return database


async def _build_sinks(settings: Settings) -> _Sinks:
    """The log sink, plus Slack and PostgreSQL when configured; raises DatabaseError."""
    slack = _slack_sink(settings)
    database = await _open_database(settings)
    postgres = (
        PostgresSink(database.store(settings.github.labels), description=database.description)
        if database
        else None
    )
    sinks: list[EventSink] = [LogSink()]
    if slack is not None:
        sinks.append(slack)
    if postgres is not None:
        sinks.append(postgres)
    return _Sinks(EventBus(sinks), slack, postgres, database)


_NOT_CONFIGURED = "[FAIL] database: not configured; export DATABASE_URL or set database.url: $VAR"


def _database_or_report(settings: Settings) -> Database | None:
    if settings.database.url is None:
        print(_NOT_CONFIGURED)
        return None
    return _database_factory(settings.database.url.get_secret_value())


# --- run-once --------------------------------------------------------------------------


def cmd_run_once(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    return asyncio.run(
        _run_once(workflow, args.number, show_prompt=args.show_prompt, model=args.model)
    )


async def _run_once(
    workflow: Workflow, number: int, *, show_prompt: bool, model: str | None = None
) -> int:
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
    try:
        sinks = await _build_sinks(settings)
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    sinks.start()
    sinks.record_issues([issue])
    try:
        return await _claim_and_run(
            workflow,
            adapter,
            sinks.bus,
            issue,
            workspaces=workspaces,
            attempt=attempt,
            rework=rework,
            record=sinks.record_issues,
            model=model,
        )
    finally:
        await sinks.close()


async def _claim_and_run(
    workflow: Workflow,
    adapter: GitHubAdapter,
    bus: EventBus,
    issue: Issue,
    *,
    workspaces: WorkspaceManager,
    attempt: int,
    rework: bool,
    record: Callable[[Sequence[Issue]], None] | None = None,
    model: str | None = None,
) -> int:
    settings = workflow.config
    number = issue.number
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
            if record is not None:
                record(refreshed)
    result = await _run_session(
        issue,
        workflow,
        adapter,
        bus,
        workspaces=workspaces,
        runner=_runner_factory(
            settings_with_model(settings, model)
            if model
            else settings_for_labels(settings, issue.labels)
        ),
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


# --- worker ----------------------------------------------------------------------------


def cmd_worker(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    return asyncio.run(_run_worker(workflow))


async def _run_worker(workflow: Workflow) -> int:
    """Run the orchestrator until a stop signal; 1 when startup validation fails."""
    try:
        sinks = await _build_sinks(workflow.config)
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    postgres = sinks.postgres
    orchestrator = _orchestrator_factory(
        workflow,
        bus=sinks.bus,
        adapter_factory=_adapter_factory,
        run_session=_run_session,
        which=_which,
        claude_auth=_claude_auth,
        # None, not sinks.record_issues: the orchestrator polls review only when on_issues is set.
        on_snapshot=postgres.record_snapshot if postgres is not None else None,
        on_issues=postgres.record_issues if postgres is not None else None,
    )
    listener: RefreshListener | None = None
    if sinks.database is not None:
        listener = sinks.database.listener(orchestrator.request_refresh)
    sinks.start()
    if listener is not None:
        listener.start()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT)
    for signum in signals:
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, orchestrator.request_stop)
    try:
        await orchestrator.run()
    except OrchestratorStartupError as exc:
        for problem in exc.problems:
            print(f"[FAIL] startup: {problem}")
        return 1
    finally:
        for signum in signals:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signum)
        try:
            if listener is not None:
                await listener.close()
        finally:
            await sinks.close()
    return 0


# --- migrate, status, stats, refresh ----------------------------------------------------------


def cmd_migrate(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    database = _database_or_report(workflow.config)
    if database is None:
        return 1
    try:
        result = asyncio.run(database.migrate())
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    for label in result.applied:
        print(f"[ OK ] migration {label}: applied")
    if result.applied:
        print(f"[ OK ] database: schema version {result.version}")
    else:
        print(f"[ OK ] database: unchanged at schema version {result.version}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    database = _database_or_report(workflow.config)
    if database is None:
        return 1
    return asyncio.run(_status(database))


async def _status(database: Database) -> int:
    try:
        async with database.queries() as queries:
            row = await queries.snapshot()
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    if row is None:
        print("no runtime snapshot yet (has the worker run against this database?)")
        return 0
    print(render_status(row, now=datetime.now(UTC)), end="")
    return 0


@dataclass(frozen=True)
class StatsView:
    closed_1d: int
    closed_7d: int
    runs_1d: int
    runs_7d: int
    by_state: dict[str, int]
    series: list[DailyPoint]


def cmd_stats(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    if not 1 <= args.days <= MAX_WINDOW_DAYS:
        print(f"[FAIL] stats: --days must be between 1 and {MAX_WINDOW_DAYS}")
        return 1
    database = _database_or_report(workflow.config)
    if database is None:
        return 1
    return asyncio.run(_stats(database, args.days))


async def _stats(database: Database, days: int) -> int:
    try:
        async with database.queries() as queries:
            view = StatsView(
                closed_1d=await queries.closed_count(timedelta(days=1)),
                closed_7d=await queries.closed_count(timedelta(days=7)),
                runs_1d=await queries.runs_count(timedelta(days=1)),
                runs_7d=await queries.runs_count(timedelta(days=7)),
                by_state=await queries.state_counts(),
                series=await queries.daily_series(days),
            )
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    print(render_stats(view), end="")
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    database = _database_or_report(workflow.config)
    if database is None:
        return 1
    try:
        asyncio.run(database.notify_refresh())
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    print("[ OK ] refresh: notified issuebot_refresh")
    return 0


# --- web -------------------------------------------------------------------------------------


def cmd_web(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    return asyncio.run(_run_web(workflow, port=args.port, bind=args.bind))


async def _run_web(workflow: Workflow, *, port: int | None, bind: str | None) -> int:
    """Migrate, build the app and serve it until a stop signal; the database is required;
    a failed bind is uvicorn's error line and exit 1."""
    settings = workflow.config
    try:
        database = await _open_database(settings)
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    if database is None:
        print(_NOT_CONFIGURED)
        return 1
    host = bind or settings.server.bind
    listen_port = settings.server.port if port is None else port
    if not 0 <= listen_port <= 65535:
        print("[FAIL] web: --port must be between 0 and 65535")
        return 1
    get_logger(__name__).info(
        "web_started", bind=host, port=listen_port, database=database.description
    )
    try:
        await _serve(create_app(database, settings), host=host, port=listen_port)
    except SystemExit as exc:  # uvicorn's startup() exits 3 when the bind fails
        return 1 if exc.code else 0
    return 0


def _stamp(value: object) -> str:
    """A second-precision UTC stamp for a datetime or an ISO 8601 string; '-' for None."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "-"


def _table(rows: Sequence[Sequence[str]]) -> list[str]:
    """Left-aligned columns two spaces apart, the last column unpadded."""
    if not rows:
        return []
    last = len(rows[0]) - 1
    widths = [max(len(row[column]) for row in rows) for column in range(last)]
    lines = []
    for row in rows:
        cells = [row[column].ljust(widths[column]) for column in range(last)]
        lines.append("  ".join([*cells, row[last]]).rstrip())
    return lines


def render_status(row: SnapshotRow, *, now: datetime) -> str:
    """The runtime snapshot row as text; tolerant of missing keys (the data is JSON)."""
    data = row.data
    age = max((now - row.written_at).total_seconds(), 0.0)
    if data.get("config_valid"):
        config = "config valid"
    else:
        config = f"config error: {data.get('config_error')}"
    running = list(data.get("running") or [])
    retrying = list(data.get("retrying") or [])
    totals = dict(data.get("totals") or {})
    counters = dict(data.get("counters") or {})
    lines = [
        f"snapshot: {_stamp(row.at)} (written {_stamp(row.written_at)}, {age:.0f} s ago)",
        f"workflow: {data.get('workflow_path')} ({config})",
        f"tick {data.get('tick_count')}, last tick {_stamp(data.get('last_tick_at'))}, "
        f"poll {data.get('poll_interval_ms')} ms, {data.get('max_concurrent_agents')} slots",
    ]
    hold = dispatch_hold(row)
    if hold is not None:
        # A held worker keeps ticking, so the lines above alone read as a healthy one (#29).
        lines.append(
            f"dispatch: held ({hold['kind']}) since {_stamp(hold['since'])}: {hold['reason']}"
        )
    lines.append(f"running: {len(running)}")
    if running:
        table = [("  NUMBER", "ATTEMPT", "TURNS", "RUN_ID", "LAST_EVENT", "STARTED", "IDENTIFIER")]
        table.extend(
            (
                f"  {entry.get('issue_number')}",
                str(entry.get("attempt")),
                str(entry.get("turns")),
                str(entry.get("run_id")),
                str(entry.get("last_event") or "-"),
                _stamp(entry.get("started_at")),
                str(entry.get("identifier")),
            )
            for entry in running
        )
        lines.extend(_table(table))
    lines.append(f"retrying: {len(retrying)}")
    if retrying:
        table = [("  NUMBER", "KIND", "ATTEMPT", "DUE", "ERROR")]
        table.extend(
            (
                f"  {entry.get('issue_number')}",
                str(entry.get("kind")),
                str(entry.get("attempt")),
                _stamp(entry.get("due_at")),
                str(entry.get("error") or "-"),
            )
            for entry in retrying
        )
        lines.extend(_table(table))
    lines.append(
        f"totals: {counters.get('runs_started', 0)} runs started, "
        f"{counters.get('runs_ended', 0)} ended, {counters.get('issues_completed', 0)} completed, "
        f"{counters.get('issues_cancelled', 0)} cancelled, {counters.get('blocked', 0)} blocked; "
        f"{totals.get('total_tokens', 0)} tokens, ${float(totals.get('cost_usd', 0.0)):.2f}, "
        f"{float(totals.get('seconds_running', 0.0)):.0f} s running"
    )
    return "\n".join(lines) + "\n"


def render_stats(view: StatsView) -> str:
    lines = _table(
        [
            ("WINDOW", "CLOSED", "RUNS"),
            ("1d", str(view.closed_1d), str(view.runs_1d)),
            ("7d", str(view.closed_7d), str(view.runs_7d)),
        ]
    )
    states = ", ".join(f"{state} {count}" for state, count in view.by_state.items())
    lines.append(f"issues: {states}")
    lines.append("")
    table = [("DAY", "CLOSED", "RUNS")]
    table.extend(
        (point.day.isoformat(), str(point.closed), str(point.runs)) for point in view.series
    )
    lines.extend(_table(table))
    return "\n".join(lines) + "\n"
