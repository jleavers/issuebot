"""Tests for the command-line entry point."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from fakes.database import DB_URL, FakeDatabase
from issuebot import __version__
from issuebot.agent import ClaudeRunner, RunResult, SessionRecord, WorkspaceManager
from issuebot.agent.runner import RateLimits, RateLimitWindow
from issuebot.agent.scrub import Scrubber
from issuebot.cli import (
    StatsView,
    _deployment_scrubber,
    _turn_capture,
    main,
    not_runnable,
    render_issue_table,
    render_run_summary,
    render_stats,
    render_status,
)
from issuebot.config import GitHubLabels, GitHubSettings, Settings
from issuebot.db import (
    MAX_WINDOW_DAYS,
    DatabaseError,
    MigrationResult,
    Probe,
    StoreError,
    StoreUnavailableError,
)
from issuebot.db.queries import DailyPoint, LedgerRow, SnapshotRow
from issuebot.events import Event, StateChanged
from issuebot.github import (
    WORKPAD_MARKER,
    FakeGitHub,
    GitHubError,
    Issue,
    LinkedPr,
    StateLabel,
    model_label_style,
)
from issuebot.notifications import PostResult
from issuebot.orchestrator import IssueLedger, OrchestratorStartupError
from issuebot.orchestrator.state import ClaudeTotals, Counters, RuntimeSnapshot

SEED_AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "workflows"
GOOD = FIXTURES / "good.md"
INVALID = FIXTURES / "invalid.md"
WEBHOOK = "https://hooks.slack.com/services/T000/B000/secret"


LOGGED_IN = '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}'


@pytest.fixture
def executables(monkeypatch: pytest.MonkeyPatch) -> Callable[[set[str]], None]:
    """Pretend the given names are on PATH, reporting Claude Code 2.1.259 and a claude.ai login."""

    def install(
        names: set[str],
        version: str | None = "2.1.259 (Claude Code)",
        auth: str | None = LOGGED_IN,
    ) -> None:
        monkeypatch.setattr(
            "issuebot.cli._which", lambda name: f"/usr/bin/{name}" if name in names else None
        )
        monkeypatch.setattr("issuebot.cli._claude_version", lambda command: version)
        monkeypatch.setattr("issuebot.cli._claude_auth", lambda command, environ, run_as=None: auth)

    install({"claude", "gh"})
    return install


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture(autouse=True)
def fake_github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    """Every CLI command talks to this in-memory GitHub instead of the real gh."""
    fake = FakeGitHub(GitHubSettings(repo="example/repo"), now=_Clock())
    monkeypatch.setattr("issuebot.cli._adapter_factory", lambda settings: fake)
    return fake


class FakeSlackPost:
    """Stands in for urllib_post: records each payload; answers from a script, else 200."""

    def __init__(self) -> None:
        self.results: list[PostResult] = []
        self.calls: list[dict[str, object]] = []

    async def __call__(self, url: str, payload: bytes, *, timeout_s: float) -> PostResult:
        self.calls.append({"url": url, "text": json.loads(payload)["text"]})
        return self.results.pop(0) if self.results else PostResult(status=200)


@pytest.fixture
def slack_post(monkeypatch: pytest.MonkeyPatch) -> FakeSlackPost:
    fake = FakeSlackPost()
    monkeypatch.setattr("issuebot.cli._slack_post", fake)
    return fake


class FakeGitHubStatus:
    """Stands in for the githubstatus.com fetch: answers "operational" unless a test says else."""

    OPERATIONAL = json.dumps(
        {
            "status": {"indicator": "none", "description": "All Systems Operational"},
            "components": [{"name": "Pull Requests", "status": "operational", "group": False}],
        }
    )

    def __init__(self) -> None:
        self.payload: str | None = self.OPERATIONAL
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        return self.payload


@pytest.fixture(autouse=True)
def github_status(monkeypatch: pytest.MonkeyPatch) -> FakeGitHubStatus:
    """Autouse: no test reaches githubstatus.com, and `validate` is the same offline or not."""
    fake = FakeGitHubStatus()
    monkeypatch.setattr("issuebot.cli._github_status", fake)
    return fake


@pytest.fixture(autouse=True)
def fake_database(monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    """Every CLI command talks to this stand-in instead of a real PostgreSQL server."""
    fake = FakeDatabase()
    monkeypatch.setattr("issuebot.cli._database_factory", fake.factory)
    return fake


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(text, encoding="utf-8")
    return path


# --- top level -------------------------------------------------------------------


def test_version_flag_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"issuebot {__version__}"


def test_no_command_prints_help_and_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage: issuebot" in capsys.readouterr().out


def test_help_stays_plain_when_the_environment_asks_for_colour(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guard behind every assertion above that reads help text (#81).

    ``FORCE_COLOR`` is what a CI runner or a developer's shell sets, and on 3.14 it is enough
    to colourise argparse on its own; only ``conftest``'s ``no_ansi_colour`` outranks it. So
    this fails wherever it is run if that fixture goes away -- unlike an assertion on plain
    output alone, which passes on any machine that happens not to ask for colour.

    ``NO_COLOR`` goes because it outranks ``FORCE_COLOR`` in turn, and a shell that exports it
    would otherwise keep this test green for the wrong reason -- which is the very accident
    that hid the bug in the first place.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert main([]) == 2
    out = capsys.readouterr().out
    assert "usage: issuebot" in out  # not merely "no escapes": empty output has none either
    assert "\x1b[" not in out


def test_unknown_command_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2


def test_installed_script_runs_version() -> None:
    result = subprocess.run(["issuebot", "--version"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert result.stdout.strip() == f"issuebot {__version__}"


# --- validate --------------------------------------------------------------------


def test_validate_good_workflow_exits_zero(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, executables: object
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token-value")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert f"[ OK ] workflow: {GOOD.resolve()}" in out
    assert "[ OK ] github.repo: example/repo" in out
    assert "[ OK ] github.token: set (from $GH_TOKEN)" in out
    assert "[ OK ] workspace.root: /workspaces" in out
    assert "[ OK ] claude.command: /usr/bin/claude (2.1.259)" in out
    assert "[ OK ] gh: /usr/bin/gh" in out
    assert "[ OK ] database.url: not configured (history and dashboard disabled)" in out
    assert (
        "[WARN] notifications.slack: not configured; export SLACK_WEBHOOK_URL to notify on "
        "blocked, state_changed, or set notifications.slack.events: [] to silence this" in out
    )
    assert "[ OK ] prompt: 44 characters, renders" in out
    assert "[ OK ] gh auth: logged in as issuebot" in out
    assert "[ OK ] github.repo access: example/repo (default branch main)" in out
    assert "[ OK ] github.labels: 5 state labels and 1 marker label present" in out
    assert (
        out.index("[ OK ] gh: ") < out.index("[ OK ] gh auth:") < out.index("[ OK ] database.url")
    )
    assert out.rstrip().endswith("15 checks: 0 failed, 2 warnings")
    assert "secret-token-value" not in out


def test_validate_names_the_overlay_and_counts_its_overrides(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    """The one check that answers "is it running my overrides?" before the worker starts."""
    monkeypatch.setenv("GH_TOKEN", "secret-token-value")
    path = _write(tmp_path, GOOD.read_text(encoding="utf-8"))
    overlay = tmp_path / "WORKFLOW.local.md"
    overlay.write_text(
        "---\ngithub:\n  repo: acme/frontend\nclaude:\n  max_budget_usd: 3.0\n---\n",
        encoding="utf-8",
    )
    assert main(["validate", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert f"[ OK ] workflow: {path.resolve()} + WORKFLOW.local.md (2 overrides)" in out
    assert "[ OK ] github.repo: acme/frontend" in out
    assert out.rstrip().endswith("15 checks: 0 failed, 2 warnings")

    overlay.write_text("---\nclaude:\n  model: null\n---\n", encoding="utf-8")
    assert main(["validate", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert f"[ OK ] workflow: {path.resolve()} + WORKFLOW.local.md (1 override)" in out
    assert "[ OK ] github.repo: example/repo" in out


def test_validate_reports_an_invalid_overlay_naming_both_files(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token-value")
    path = _write(tmp_path, GOOD.read_text(encoding="utf-8"))
    (tmp_path / "WORKFLOW.local.md").write_text(
        "---\nclaude:\n  max_budget: 3.0\n---\n", encoding="utf-8"
    )
    assert main(["validate", "--workflow", str(path)]) == 2
    out = capsys.readouterr().out
    assert out.startswith(f"[FAIL] workflow: {path.resolve()} (+ WORKFLOW.local.md): ")
    assert "claude.max_budget: Extra inputs are not permitted" in out


def test_validate_token_from_fallback_variable(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(path)]) == 0
    assert "[ OK ] github.token: set (from GH_TOKEN)" in capsys.readouterr().out


def test_validate_missing_token_fails(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, executables: object
) -> None:
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] github.token: not set; export GH_TOKEN or set github.token: $VAR" in out
    assert "1 failed" in out


def test_validate_literal_token_warns(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, executables: object
) -> None:
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n  token: ghp_literal\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] github.token: literal value in WORKFLOW.md; prefer $VAR" in out
    assert "0 failed, 3 warnings" in out


def test_validate_missing_executables_fail(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: Callable[[set[str]], None],
) -> None:
    executables(set())
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] claude.command: 'claude' not found on PATH" in out
    assert "[FAIL] gh: 'gh' not found on PATH" in out
    assert "[WARN] gh auth: skipped (gh not found)" in out
    assert "[WARN] github.repo access: skipped (gh not found)" in out
    assert "[WARN] github.labels: skipped (gh not found)" in out
    assert "[WARN] claude auth: skipped (claude not found)" in out
    assert "2 failed, 6 warnings" in out


def test_validate_custom_claude_command_is_looked_up(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: Callable[[set[str]], None],
) -> None:
    executables({"my-claude", "gh"})
    monkeypatch.setenv("GH_TOKEN", "t")
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\nclaude:\n  command: my-claude\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 0
    assert "[ OK ] claude.command: /usr/bin/my-claude" in capsys.readouterr().out


def test_validate_workspace_root_parent_missing_warns(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    root = tmp_path / "missing-parent" / "ws"
    path = _write(tmp_path, f"---\ngithub:\n  repo: o/r\nworkspace:\n  root: {root}\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 0
    assert f"[WARN] workspace.root: {root} (parent directory does not exist)" in (
        capsys.readouterr().out
    )


def test_validate_empty_prompt_warns(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\n")
    assert main(["validate", "--workflow", str(path)]) == 0
    assert "[WARN] prompt: body is empty" in capsys.readouterr().out


def test_validate_configured_database_and_slack(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/x")
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] database.url: connected (PostgreSQL 18.1); schema version 2" in out
    assert (
        "[WARN] notifications.slack: configured (blocked, state_changed); the URL is not a "
        "hooks.slack.com/services/ webhook (a compatible endpoint is fine)" in out
    )
    assert "hooks.example" not in out
    assert "15 checks: 0 failed, 2 warnings" in out


def _validate_with_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str = DB_URL
) -> int:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", url)
    path = _write(
        tmp_path, "---\ngithub:\n  repo: o/r\nnotifications:\n  slack:\n    events: []\n---\nBody"
    )
    return main(["validate", "--workflow", str(path)])


def test_validate_rejects_a_non_postgres_database_url(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    fake_database: FakeDatabase,
) -> None:
    assert _validate_with_database(tmp_path, monkeypatch, "mysql://u:p@h/db") == 1
    out = capsys.readouterr().out
    assert "[FAIL] database.url: not a postgresql:// URL" in out
    assert "15 checks: 1 failed, 1 warnings" in out
    assert fake_database.urls == []


KEYWORD_DSN = "host=db.example port=5432 user=issuebot password=s3cretpassword dbname=issuebot"


def test_validate_rejects_a_keyword_value_dsn_without_echoing_it(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    fake_database: FakeDatabase,
) -> None:
    assert _validate_with_database(tmp_path, monkeypatch, KEYWORD_DSN) == 1
    out = capsys.readouterr().out
    assert "[FAIL] database.url: not a postgresql:// URL" in out
    assert "s3cretpassword" not in out
    assert fake_database.urls == []


def test_validate_reports_an_unreachable_database_without_the_url(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    fake_database: FakeDatabase,
) -> None:
    fake_database.probe_error = StoreUnavailableError(
        "cannot connect: connection to server at <database url> failed"
    )
    assert _validate_with_database(tmp_path, monkeypatch) == 1
    out = capsys.readouterr().out
    assert (
        "[FAIL] database.url: cannot connect: connection to server at <database url> failed" in out
    )
    assert "s3cret" not in out
    assert fake_database.urls == [DB_URL]


def test_validate_warns_when_the_schema_is_behind(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    fake_database: FakeDatabase,
) -> None:
    fake_database.probe_result = Probe(
        server_version="PostgreSQL 18.1", schema_version=0, latest_version=1
    )
    assert _validate_with_database(tmp_path, monkeypatch) == 0
    out = capsys.readouterr().out
    assert (
        "[WARN] database.url: connected (PostgreSQL 18.1); schema version 0 of 1; "
        "run issuebot migrate" in out
    )
    assert "15 checks: 0 failed, 2 warnings" in out


# --- validate: github.status (#88) -------------------------------------------------


def test_validate_reports_an_operational_status_page(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    github_status: FakeGitHubStatus,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    assert "[ OK ] github.status: All Systems Operational" in capsys.readouterr().out
    assert github_status.calls == 1


def test_validate_warns_about_an_incident_without_failing(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    github_status: FakeGitHubStatus,
) -> None:
    """Advisory: a human is running this and there is no dispatch to hold (#88)."""
    github_status.payload = json.dumps(
        {
            "status": {"indicator": "major", "description": "Partial System Outage"},
            "components": [
                {"name": "Pull Requests", "status": "major_outage", "group": False},
                {"name": "Actions", "status": "degraded_performance", "group": False},
            ],
        }
    )
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert (
        "[WARN] github.status: incident in progress \u2014 "
        "Pull Requests, major outage; Actions, degraded performance" in out
    )
    assert "15 checks: 0 failed, 3 warnings" in out


def test_validate_says_so_when_the_status_page_does_not_answer(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    github_status: FakeGitHubStatus,
) -> None:
    """An offline host still validates: the check names its own blank and never fails."""
    github_status.payload = None
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] github.status: githubstatus.com did not answer; this check is advisory" in out
    assert "15 checks: 0 failed, 3 warnings" in out


