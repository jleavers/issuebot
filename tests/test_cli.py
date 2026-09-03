"""Tests for the command-line entry point."""

import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from issuebot import __version__
from issuebot.agent import RunResult, SessionRecord, WorkspaceManager
from issuebot.cli import main, not_runnable, render_issue_table, render_run_summary
from issuebot.config import GitHubSettings, Settings
from issuebot.github import FakeGitHub, GitHubError, Issue, LinkedPr, StateLabel

FIXTURES = Path(__file__).parent / "fixtures" / "workflows"
GOOD = FIXTURES / "good.md"
INVALID = FIXTURES / "invalid.md"


@pytest.fixture
def executables(monkeypatch: pytest.MonkeyPatch) -> Callable[[set[str]], None]:
    """Pretend the given executable names exist on PATH and report Claude Code 2.1.259."""

    def install(names: set[str], version: str | None = "2.1.259 (Claude Code)") -> None:
        monkeypatch.setattr(
            "issuebot.cli._which", lambda name: f"/usr/bin/{name}" if name in names else None
        )
        monkeypatch.setattr("issuebot.cli._claude_version", lambda command: version)

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
    assert "[ OK ] notifications.slack: not configured" in out
    assert "[ OK ] prompt: 44 characters, renders" in out
    assert "[ OK ] gh auth: logged in as fake-user" in out
    assert "[ OK ] github.repo access: example/repo (default branch main)" in out
    assert "[ OK ] github.labels: 5 labels present" in out
    assert (
        out.index("[ OK ] gh: ") < out.index("[ OK ] gh auth:") < out.index("[ OK ] database.url")
    )
    assert out.rstrip().endswith("12 checks: 0 failed, 0 warnings")
    assert "secret-token-value" not in out


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
    assert "0 failed, 1 warnings" in out


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
    assert "2 failed, 3 warnings" in out


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
    assert "[ OK ] database.url: configured" in out
    assert "[ OK ] notifications.slack: configured" in out


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
    assert "0 failed, 1 warnings" in out


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


def test_validate_defaults_to_cwd_workflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, executables: object
) -> None:
    _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
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
    assert "[ OK ] gh auth: logged in as fake-user" in out
    assert "[FAIL] github.repo access: not_found: injected not_found failure" in out
    assert "[ OK ] github.labels: 5 labels present" in out


def test_validate_reports_labels_failure(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    fake_github: FakeGitHub,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")

    async def failing_missing_labels() -> object:
        raise GitHubError("transport", "injected transport failure")

    monkeypatch.setattr(fake_github, "missing_labels", failing_missing_labels)
    assert main(["validate", "--workflow", str(GOOD)]) == 1
    out = capsys.readouterr().out
    assert "[ OK ] gh auth: logged in as fake-user" in out
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
    assert "0 failed, 1 warnings" in out


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
    ]
    assert main(["labels", "ensure", "--workflow", str(GOOD)]) == 0
    assert all(line.endswith(": unchanged") for line in capsys.readouterr().out.splitlines())


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
        )


@pytest.fixture
def stub_session(monkeypatch: pytest.MonkeyPatch) -> StubSession:
    stub = StubSession()
    monkeypatch.setattr("issuebot.cli._run_session", stub)
    return stub


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


def test_run_once_rework_sets_the_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/rework",), number=42)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert stub_session.calls[0]["rework"] is True
    assert ("set_state", (42, StateLabel.IN_PROGRESS)) in fake_github.calls


def test_run_once_in_progress_issue_is_not_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/in-progress",), number=42)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert all(name != "set_state" for name, _ in fake_github.calls)
    assert len(stub_session.calls) == 1


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
        "turn budget exhausted; issue #42 remains in_progress (the blocked escape is Phase 4)"
        in out
    )


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
    assert [name for name, _ in fake_github.calls] == ["fetch_issues_by_ids"]
    assert fake_github.issue(42).state is StateLabel.TODO
    assert not (tmp_path / "ws").exists()


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
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)

    async def failing_set_state(number: int, state: StateLabel) -> None:
        raise GitHubError("transport", "injected transport failure")

    monkeypatch.setattr(fake_github, "set_state", failing_set_state)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    assert "[FAIL] claim: transport: injected transport failure" in capsys.readouterr().out
    assert stub_session.calls == []


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