def test_validate_survives_a_status_page_that_cannot_be_read_at_all(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    github_status: FakeGitHubStatus,
) -> None:
    """A third party must not end `validate` with a traceback in place of the checks after it."""
    github_status.payload = "[" * 100_000 + "]" * 100_000
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] github.status: githubstatus.com did not answer" in out
    assert "[ OK ] prompt:" in out
    assert "15 checks: 0 failed, 3 warnings" in out


def test_validate_does_not_wait_on_a_status_probe_that_will_not_return(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
) -> None:
    """urllib's timeout does not reach the name lookup, and this is a human at a terminal."""
    released = threading.Event()

    def wedged() -> str | None:
        released.wait(30)  # a resolver with nowhere to ask
        return None

    monkeypatch.setattr("issuebot.cli._github_status", wedged)
    monkeypatch.setattr("issuebot.cli.GITHUB_STATUS_DEADLINE_S", 0.05)
    monkeypatch.setenv("GH_TOKEN", "t")
    try:
        assert main(["validate", "--workflow", str(GOOD)]) == 0
        out = capsys.readouterr().out
        assert "[WARN] github.status: githubstatus.com could not be read: TimeoutError" in out
        # The checks after it still ran, which is the whole point of the deadline.
        assert "[ OK ] prompt:" in out
        assert "15 checks: 0 failed, 3 warnings" in out
    finally:
        released.set()


def test_validate_survives_a_status_probe_that_raises(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
) -> None:
    def exploding() -> str | None:
        raise RuntimeError("the status page went up in smoke")

    monkeypatch.setattr("issuebot.cli._github_status", exploding)
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] github.status: githubstatus.com could not be read: RuntimeError" in out
    assert "15 checks: 0 failed, 3 warnings" in out


def test_validate_checks_the_status_page_even_without_gh(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    github_status: FakeGitHubStatus,
) -> None:
    """The page has nothing to do with `gh`, so a missing `gh` must not silence it."""
    monkeypatch.setattr("issuebot.cli._which", lambda name: None)
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 1
    assert "[ OK ] github.status: All Systems Operational" in capsys.readouterr().out


def test_validate_fails_when_the_schema_is_ahead(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    fake_database: FakeDatabase,
) -> None:
    fake_database.probe_result = Probe(
        server_version="PostgreSQL 18.1", schema_version=2, latest_version=1
    )
    assert _validate_with_database(tmp_path, monkeypatch) == 1
    out = capsys.readouterr().out
    assert (
        "[FAIL] database.url: connected (PostgreSQL 18.1); schema version 2 is newer than "
        "this issuebot knows (1)" in out
    )


def test_validate_slack_configured_ok(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] notifications.slack: configured (blocked, state_changed)" in out
    assert "15 checks: 0 failed, 1 warnings" in out
    assert "secret" not in out


def test_validate_slack_empty_events_is_ok_without_a_webhook(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    text = "---\ngithub:\n  repo: o/r\nnotifications:\n  slack:\n    events: []\n---\nBody"
    assert main(["validate", "--workflow", str(_write(tmp_path, text))]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] notifications.slack: not configured (events: [])" in out
    assert "0 failed, 1 warnings" in out


def test_validate_slack_empty_events_with_a_webhook_warns(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    text = "---\ngithub:\n  repo: o/r\nnotifications:\n  slack:\n    events: []\n---\nBody"
    assert main(["validate", "--workflow", str(_write(tmp_path, text))]) == 0
    out = capsys.readouterr().out
    assert "[WARN] notifications.slack: configured but events is empty; nothing will be sent" in out


def test_validate_slack_http_url_fails_without_printing_it(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://hooks.slack.com/services/T0/B0/plain")
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] notifications.slack: webhook_url is not an https URL" in out
    assert "plain" not in out
    assert "1 failed" in out


def test_validate_slack_unparseable_url_fails_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://[::1")
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] notifications.slack: webhook_url is not an https URL" in out
    assert "1 failed" in out
    assert "::1" not in out


def test_validate_slack_probe_posts_one_test_message(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    slack_post: FakeSlackPost,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--slack-probe", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert (
        "[ OK ] notifications.slack: configured (blocked, state_changed); test message delivered"
        in out
    )
    assert slack_post.calls == [
        {
            "url": WEBHOOK,
            "text": ":wave: issuebot validate: Slack notifications are configured for "
            "blocked, state_changed (o/r)",
        }
    ]


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (PostResult(status=403, error="HTTP Error 403: Forbidden"), "HTTP 403"),
        (
            PostResult(status=None, error="ConnectionRefusedError: refused"),
            "ConnectionRefusedError: refused",
        ),
    ],
)
def test_validate_slack_probe_reports_a_failed_post(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    slack_post: FakeSlackPost,
    result: PostResult,
    reason: str,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    slack_post.results = [result]
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--slack-probe", "--workflow", str(path)]) == 1
    out = capsys.readouterr().out
    assert f"[FAIL] notifications.slack: test message not delivered: {reason}" in out
    assert "secret" not in out


def test_validate_slack_probe_is_skipped_when_not_configured(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    slack_post: FakeSlackPost,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--slack-probe", "--workflow", str(GOOD)]) == 0
    assert "[WARN] notifications.slack: not configured;" in capsys.readouterr().out
    assert slack_post.calls == []


def test_validate_old_claude_fails(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: Callable[..., None],
) -> None:
    executables({"claude", "gh"}, version="2.1.240 (Claude Code)")
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 1
    out = capsys.readouterr().out
    assert (
        "[FAIL] claude.command: /usr/bin/claude is 2.1.240; issuebot needs 2.1.259 or newer" in out
    )


def test_validate_unknown_claude_version_warns(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: Callable[..., None],
) -> None:
    executables({"claude", "gh"}, version=None)
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] claude.command: /usr/bin/claude (version unknown: no output)" in out
    assert "0 failed, 3 warnings" in out


def test_validate_reports_a_claude_ai_login(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] claude auth: logged in (claude.ai, max)" in out
    assert out.rstrip().endswith("15 checks: 0 failed, 2 warnings")


def test_validate_reports_an_oauth_token_login(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: Callable[..., None],
) -> None:
    executables({"claude", "gh"}, auth='{"loggedIn": true, "authMethod": "oauth_token"}')
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    assert "[ OK ] claude auth: logged in (CLAUDE_CODE_OAUTH_TOKEN)" in capsys.readouterr().out


def test_validate_reports_an_api_key_login(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: Callable[..., None],
) -> None:
    status = '{"loggedIn": true, "authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"}'
    executables({"claude", "gh"}, auth=status)
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] claude auth: logged in (API key from ANTHROPIC_API_KEY)" in out


def test_validate_logged_out_claude_fails(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: Callable[..., None],
) -> None:
    executables({"claude", "gh"}, auth='{"loggedIn": false, "authMethod": "none"}')
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 1
    out = capsys.readouterr().out
    assert (
        "[FAIL] claude auth: not logged in; run claude auth login or set ANTHROPIC_API_KEY" in out
    )


def test_validate_auth_status_without_output_warns(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: Callable[..., None],
) -> None:
    executables({"claude", "gh"}, auth=None)
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] claude auth: could not read auth status (no output)" in out


def test_validate_unparseable_auth_status_warns(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: Callable[..., None],
) -> None:
    executables({"claude", "gh"}, auth="error: unknown command auth\n")
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert (
        "[WARN] claude auth: could not read auth status "
        "(unparseable output 'error: unknown command auth')" in out
    )


def test_validate_warns_when_a_login_and_an_api_key_are_both_set(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: Callable[..., None],
) -> None:
    status = '{"loggedIn": true, "authMethod": "claude.ai", "apiKeySource": "ANTHROPIC_API_KEY"}'
    executables({"claude", "gh"}, auth=status)
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert (
        "[WARN] claude auth: logged in (claude.ai) with ANTHROPIC_API_KEY also set; "
        "unset one to be sure which credential is used" in out
    )


def test_validate_never_prints_the_account_behind_the_login(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: Callable[..., None],
) -> None:
    status = (
        '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max", '
        '"email": "someone@example.com", "orgName": "Someone\'s Organization"}'
    )
    executables({"claude", "gh"}, auth=status)
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] claude auth: logged in (claude.ai, max)" in out
    assert "someone@example.com" not in out
    assert "Organization" not in out


def test_validate_prompt_that_does_not_render_fails(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nHello {{ nope }}")
    assert main(["validate", "--workflow", str(path)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] prompt: template does not render: 'nope' is undefined" in out


def test_validate_unloadable_workflow_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", "--workflow", str(INVALID)]) == 2
    out = capsys.readouterr().out
    assert out.startswith("[FAIL] workflow: ")
    assert "polling.interval_ms" in out
    assert "agnet" in out
    assert "checks:" not in out


def test_validate_missing_file_exits_two(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert main(["validate", "--workflow", str(tmp_path / "nope.md")]) == 2
    assert "not found" in capsys.readouterr().out


def test_validate_uses_env_workflow_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, executables: object
) -> None:
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    monkeypatch.setenv("ISSUEBOT_WORKFLOW", str(path))
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate"]) == 0


def test_validate_defaults_to_the_configs_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, executables: object
) -> None:
    """The default is `configs/WORKFLOW.md`: a directory is what Compose can mount (#46)."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "WORKFLOW.md").write_text("---\ngithub:\n  repo: o/r\n---\nBody", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate"]) == 0


def test_show_config_masks_secrets(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, executables: object
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token-value")
    assert main(["validate", "--workflow", str(GOOD), "--show-config"]) == 0
    out = capsys.readouterr().out
    assert "repo: example/repo" in out
    assert "interval_ms: 5000" in out
    assert "**********" in out
    assert "secret-token-value" not in out
    assert "root: /workspaces" in out


def test_log_flags_are_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, executables: object
) -> None:
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    monkeypatch.setenv("GH_TOKEN", "t")
    flags = ["--log-level", "DEBUG", "--log-format", "console"]
    assert main([*flags, "validate", "--workflow", str(path)]) == 0


def test_invalid_log_level_flag_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--log-level", "FOO", "validate"])
    assert exc.value.code == 2


def test_invalid_log_level_env_exits_two(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ISSUEBOT_LOG_LEVEL", "FOO")
    with pytest.raises(SystemExit) as exc:
        main(["validate", "--workflow", str(GOOD)])
    assert exc.value.code == 2
    assert "unknown log level" in capsys.readouterr().err


def test_lowercase_log_level_flag_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, executables: object
) -> None:
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["--log-level", "debug", "validate", "--workflow", str(path)]) == 0


def test_explicit_workflow_flag_beats_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, executables: object
) -> None:
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    monkeypatch.setenv("ISSUEBOT_WORKFLOW", str(tmp_path / "does-not-exist.md"))
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(path)]) == 0


# --- validate: network checks ----------------------------------------------------------


def test_validate_reports_auth_failure(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    fake_github: FakeGitHub,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.fail_next("auth")
    assert main(["validate", "--workflow", str(GOOD)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] gh auth: injected auth failure; run gh auth login or set GH_TOKEN" in out
    assert "[ OK ] github.repo access: example/repo (default branch main)" in out


def test_validate_reports_repo_access_failure(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    fake_github: FakeGitHub,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")

    async def failing_repo_info() -> object:
        raise GitHubError("not_found", "injected not_found failure")

    monkeypatch.setattr(fake_github, "repo_info", failing_repo_info)
    assert main(["validate", "--workflow", str(GOOD)]) == 1
    out = capsys.readouterr().out
    assert "[ OK ] gh auth: logged in as issuebot" in out
    assert "[FAIL] github.repo access: not_found: injected not_found failure" in out
    assert "[ OK ] github.labels: 5 state labels and 1 marker label present" in out


def test_validate_reports_labels_failure(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    fake_github: FakeGitHub,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")

    async def failing_missing_labels(extra: object = ()) -> object:
        raise GitHubError("transport", "injected transport failure")

    monkeypatch.setattr(fake_github, "missing_labels", failing_missing_labels)
    assert main(["validate", "--workflow", str(GOOD)]) == 1
    out = capsys.readouterr().out
    assert "[ OK ] gh auth: logged in as issuebot" in out
    assert "[ OK ] github.repo access: example/repo (default branch main)" in out
    assert "[FAIL] github.labels: transport: injected transport failure" in out


def test_validate_warns_about_missing_labels(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    fake_github: FakeGitHub,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    del fake_github.repo_labels["issuebot/rework"]
    del fake_github.repo_labels["issuebot/complete"]
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert (
        "[WARN] github.labels: missing: issuebot/rework, issuebot/complete; "
        "run issuebot labels ensure" in out
    )
    assert "0 failed, 3 warnings" in out


def test_validate_warns_about_missing_model_labels(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    fake_github: FakeGitHub,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    path = _workflow_with_root(tmp_path, claude=MODEL_CLAUDE_BLOCK)
    assert main(["validate", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] github.labels: missing: issuebot/model/sonnet; run issuebot labels ensure" in out


def test_validate_counts_the_model_labels_it_finds(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    fake_github: FakeGitHub,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.repo_labels["issuebot/model/sonnet"] = model_label_style("sonnet")
    path = _workflow_with_root(tmp_path, claude=MODEL_CLAUDE_BLOCK)
    assert main(["validate", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] github.labels: 5 state labels, 1 marker label and 1 model label present" in out


# --- labels ensure -----------------------------------------------------------------------


def test_labels_ensure_reports_each_label(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_github: FakeGitHub
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.repo_labels.clear()
    assert main(["labels", "ensure", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "[ OK ] issuebot/todo: created",
        "[ OK ] issuebot/in-progress: created",
        "[ OK ] issuebot/review: created",
        "[ OK ] issuebot/rework: created",
        "[ OK ] issuebot/complete: created",
        "[ OK ] issuebot/no-fault: created",
    ]
    assert main(["labels", "ensure", "--workflow", str(GOOD)]) == 0
    assert all(line.endswith(": unchanged") for line in capsys.readouterr().out.splitlines())


def test_labels_ensure_creates_the_model_labels_too(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.repo_labels.clear()
    path = _workflow_with_root(tmp_path, claude=MODEL_CLAUDE_BLOCK)
    assert main(["labels", "ensure", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "[ OK ] issuebot/model/sonnet: created"
    assert fake_github.repo_labels["issuebot/model/sonnet"].description == (
        "Run this issue with the sonnet model"
    )


def test_labels_ensure_reports_github_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_github: FakeGitHub
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.fail_next("transport")
    assert main(["labels", "ensure", "--workflow", str(GOOD)]) == 1
    assert "[FAIL] labels: transport: injected transport failure" in capsys.readouterr().out


def test_labels_ensure_unloadable_workflow_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["labels", "ensure", "--workflow", str(INVALID)]) == 2
    assert capsys.readouterr().out.startswith("[FAIL] workflow: ")


def test_labels_without_subcommand_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["labels"])
    assert exc.value.code == 2


# --- issues list --------------------------------------------------------------------------


def test_issues_list_prints_table_sorted_by_role(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_github: FakeGitHub
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    review = fake_github.add_issue("Fix label parsing", labels=("issuebot/review",))
    fake_github.open_pr(review.number)
    fake_github.add_issue("Add retry backoff", labels=("issuebot/in-progress",))
    fake_github.add_issue("Untracked")
    assert main(["issues", "list", "--workflow", str(GOOD)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["NUMBER", "STATE", "PR", "UPDATED", "TITLE"]
    assert lines[1].startswith("3       in_progress  -        2026-09-02T")
    assert lines[1].endswith("  Add retry backoff")
    assert lines[2].startswith("1       review       #2 open  2026-09-02T")
    assert lines[2].endswith("  Fix label parsing")
    assert len(lines) == 3


def test_issues_list_filters_by_state_and_reports_empty(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_github: FakeGitHub
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Only review", labels=("issuebot/review",))
    assert main(["issues", "list", "--workflow", str(GOOD), "--state", "todo"]) == 0
    assert capsys.readouterr().out == "no tracked issues\n"
    assert main(["issues", "list", "--workflow", str(GOOD), "--state", "review"]) == 0
    assert "Only review" in capsys.readouterr().out
    assert fake_github.calls[-1] == ("fetch_issues_by_states", ((StateLabel.REVIEW,),))


def test_issues_list_reports_github_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_github: FakeGitHub
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.fail_next("rate_limited")
    assert main(["issues", "list", "--workflow", str(GOOD)]) == 1
    assert "[FAIL] issues: rate_limited: injected rate_limited failure" in capsys.readouterr().out


def test_render_issue_table_marks_conflicts_and_aligns(make_issue: Callable[..., Issue]) -> None:
    conflict = make_issue(number=5, state=None, state_labels=("issuebot/todo", "issuebot/review"))
    todo = make_issue(number=2, title="Second")
    review = make_issue(
        number=9,
        state=StateLabel.REVIEW,
        title="Third",
        linked_pr=LinkedPr(number=10, url="https://x/pull/10", state="merged", merged_at=None),
    )
    lines = render_issue_table([conflict, review, todo]).splitlines()
    assert [line.split()[0] for line in lines[1:]] == ["2", "9", "5"]
    assert "conflict" in lines[3]
    assert "#10 merged" in lines[2]
    assert all(line.startswith(("NUMBER", "2 ", "9 ", "5 ")) for line in lines)


# --- run-once ------------------------------------------------------------------------------


class StubSession:
    """Stands in for run_session: records the call and returns a configurable RunResult."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.outcome = "succeeded"
        self.stop_reason = "issue_moved"
        self.error_category: str | None = None
        self.final_state: StateLabel | None = StateLabel.REVIEW
        self.blocker: str | None = None

    async def __call__(
        self,
        issue: Issue,
        workflow: object,
        adapter: object,
        bus: object,
        *,
        workspaces: WorkspaceManager,
        runner: object,
        attempt: int = 1,
        rework: bool = False,
        **kwargs: object,
    ) -> RunResult:
        self.calls.append({"issue": issue, "attempt": attempt, "rework": rework})
        run_id = "20260903T081200Z-abc123"
        workspace = workspaces.root / "repo-42"
        return RunResult(
            run_id=run_id,
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            attempt=attempt,
            session_id="s",
            outcome=self.outcome,  # type: ignore[arg-type]
            stop_reason=self.stop_reason,  # type: ignore[arg-type]
            error_category=self.error_category,  # type: ignore[arg-type]
            error="injected failure" if self.error_category else None,
            turns=2,
            input_tokens=45120,
            output_tokens=3004,
            cost_usd=0.31,
            duration_s=102.0,
            final_state=self.final_state,
            final_issue=None,
            workspace_path=workspace,
            log_dir=workspace / ".issuebot" / "runs" / run_id,
            blocker=self.blocker,
        )


@pytest.fixture
def stub_session(monkeypatch: pytest.MonkeyPatch) -> StubSession:
    stub = StubSession()
    monkeypatch.setattr("issuebot.cli._run_session", stub)
    return stub


class RecordingSink:
    """Stands in for LogSink: records every published event instead of logging it."""

    name = "recording"
    events: ClassVar[list[Event]] = []

    def handle(self, event: Event) -> None:
        RecordingSink.events.append(event)


@pytest.fixture
def recording_sink(monkeypatch: pytest.MonkeyPatch) -> type[RecordingSink]:
    RecordingSink.events = []
    monkeypatch.setattr("issuebot.cli.LogSink", RecordingSink)
    return RecordingSink


def _workflow_with_root(tmp_path: Path, **extra_lines: str) -> Path:
    lines = ["---", "github:", "  repo: example/repo", "workspace:", f"  root: {tmp_path / 'ws'}"]
    lines.extend(extra_lines.values())
    lines.extend(["---", "Body for `{{ issue.identifier }}`"])
    return _write(tmp_path, "\n".join(lines) + "\n")


def test_run_once_reports_missing_issue(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, stub_session: StubSession
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["run-once", "7", "--workflow", str(GOOD)]) == 1
    assert "[FAIL] issue: #7 not found" in capsys.readouterr().out
    assert stub_session.calls == []


@pytest.mark.parametrize(
    ("labels", "closed", "needle"),
    [
        (("issuebot/review",), False, "#42 is review; label it issuebot/todo or issuebot/rework"),
        (("issuebot/complete",), False, "#42 is complete"),
        ((), False, "#42 is unlabelled"),
        (("issuebot/todo", "issuebot/review"), False, "#42 carries more than one state label"),
        (("issuebot/todo",), True, "#42 is closed"),
    ],
)
def test_run_once_refuses_unrunnable_issues(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    labels: tuple[str, ...],
    closed: bool,
    needle: str,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=labels, number=42)
    if closed:
        fake_github.close_issue(42)
    assert main(["run-once", "42", "--workflow", str(GOOD)]) == 1
    assert f"[FAIL] issue: {needle}" in capsys.readouterr().out
    assert stub_session.calls == []
    assert ("set_state", (42, StateLabel.IN_PROGRESS)) not in fake_github.calls


def test_run_once_claims_a_todo_issue_and_prints_the_summary(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    recording_sink: type[RecordingSink],
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    path = _workflow_with_root(tmp_path)
    assert main(["run-once", "42", "--workflow", str(path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == (
        "run 20260903T081200Z-abc123: succeeded (issue_moved) after 2 turns in 1m42s, "
        "$0.31, 45120 in / 3004 out"
    )
    assert lines[1] == "issue #42 is now review"
    log_dir = tmp_path / "ws" / "repo-42" / ".issuebot" / "runs" / "20260903T081200Z-abc123"
    assert lines[2] == f"logs: {log_dir}"
    assert ("set_state", (42, StateLabel.IN_PROGRESS)) in fake_github.calls
    assert fake_github.issue(42).state is StateLabel.IN_PROGRESS
    [call] = stub_session.calls
    assert call["attempt"] == 1
    assert call["rework"] is False
    issue = call["issue"]
    assert isinstance(issue, Issue)
    assert issue.state is StateLabel.IN_PROGRESS
    state_changes = [event for event in recording_sink.events if isinstance(event, StateChanged)]
    [state_changed] = state_changes
    assert state_changed.from_label == "issuebot/todo"
    assert state_changed.to_label == "issuebot/in-progress"
    assert state_changed.actor == "issuebot"
    assert state_changed.issue_number == 42


def test_run_once_rework_sets_the_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    recording_sink: type[RecordingSink],
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/rework",), number=42)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert stub_session.calls[0]["rework"] is True
    assert ("set_state", (42, StateLabel.IN_PROGRESS)) in fake_github.calls
    state_changes = [event for event in recording_sink.events if isinstance(event, StateChanged)]
    [state_changed] = state_changes
    assert state_changed.from_label == "issuebot/rework"
    assert state_changed.to_label == "issuebot/in-progress"
    assert state_changed.actor == "issuebot"
    assert state_changed.issue_number == 42


class RecordingRunners:
    """Stands in for _runner_factory: records the settings each runner was built from."""

    def __init__(self) -> None:
        self.settings: list[Settings] = []

    def __call__(self, settings: Settings) -> ClaudeRunner:
        self.settings.append(settings)
        return ClaudeRunner(settings)


@pytest.fixture
def recording_runners(monkeypatch: pytest.MonkeyPatch) -> RecordingRunners:
    runners = RecordingRunners()
    monkeypatch.setattr("issuebot.cli._runner_factory", runners)
    return runners


MODEL_CLAUDE_BLOCK = "claude:\n  model: opus\n  model_labels:\n    issuebot/model/sonnet: sonnet"


def test_run_once_uses_the_model_the_issue_label_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    recording_runners: RecordingRunners,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    labels = ("issuebot/todo", "issuebot/model/sonnet")
    fake_github.add_issue("Add retry backoff", labels=labels, number=42)
    path = _workflow_with_root(tmp_path, claude=MODEL_CLAUDE_BLOCK)
    assert main(["run-once", "42", "--workflow", str(path)]) == 0
    assert [settings.claude.model for settings in recording_runners.settings] == ["sonnet"]


def test_run_once_model_option_beats_the_label_and_the_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    recording_runners: RecordingRunners,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    labels = ("issuebot/todo", "issuebot/model/sonnet")
    fake_github.add_issue("Add retry backoff", labels=labels, number=42)
    path = _workflow_with_root(tmp_path, claude=MODEL_CLAUDE_BLOCK)
    assert main(["run-once", "42", "--workflow", str(path), "--model", "fable"]) == 0
    assert [settings.claude.model for settings in recording_runners.settings] == ["fable"]


def test_run_once_in_progress_issue_is_not_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    recording_sink: type[RecordingSink],
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/in-progress",), number=42)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert all(name != "set_state" for name, _ in fake_github.calls)
    assert len(stub_session.calls) == 1
    assert not any(isinstance(event, StateChanged) for event in recording_sink.events)


def test_run_once_reports_an_exhausted_turn_budget(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    stub_session.stop_reason = "max_turns"
    stub_session.final_state = StateLabel.IN_PROGRESS
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert (
        "turn budget exhausted; issue #42 remains in_progress (the worker would escalate it)" in out
    )


def test_run_once_reports_a_blocked_stop(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    stub_session.stop_reason = "blocked"
    stub_session.blocker = "gh cannot reach api.github.com; a human must fix DNS"
    stub_session.final_state = StateLabel.IN_PROGRESS
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "succeeded (blocked) after 2 turns" in out
    assert (
        "blocked: gh cannot reach api.github.com; a human must fix DNS; issue #42 remains "
        "in_progress (the worker would escalate it)"
    ) in out


def test_run_once_reports_failure_and_exits_one(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    stub_session.outcome = "failed"
    stub_session.stop_reason = "failure"
    stub_session.error_category = "turn_failed"
    stub_session.final_state = StateLabel.IN_PROGRESS
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    out = capsys.readouterr().out
    assert "run 20260903T081200Z-abc123: failed (failure) after 2 turns" in out
    assert "error: turn_failed: injected failure" in out


def test_run_once_show_prompt_has_no_side_effects(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    path = _workflow_with_root(tmp_path)
    assert main(["run-once", "42", "--workflow", str(path), "--show-prompt"]) == 0
    assert capsys.readouterr().out == "Body for `repo-42`\n"
    assert stub_session.calls == []
    assert [name for name, _ in fake_github.calls] == [
        "fetch_issues_by_ids",
        "find_workpad_comment",
    ]
    assert fake_github.issue(42).state is StateLabel.TODO
    assert not (tmp_path / "ws").exists()


def test_run_once_show_prompt_renders_the_workpad_issuebot_resolved(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
) -> None:
    """The preview is what the first turn gets: the account's own workpad, not an impostor's."""
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    impostor = fake_github.add_comment(42, f"{WORKPAD_MARKER}\n\nnope", author="mallory")
    own = asyncio.run(fake_github.comment(42, f"{WORKPAD_MARKER}\n\n### Plan\n"))
    template = "{% if workpad %}pad {{ workpad.id }}{% else %}no pad{% endif %}"
    path = _write(tmp_path, f"---\ngithub:\n  repo: example/repo\n---\n{template}")
    assert main(["run-once", "42", "--workflow", str(path), "--show-prompt"]) == 0
    out = capsys.readouterr().out
    assert out == f"pad {own.id}\n"
    assert str(impostor.id) not in out

    async def unreachable(number: int) -> None:
        raise GitHubError("transport", "comments unreachable")

    monkeypatch.setattr(fake_github, "find_workpad_comment", unreachable)
    assert main(["run-once", "42", "--workflow", str(path), "--show-prompt"]) == 1
    assert "[FAIL] workpad: transport: comments unreachable" in capsys.readouterr().out


def test_run_once_show_prompt_reports_template_errors(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    path = _write(tmp_path, "---\ngithub:\n  repo: example/repo\n---\n{{ nope }}")
    assert main(["run-once", "42", "--workflow", str(path), "--show-prompt"]) == 1
    assert "[FAIL] prompt: template does not render" in capsys.readouterr().out


def test_run_once_attempt_increments_from_the_session_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    path = _workflow_with_root(tmp_path)
    workspace = tmp_path / "ws" / "repo-42"
    workspace.mkdir(parents=True)
    manager = WorkspaceManager(
        Settings.model_validate(
            {"github": {"repo": "example/repo"}, "workspace": {"root": str(tmp_path / "ws")}}
        ),
        environ={},
    )
    manager.write_session(
        workspace,
        SessionRecord(
            issue_number=42,
            issue_identifier="repo-42",
            run_id="old",
            session_id="old-session",
            attempt=2,
            turn_number=5,
            last_outcome="succeeded",
            updated_at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
        ),
    )
    assert main(["run-once", "42", "--workflow", str(path)]) == 0
    assert stub_session.calls[0]["attempt"] == 3


def test_run_once_reports_claim_failure(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    recording_sink: type[RecordingSink],
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)

    async def failing_set_state(number: int, state: StateLabel) -> None:
        raise GitHubError("transport", "injected transport failure")

    monkeypatch.setattr(fake_github, "set_state", failing_set_state)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    assert "[FAIL] claim: transport: injected transport failure" in capsys.readouterr().out
    assert stub_session.calls == []
    assert not any(isinstance(event, StateChanged) for event in recording_sink.events)


def test_run_once_claim_event_is_published_even_when_the_refetch_fails(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    recording_sink: type[RecordingSink],
) -> None:
    """set_state lands on GitHub even though the re-fetch after it fails; the event still fires."""
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    original_fetch = fake_github.fetch_issues_by_ids
    calls = {"n": 0}

    async def flaky_fetch(ids: object) -> list[Issue]:
        calls["n"] += 1
        if calls["n"] == 1:
            return await original_fetch(ids)  # type: ignore[arg-type]
        raise GitHubError("transport", "injected refetch failure")

    monkeypatch.setattr(fake_github, "fetch_issues_by_ids", flaky_fetch)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    assert "[FAIL] claim: transport: injected refetch failure" in capsys.readouterr().out
    assert stub_session.calls == []
    assert ("set_state", (42, StateLabel.IN_PROGRESS)) in fake_github.calls
    state_changes = [event for event in recording_sink.events if isinstance(event, StateChanged)]
    [state_changed] = state_changes
    assert state_changed.from_label == "issuebot/todo"
    assert state_changed.to_label == "issuebot/in-progress"


def test_run_once_unloadable_workflow_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run-once", "42", "--workflow", str(INVALID)]) == 2
    assert capsys.readouterr().out.startswith("[FAIL] workflow: ")


def test_run_once_requires_a_number() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["run-once", "forty-two"])
    assert exc.value.code == 2


def test_not_runnable_messages(make_issue: Callable[..., Issue]) -> None:
    labels = GitHubSettings(repo="o/r").labels
    assert not_runnable(make_issue(), labels) is None
    assert not_runnable(make_issue(state=StateLabel.IN_PROGRESS), labels) is None
    closed = make_issue(github_state="closed", dispatchable=False)
    assert not_runnable(closed, labels) == "is closed"
    assert not_runnable(make_issue(state=StateLabel.COMPLETE), labels) == (
        "is complete; label it issuebot/todo or issuebot/rework first"
    )


def test_render_run_summary_singular_turn_and_missing_log_dir() -> None:
    result = RunResult(
        run_id="r",
        issue_number=7,
        issue_identifier="repo-7",
        attempt=1,
        session_id="s",
        outcome="succeeded",
        stop_reason="issue_missing",
        error_category=None,
        error=None,
        turns=1,
        input_tokens=10,
        output_tokens=2,
        cost_usd=0.5,
        duration_s=59.9,
        final_state=None,
        final_issue=None,
        workspace_path=None,
        log_dir=None,
    )
    assert render_run_summary(result) == (
        "run r: succeeded (issue_missing) after 1 turn in 0m59s, $0.50, 10 in / 2 out\n"
        "issue #7 is now unlabelled\n"
    )


def test_run_once_posts_the_claim_to_slack(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    slack_post: FakeSlackPost,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert "issue #42 is now review" in capsys.readouterr().out
    (call,) = slack_post.calls
    assert call["url"] == WEBHOOK
    assert call["text"] == (
        ":hammer_and_wrench: <https://github.com/example/repo/issues/42|repo-42> "
        "`issuebot/todo` → `issuebot/in-progress` by issuebot"
    )


def test_run_once_skips_the_slack_sink_for_a_non_https_webhook(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    slack_post: FakeSlackPost,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://hooks.slack.com/services/T0/B0/plain")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert "issue #42 is now review" in capsys.readouterr().out
    assert slack_post.calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="the fakes are POSIX shebang scripts")
def test_run_once_end_to_end_with_the_fakes(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
) -> None:
    fakes = Path(__file__).parent / "fakes"
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("PATH", f"{fakes}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_FAKE_SCENARIO", raising=False)
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    path = _workflow_with_root(
        tmp_path,
        agent="agent:\n  max_turns: 1",
        claude=f"claude:\n  command: {fakes / 'claude'}",
    )
    assert main(["run-once", "42", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert "succeeded (max_turns) after 1 turn" in out
    assert "turn budget exhausted; issue #42 remains in_progress" in out
    workspace = tmp_path / "ws" / "repo-42"
    assert (workspace / ".git").is_dir()
    assert (workspace / ".issuebot" / "session.json").exists()
    runs = list((workspace / ".issuebot" / "runs").iterdir())
    assert len(runs) == 1
    assert (runs[0] / "turn-1.jsonl").exists()
    assert (runs[0] / "turn-1.prompt.md").read_text() == "Body for `repo-42`"


# --- worker --------------------------------------------------------------------------------


class StubOrchestrator:
    """Stands in for Orchestrator: records its construction and plays one scripted run()."""

    instances: ClassVar[list[StubOrchestrator]] = []
    next_problems: ClassVar[list[str] | None] = None
    next_sigterm: ClassVar[bool] = False
    next_event: ClassVar[Event | None] = None

    def __init__(self, workflow: object, **kwargs: object) -> None:
        self.workflow = workflow
        self.kwargs = kwargs
        self.stops = 0
        self.refreshes = 0
        StubOrchestrator.instances.append(self)

    def request_stop(self) -> None:
        self.stops += 1

    def request_refresh(self) -> None:
        self.refreshes += 1

    async def run(self) -> None:
        if StubOrchestrator.next_problems is not None:
            raise OrchestratorStartupError(StubOrchestrator.next_problems)
        if StubOrchestrator.next_event is not None:
            self.kwargs["bus"].publish(StubOrchestrator.next_event)  # type: ignore[attr-defined]
        if StubOrchestrator.next_sigterm:
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(200):
                await asyncio.sleep(0.01)
                if self.stops:
                    return
            raise AssertionError("SIGTERM did not reach request_stop")


@pytest.fixture
def stub_orchestrator(monkeypatch: pytest.MonkeyPatch) -> type[StubOrchestrator]:
    StubOrchestrator.instances = []
    StubOrchestrator.next_problems = None
    StubOrchestrator.next_sigterm = False
    StubOrchestrator.next_event = None
    monkeypatch.setattr("issuebot.cli._orchestrator_factory", StubOrchestrator)
    return StubOrchestrator


def test_worker_unloadable_workflow_exits_two(
    stub_orchestrator: type[StubOrchestrator], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["worker", "--workflow", str(INVALID)]) == 2
    assert "[FAIL] workflow:" in capsys.readouterr().out
    assert stub_orchestrator.instances == []


def test_worker_reports_startup_failures(
    tmp_path: Path,
    stub_orchestrator: type[StubOrchestrator],
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub_orchestrator.next_problems = [
        "'gh' not found on PATH",
        "labels missing: a",
        "claude auth: not logged in; run claude auth login or set ANTHROPIC_API_KEY",
    ]
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "[FAIL] startup: 'gh' not found on PATH",
        "[FAIL] startup: labels missing: a",
        "[FAIL] startup: claude auth: not logged in; run claude auth login or set "
        "ANTHROPIC_API_KEY",
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM handling is POSIX")
def test_worker_stops_on_sigterm_and_wires_the_seams(
    tmp_path: Path,
    stub_orchestrator: type[StubOrchestrator],
    stub_session: StubSession,
    fake_github: FakeGitHub,
    executables: Callable[[set[str]], None],
) -> None:
    stub_orchestrator.next_sigterm = True
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    instance = stub_orchestrator.instances[0]
    assert instance.stops == 1
    assert instance.workflow.config.github.repo == "example/repo"  # type: ignore[attr-defined]
    kwargs = instance.kwargs
    assert kwargs["adapter_factory"](None) is fake_github  # type: ignore[operator]
    assert kwargs["run_session"] is stub_session
    assert kwargs["which"]("gh") == "/usr/bin/gh"  # type: ignore[operator]
    assert kwargs["claude_auth"]("/usr/bin/claude", {}) == LOGGED_IN  # type: ignore[operator]
    assert [sink.name for sink in kwargs["bus"].sinks] == ["log"]  # type: ignore[attr-defined]


def test_worker_wires_the_slack_sink_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    slack_post: FakeSlackPost,
) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    stub_orchestrator.next_event = StateChanged(
        issue_number=7,
        issue_identifier="repo-7",
        from_label="issuebot/in-progress",
        to_label="issuebot/review",
        actor="agent",
        pr_url="https://github.com/example/repo/pull/8",
    )
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    instance = stub_orchestrator.instances[0]
    assert [sink.name for sink in instance.kwargs["bus"].sinks] == ["log", "slack"]  # type: ignore[attr-defined]
    (call,) = slack_post.calls
    assert call["text"] == (
        ":eyes: <https://github.com/example/repo/issues/7|repo-7> `issuebot/in-progress` → "
        "`issuebot/review` by the agent · <https://github.com/example/repo/pull/8|PR #8>"
    )


def test_worker_skips_the_slack_sink_for_a_non_https_webhook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_orchestrator: type[StubOrchestrator],
    slack_post: FakeSlackPost,
) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://hooks.slack.com/services/T0/B0/plain")
    stub_orchestrator.next_event = StateChanged(
        issue_number=7,
        issue_identifier="repo-7",
        from_label="issuebot/in-progress",
        to_label="issuebot/review",
        actor="agent",
        pr_url="https://github.com/example/repo/pull/8",
    )
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    instance = stub_orchestrator.instances[0]
    assert [sink.name for sink in instance.kwargs["bus"].sinks] == ["log"]  # type: ignore[attr-defined]
    assert slack_post.calls == []
    assert "plain" not in capsys.readouterr().err


def test_worker_requires_no_arguments(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["worker", "extra"])
    assert exc.value.code == 2


# --- migrate, status, stats, refresh ---------------------------------------------------------


def _db_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, url: str | None = DB_URL
) -> Path:
    if url is not None:
        monkeypatch.setenv("DATABASE_URL", url)
    return _write(tmp_path, "---\ngithub:\n  repo: example/repo\n---\nBody")


@pytest.mark.parametrize(
    "command",
    [
        ["migrate"],
        ["status"],
        ["stats"],
        ["refresh"],
    ],
)
def test_database_commands_need_a_configured_url(
    command: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    path = _db_workflow(tmp_path, monkeypatch, url=None)
    assert main([*command, "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == (
        "[FAIL] database: not configured; export DATABASE_URL or set database.url: $VAR\n"
    )
    assert fake_database.urls == []


@pytest.mark.parametrize(
    "command",
    [
        ["migrate"],
        ["status"],
        ["stats"],
        ["refresh"],
    ],
)
def test_database_commands_exit_two_on_an_unloadable_workflow(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*command, "--workflow", str(INVALID)]) == 2
    assert "[FAIL] workflow:" in capsys.readouterr().out


@pytest.mark.parametrize("command", [["migrate"], ["status"], ["stats"], ["refresh"]])
def test_a_keyword_value_dsn_is_refused_on_every_database_command(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
    command: list[str],
) -> None:
    """#105: the shape check ``validate`` performs now sits on the path the other commands take,
    in the facade, and the line that reports it names neither the DSN nor its password."""
    path = _db_workflow(tmp_path, monkeypatch, url=KEYWORD_DSN)
    assert main([*command, "--workflow", str(path)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("[FAIL] database: database.url is not a postgresql:// URL")
    assert "s3cretpassword" not in out and "db.example" not in out
    assert fake_database.urls == [] and fake_database.migrations == 0


def test_migrate_reports_what_it_applied(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    fake_database.migrate_result = MigrationResult(applied=("0001_initial",), version=1)
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["migrate", "--workflow", str(path)]) == 0
    assert capsys.readouterr().out == (
        "[ OK ] migration 0001_initial: applied\n[ OK ] database: schema version 1\n"
    )
    assert fake_database.urls == [DB_URL]
    assert fake_database.migrations == 1


def test_migrate_reports_nothing_to_do_and_failures(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["migrate", "--workflow", str(path)]) == 0
    assert capsys.readouterr().out == "[ OK ] database: unchanged at schema version 2\n"
    fake_database.migrate_error = StoreUnavailableError("cannot connect: refused")
    assert main(["migrate", "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"


SNAPSHOT_AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
SNAPSHOT_DATA: dict[str, Any] = {
    "at": SNAPSHOT_AT.isoformat(),
    "workflow_path": "/configs/WORKFLOW.md",
    "workflow_mtime_ns": 1,
    "config_valid": True,
    "config_error": None,
    "poll_interval_ms": 30000,
    "max_concurrent_agents": 2,
    "tick_count": 42,
    "last_tick_at": "2026-09-04T11:59:58+00:00",
    "running": [
        {
            "issue_number": 7,
            "identifier": "repo-7",
            "title": "Seven",
            "url": "https://github.com/example/repo/issues/7",
            "state": "in_progress",
            "attempt": 2,
            "rework": False,
            "resumed": True,
            "run_id": "20260904T115000Z-abc123",
            "session_id": "s",
            "started_at": "2026-09-04T11:50:00+00:00",
            "last_activity_at": "2026-09-04T11:59:00+00:00",
            "last_event": "turn_activity:Edit",
            "turns": 1,
            "stop_cause": None,
        }
    ],
    "retrying": [
        {
            "issue_number": 9,
            "identifier": "repo-9",
            "url": "https://github.com/example/repo/issues/9",
            "attempt": 3,
            "kind": "failure",
            "due_at": "2026-09-04T12:00:40+00:00",
            "error": "turn_failed: boom",
        }
    ],
    "totals": {
        "input_tokens": 1000,
        "output_tokens": 234,
        "cost_usd": 1.2345,
        "seconds_running": 321.4,
        "total_tokens": 1234,
    },
    "counters": {
        "runs_started": 3,
        "runs_ended": 2,
        "issues_completed": 1,
        "issues_cancelled": 0,
        "blocked": 1,
    },
}


def test_render_status_lists_running_and_retrying_entries() -> None:
    row = SnapshotRow(
        at=SNAPSHOT_AT, written_at=SNAPSHOT_AT + timedelta(seconds=1), data=SNAPSHOT_DATA
    )
    text = render_status(row, now=SNAPSHOT_AT + timedelta(seconds=13))
    assert text.splitlines() == [
        "snapshot: 2026-09-04T12:00:00Z (written 2026-09-04T12:00:01Z, 12 s ago)",
        "workflow: /configs/WORKFLOW.md (config valid)",
        "tick 42, last tick 2026-09-04T11:59:58Z, poll 30000 ms, 2 slots",
        "running: 1",
        "  NUMBER  ATTEMPT  TURNS  RUN_ID                   LAST_EVENT          "
        "STARTED               IDENTIFIER",
        "  7       2        1      20260904T115000Z-abc123  turn_activity:Edit  "
        "2026-09-04T11:50:00Z  repo-7",
        "retrying: 1",
        "  NUMBER  KIND     ATTEMPT  DUE                   ERROR",
        "  9       failure  3        2026-09-04T12:00:40Z  turn_failed: boom",
        "totals: 3 runs started, 2 ended, 1 completed, 0 cancelled, 1 blocked; 1234 tokens, "
        "$1.23, 321 s running",
    ]


def test_render_status_names_the_overlay_in_force() -> None:
    """The second place that answers "is the worker running my overrides?"."""
    data = dict(SNAPSHOT_DATA)
    data["workflow_overlay_path"] = "/configs/WORKFLOW.local.md"
    row = SnapshotRow(at=SNAPSHOT_AT, written_at=SNAPSHOT_AT, data=data)
    lines = render_status(row, now=SNAPSHOT_AT).splitlines()
    assert lines[1] == "workflow: /configs/WORKFLOW.md + /configs/WORKFLOW.local.md (config valid)"
    # None, or a snapshot from a worker that predates the field, reads as before.
    data["workflow_overlay_path"] = None
    lines = render_status(row, now=SNAPSHOT_AT).splitlines()
    assert lines[1] == "workflow: /configs/WORKFLOW.md (config valid)"


def test_render_status_names_a_github_hold(tmp_path: Path) -> None:
    """The surface #88 is about: an operator asking a quiet worker why the board is not moving."""
    data = dict(SNAPSHOT_DATA)
    data["dispatch_hold"] = {
        "kind": "github",
        "reason": (
            "GitHub is not answering this worker: transport: http 502: Bad Gateway "
            "\u2014 githubstatus.com: Pull Requests, major outage"
        ),
        "since": "2026-09-04T11:55:00+00:00",
    }
    row = SnapshotRow(at=SNAPSHOT_AT, written_at=SNAPSHOT_AT, data=data)
    lines = render_status(row, now=SNAPSHOT_AT).splitlines()
    assert lines[3] == (
        "dispatch: held (github) since 2026-09-04T11:55:00Z: "
        "GitHub is not answering this worker: transport: http 502: Bad Gateway "
        "\u2014 githubstatus.com: Pull Requests, major outage"
    )


def test_render_status_names_a_held_dispatch() -> None:
    """A held worker keeps ticking, so nothing else in the snapshot says it has stopped."""
    data = dict(SNAPSHOT_DATA)
    data["dispatch_hold"] = {
        "kind": "auth",
        "reason": "claude authentication unavailable: not logged in",
        "since": "2026-09-04T11:55:00+00:00",
    }
    row = SnapshotRow(at=SNAPSHOT_AT, written_at=SNAPSHOT_AT, data=data)
    lines = render_status(row, now=SNAPSHOT_AT).splitlines()
    assert lines[3] == (
        "dispatch: held (auth) since 2026-09-04T11:55:00Z: "
        "claude authentication unavailable: not logged in"
    )
    # A snapshot without a hold, or with a hold naming no reason, says nothing.
    assert not any(
        line.startswith("dispatch:")
        for line in render_status(
            SnapshotRow(at=SNAPSHOT_AT, written_at=SNAPSHOT_AT, data=SNAPSHOT_DATA), now=SNAPSHOT_AT
        ).splitlines()
    )
    data["dispatch_hold"] = {"kind": "auth"}
    assert not any(
        line.startswith("dispatch:") for line in render_status(row, now=SNAPSHOT_AT).splitlines()
    )


def test_render_status_copes_with_an_empty_or_broken_snapshot() -> None:
    row = SnapshotRow(at=SNAPSHOT_AT, written_at=SNAPSHOT_AT, data={"config_error": "bad yaml"})
    text = render_status(row, now=SNAPSHOT_AT - timedelta(seconds=5))
    assert text.splitlines() == [
        "snapshot: 2026-09-04T12:00:00Z (written 2026-09-04T12:00:00Z, 0 s ago)",
        "workflow: None (config error: bad yaml)",
        "tick None, last tick -, poll None ms, None slots",
        "running: 0",
        "retrying: 0",
        "totals: 0 runs started, 0 ended, 0 completed, 0 cancelled, 0 blocked; 0 tokens, $0.00, "
        "0 s running",
    ]


def test_status_prints_the_snapshot_or_says_there_is_none(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["status", "--workflow", str(path)]) == 0
    assert capsys.readouterr().out == (
        "no runtime snapshot yet (has the worker run against this database?)\n"
    )
    fake_database.queries_obj.snapshot_row = SnapshotRow(
        at=SNAPSHOT_AT, written_at=SNAPSHOT_AT, data=SNAPSHOT_DATA
    )
    assert main(["status", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("snapshot: 2026-09-04T12:00:00Z (written 2026-09-04T12:00:00Z, ")
    assert "running: 1" in out
    fake_database.queries_obj.error = StoreError("UndefinedTable: relation does not exist")
    assert main(["status", "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == "[FAIL] database: UndefinedTable: relation does not exist\n"


def test_render_stats() -> None:
    view = StatsView(
        closed_1d=1,
        closed_7d=12,
        runs_1d=3,
        runs_7d=45,
        by_state={"todo": 2, "in_progress": 1, "review": 0, "rework": 0, "complete": 12},
        series=[
            DailyPoint(day=date(2026, 9, 3), closed=11, runs=42),
            DailyPoint(day=date(2026, 9, 4), closed=1, runs=3),
        ],
    )
    assert render_stats(view).splitlines() == [
        "WINDOW  CLOSED  RUNS",
        "1d      1       3",
        "7d      12      45",
        "issues: todo 2, in_progress 1, review 0, rework 0, complete 12",
        "",
        "DAY         CLOSED  RUNS",
        "2026-09-03  11      42",
        "2026-09-04  1       3",
    ]


def test_stats_prints_the_windows_and_the_series(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    queries = fake_database.queries_obj
    queries.closed = {1: 1, 7: 2}
    queries.runs = {1: 3, 7: 4}
    queries.counts["review"] = 1
    queries.counts["complete"] = 73  # from state_counts, so no COMPLETE_LIMIT cap
    queries.series = [DailyPoint(day=date(2026, 9, 4), closed=1, runs=3)]
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["stats", "--workflow", str(path), "--days", "3"]) == 0
    out = capsys.readouterr().out
    assert "1d      1       3" in out
    assert "7d      2       4" in out
    assert "issues: todo 0, in_progress 0, review 1, rework 0, complete 73" in out
    assert out.endswith("DAY         CLOSED  RUNS\n2026-09-04  1       3\n")
    assert queries.days_asked == 3
    assert "issues_by_state" not in queries.calls
    for days in ("0", str(MAX_WINDOW_DAYS + 1)):
        assert main(["stats", "--workflow", str(path), "--days", days]) == 1
        assert capsys.readouterr().out == (
            f"[FAIL] stats: --days must be between 1 and {MAX_WINDOW_DAYS}\n"
        )
    assert main(["stats", "--workflow", str(path), "--days", str(MAX_WINDOW_DAYS)]) == 0
    assert queries.days_asked == MAX_WINDOW_DAYS
    capsys.readouterr()
    queries.error = StoreUnavailableError("cannot connect: refused")
    assert main(["stats", "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"


class FakeServe:
    """Stands in for cli._serve: records the app and the bind instead of running uvicorn."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, str, int]] = []

    async def __call__(self, app: object, *, host: str, port: int) -> None:
        self.calls.append((app, host, port))


@pytest.fixture
def fake_serve(monkeypatch: pytest.MonkeyPatch) -> FakeServe:
    fake = FakeServe()
    monkeypatch.setattr("issuebot.cli._serve", fake)
    return fake


WEB_PASSWORD = "dashboard-password-for-tests"


@pytest.fixture
def web_password(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("ISSUEBOT_WEB_PASSWORD", WEB_PASSWORD)
    return WEB_PASSWORD


def test_web_migrates_then_serves_on_the_defaults(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    assert main(["web"]) == 0
    assert fake_database.migrations == 1 and fake_database.urls == [DB_URL]
    ((app, host, port),) = fake_serve.calls
    # Loopback by default (#73): placement hardens the gate rather than standing in for it.
    assert (host, port) == ("127.0.0.1", 8080)
    assert getattr(app, "title", None) == "issuebot"
    err = capsys.readouterr().err
    assert "web_started" in err and "s3cret" not in err and WEB_PASSWORD not in err


def test_web_refuses_a_keyword_value_dsn_before_serving(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", KEYWORD_DSN)
    assert main(["web"]) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("[FAIL] database: database.url is not a postgresql:// URL")
    assert "s3cretpassword" not in captured.out + captured.err
    assert fake_serve.calls == [] and fake_database.urls == []


def test_web_gates_the_app_with_the_password_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    from base64 import b64encode

    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", DB_URL)
    assert main(["web"]) == 0
    ((app, _host, _port),) = fake_serve.calls
    client = TestClient(app)  # type: ignore[arg-type]
    assert client.get("/api/v1/repos").status_code == 401
    token = b64encode(f":{web_password}".encode()).decode()
    assert (
        client.get("/api/v1/repos", headers={"Authorization": f"Basic {token}"}).status_code == 200
    )


def test_web_needs_its_password(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    for value in (None, ""):
        if value is None:
            monkeypatch.delenv("ISSUEBOT_WEB_PASSWORD", raising=False)
        else:
            monkeypatch.setenv("ISSUEBOT_WEB_PASSWORD", value)
        assert main(["web"]) == 1
        assert capsys.readouterr().out == (
            "[FAIL] web: not configured; export ISSUEBOT_WEB_PASSWORD\n"
        )
    assert fake_database.urls == [] and fake_serve.calls == []


def test_web_takes_the_bind_and_port_from_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    assert main(["web", "--bind", "0.0.0.0", "--port", "0"]) == 0
    ((_app, host, port),) = fake_serve.calls
    assert (host, port) == ("0.0.0.0", 0)


def test_web_reads_no_workflow(
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("ISSUEBOT_WORKFLOW", "/nowhere/WORKFLOW.md")
    assert main(["web"]) == 0
    with pytest.raises(SystemExit) as exc:
        main(["web", "--workflow", "x"])
    assert exc.value.code == 2


def test_web_needs_database_url(
    capsys: pytest.CaptureFixture[str],
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    assert main(["web"]) == 1
    assert capsys.readouterr().out == "[FAIL] database: not configured; export DATABASE_URL\n"
    assert fake_database.urls == [] and fake_serve.calls == []


def test_web_fails_fast_when_the_migration_fails(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    fake_database.migrate_error = StoreUnavailableError("cannot connect: refused")
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    assert main(["web"]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"
    assert fake_serve.calls == []


def test_web_rejects_a_port_out_of_range(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    assert main(["web", "--port", "70000"]) == 1
    assert capsys.readouterr().out == "[FAIL] web: --port must be between 0 and 65535\n"
    assert fake_serve.calls == []


def test_web_exits_one_when_uvicorn_cannot_bind(
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    web_password: str,
) -> None:
    async def refuse(app: object, *, host: str, port: int) -> None:
        raise SystemExit(3)  # what uvicorn's startup() does on a bind failure

    monkeypatch.setattr("issuebot.cli._serve", refuse)
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    assert main(["web"]) == 1
    assert fake_database.migrations == 1


def test_refresh_notifies_and_reports_failures(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["refresh", "--workflow", str(path)]) == 0
    assert capsys.readouterr().out == "[ OK ] refresh: notified issuebot_refresh for example/repo\n"
    assert fake_database.notified == 1
    fake_database.notify_error = StoreUnavailableError("cannot connect: refused")
    assert main(["refresh", "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"


# --- the database in run-once and worker ------------------------------------------------------


def test_run_once_records_the_issue_and_the_claim_in_the_database(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert "issue #42 is now review" in capsys.readouterr().out
    assert fake_database.migrations == 1
    assert fake_database.labels == GitHubLabels()
    store = fake_database.store_obj
    assert [event.kind for event in store.events] == ["state_changed"]  # the stub session is silent
    assert [snapshot.issue.state for snapshot in store.issues[-1]] == [StateLabel.IN_PROGRESS]
    assert store.closed


def test_run_once_fails_before_running_when_migration_fails(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_database.migrate_error = StoreUnavailableError("cannot connect: refused")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"
    assert stub_session.calls == []
    assert fake_github.issue(42).state is StateLabel.TODO


def test_worker_wires_the_database_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    stub_orchestrator.next_event = StateChanged(
        issue_number=7,
        issue_identifier="repo-7",
        from_label="issuebot/in-progress",
        to_label="issuebot/review",
        actor="agent",
    )
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert fake_database.migrations == 1
    instance = stub_orchestrator.instances[0]
    kwargs = instance.kwargs
    assert [sink.name for sink in kwargs["bus"].sinks] == ["log", "postgres"]  # type: ignore[attr-defined]
    (postgres,) = [sink for sink in kwargs["bus"].sinks if sink.name == "postgres"]  # type: ignore[attr-defined]
    assert kwargs["on_snapshot"] == postgres.record_snapshot
    assert kwargs["on_issues"] == postgres.record_issues
    assert postgres._description == fake_database.description
    (listener,) = fake_database.listeners
    assert listener.on_notify == instance.request_refresh
    assert listener.started and listener.closed
    store = fake_database.store_obj
    assert [event.kind for event in store.events] == ["state_changed"]
    assert store.closed


def test_the_sink_captures_turns_through_the_deployment_scrubber(tmp_path: Path) -> None:
    """The worker's own token, put into the agent's environment by issuebot, and the home
    directory every path names, are what the capture masks on top of the credential shapes."""
    config = Settings.model_validate(
        {"github": {"repo": "acme/widgets", "token": "literal-token-value"}}
    )
    scrubber = _deployment_scrubber(
        config, {"HOME": "/home/alice", "ANTHROPIC_API_KEY": "key-value-1234"}
    )
    capture = _turn_capture(scrubber)
    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "result": "env: GH_TOKEN=literal-token-value key-value-1234 at /home/alice/ws",
        }
    )
    (tmp_path / "turn-1.jsonl").write_text(line + "\n")
    (turn,) = capture(tmp_path)
    assert turn.result_text == "env: GH_TOKEN=*** *** at ~/ws"
    assert "literal-token-value" not in turn.stream and "key-value-1234" not in turn.stream


def test_worker_hands_the_orchestrator_the_deployment_scrubber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_orchestrator: type[StubOrchestrator]
) -> None:
    """The blocked escape writes a log directory on the public issue; the scrubber that knows
    the home directory is the one the worker built (#91)."""
    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setenv("SOME_API_KEY", "key-value-1234")
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    scrubber = stub_orchestrator.instances[0].kwargs["scrubber"]
    assert isinstance(scrubber, Scrubber)
    assert scrubber.scrub("key-value-1234 at /home/alice/ws") == "*** at ~/ws"


def test_worker_seeds_the_orchestrator_with_the_last_stored_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    """Restarting is how the worker is deployed, so the limits tile must survive one."""
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    limits = RateLimits(
        five_hour=RateLimitWindow(utilization=0.42, resets_at=SEED_AT),
        seven_day=RateLimitWindow(utilization=0.32, resets_at=SEED_AT),
        observed_at=SEED_AT,
    )
    fake_database.queries_obj.snapshot_row = _snapshot_row(limits)
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert stub_orchestrator.instances[0].kwargs["initial_rate_limits"] == limits


def test_worker_seeds_nothing_when_the_snapshot_has_no_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_database.queries_obj.snapshot_row = _snapshot_row(None)
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert stub_orchestrator.instances[0].kwargs["initial_rate_limits"] is None


def test_worker_starts_when_the_seed_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    """A tile losing its last figure is no reason to refuse to start."""
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_database.queries_obj.error = DatabaseError("connection refused")
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert stub_orchestrator.instances[0].kwargs["initial_rate_limits"] is None


def test_worker_without_a_database_seeds_nothing(
    tmp_path: Path, stub_orchestrator: type[StubOrchestrator], fake_database: FakeDatabase
) -> None:
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert stub_orchestrator.instances[0].kwargs["initial_rate_limits"] is None
    assert stub_orchestrator.instances[0].kwargs["initial_ledger"] == {}


def test_worker_seeds_the_admission_ledger_from_the_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    """#112: a budget a deployment resets is not a ceiling, and this worker deploys by restart."""
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_database.queries_obj.ledger_rows = [
        LedgerRow(
            identifier="repo-7",
            failures=2,
            runs=5,
            turns=9,
            cost_usd=3.25,
            last_run_at=SEED_AT,
        )
    ]
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert stub_orchestrator.instances[0].kwargs["initial_ledger"] == {
        "repo-7": IssueLedger(failures=2, runs=5, turns=9, cost_usd=3.25, last_run_at=SEED_AT)
    }


def test_worker_starts_when_the_ledger_seed_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    """Like the limits tile: a budget read that fails costs history, never the start."""
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_database.queries_obj.error = DatabaseError("connection refused")
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert stub_orchestrator.instances[0].kwargs["initial_ledger"] == {}


def _snapshot_row(limits: RateLimits | None) -> SnapshotRow:
    data = RuntimeSnapshot(
        at=SEED_AT,
        workflow_path="/configs/WORKFLOW.md",
        workflow_mtime_ns=1,
        config_valid=True,
        config_error=None,
        dispatch_hold=None,
        poll_interval_ms=30_000,
        max_concurrent_agents=2,
        tick_count=1,
        last_tick_at=SEED_AT,
        running=(),
        retrying=(),
        totals=ClaudeTotals(),
        counters=Counters(),
        credential="subscription",
        rate_limits=limits,
    ).to_dict()
    return SnapshotRow(at=SEED_AT, written_at=SEED_AT, data=data)


def test_worker_closes_the_sinks_when_the_listener_close_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_database.listener_close_error = RuntimeError("listener close failed")
    with pytest.raises(RuntimeError, match="listener close failed"):
        main(["worker", "--workflow", str(_workflow_with_root(tmp_path))])
    (listener,) = fake_database.listeners
    assert listener.closed
    assert fake_database.store_obj.closed


def test_worker_without_a_database_passes_no_callbacks(
    tmp_path: Path,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    kwargs = stub_orchestrator.instances[0].kwargs
    assert (kwargs["on_snapshot"], kwargs["on_issues"]) == (None, None)
    assert fake_database.urls == []
    assert fake_database.listeners == []


def test_run_once_refuses_a_keyword_value_dsn_before_claiming(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", KEYWORD_DSN)
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    out = capsys.readouterr().out
    assert out.startswith("[FAIL] database: database.url is not a postgresql:// URL")
    assert "s3cretpassword" not in out
    assert stub_session.calls == [] and fake_database.urls == []
    assert fake_github.issue(42).state is StateLabel.TODO


def test_worker_refuses_a_keyword_value_dsn_before_the_orchestrator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("DATABASE_URL", KEYWORD_DSN)
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    out = capsys.readouterr().out
    assert out.startswith("[FAIL] database: database.url is not a postgresql:// URL")
    assert "s3cretpassword" not in out
    assert stub_orchestrator.instances == [] and fake_database.urls == []


def test_worker_fails_before_the_orchestrator_when_migration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_database.migrate_error = StoreUnavailableError("cannot connect: refused")
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"
    assert stub_orchestrator.instances == []
    assert fake_database.listeners == []


def test_worker_registers_its_repository_before_the_sinks_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    path = _workflow_with_root(tmp_path)
    assert main(["worker", "--workflow", str(path)]) == 0
    ((repo, labels, workflow_path),) = fake_database.registrations
    assert (repo, workflow_path) == ("example/repo", str(path))
    assert labels == GitHubLabels()
    assert fake_database.repo == "example/repo"  # the store was built for this repository
    (listener,) = fake_database.listeners
    assert listener.repo == "example/repo"


def test_worker_fails_fast_when_registration_fails(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_database.register_error = StoreUnavailableError("cannot connect: refused")
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"
    assert stub_orchestrator.instances == []


def test_run_once_registers_its_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubSession,
    fake_github: FakeGitHub,
    fake_database: FakeDatabase,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    path = _workflow_with_root(tmp_path)
    assert main(["run-once", "42", "--workflow", str(path)]) == 0
    ((repo, labels, workflow_path),) = fake_database.registrations
    assert (repo, labels, workflow_path) == ("example/repo", GitHubLabels(), str(path))
    assert fake_database.repo == "example/repo"


def test_refresh_names_its_repository(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
) -> None:
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["refresh", "--workflow", str(path)]) == 0
    assert capsys.readouterr().out == "[ OK ] refresh: notified issuebot_refresh for example/repo\n"
    assert fake_database.notified_repos == ["example/repo"]


def test_status_and_stats_read_their_own_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_database: FakeDatabase
) -> None:
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["status", "--workflow", str(path)]) == 0
    assert main(["stats", "--workflow", str(path)]) == 0
    assert fake_database.queries_obj.scoped_repos == ["example/repo", "example/repo"]


# --- agent.run_as (#75) -----------------------------------------------------------------


def test_validate_reports_the_session_account_when_the_delegation_works(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, executables: object
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token-value")
    monkeypatch.setenv("ISSUEBOT_AGENT_USER", "agent")
    probed: list[str] = []
    monkeypatch.setattr("issuebot.cli._run_as_probe", lambda user, environ: probed.append(user))
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] agent.run_as: agent; the session runs as a separate account" in out
    assert probed == ["agent"]
    assert "15 checks: 0 failed, 1 warnings" in out


def test_validate_fails_when_the_session_account_cannot_be_reached(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, executables: object
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token-value")
    monkeypatch.setenv("ISSUEBOT_AGENT_USER", "agent")
    monkeypatch.setattr(
        "issuebot.cli._run_as_probe",
        lambda user, environ: f"cannot run as {user!r}: sudo: a password is required",
    )
    assert main(["validate", "--workflow", str(GOOD)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] agent.run_as: cannot run as 'agent': sudo: a password is required" in out
    assert "15 checks: 1 failed, 1 warnings" in out
