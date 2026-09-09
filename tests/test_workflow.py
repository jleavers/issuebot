"""Tests for WORKFLOW.md parsing and loading."""

from pathlib import Path

import pytest

from issuebot.config import Settings, Workflow, load_workflow
from issuebot.config.errors import (
    ConfigError,
    FrontMatterNotAMap,
    MissingEnvironmentVariable,
    MissingWorkflowFile,
    SettingsValidationError,
    WorkflowParseError,
)
from issuebot.config.workflow import parse_workflow_text

# --- parse_workflow_text -------------------------------------------------------


def test_splits_front_matter_and_body() -> None:
    raw, body = parse_workflow_text("---\ngithub:\n  repo: o/r\n---\n\nHello {{ issue.title }}\n")
    assert raw == {"github": {"repo": "o/r"}}
    assert body == "Hello {{ issue.title }}"


def test_crlf_input_is_normalised() -> None:
    raw, body = parse_workflow_text("---\r\npolling:\r\n  interval_ms: 5000\r\n---\r\nBody\r\n")
    assert raw == {"polling": {"interval_ms": 5000}}
    assert body == "Body"


def test_bom_is_ignored() -> None:
    raw, body = parse_workflow_text("﻿---\nx: 1\n---\nB")
    assert raw == {"x": 1}
    assert body == "B"


def test_no_front_matter_is_all_body() -> None:
    assert parse_workflow_text("Just a prompt\n") == ({}, "Just a prompt")


def test_empty_front_matter_is_empty_mapping() -> None:
    assert parse_workflow_text("---\n---\nBody") == ({}, "Body")


def test_unterminated_front_matter_is_parse_error() -> None:
    with pytest.raises(WorkflowParseError, match="never closed"):
        parse_workflow_text("---\ngithub: {}\nBody")


def test_invalid_yaml_is_parse_error() -> None:
    with pytest.raises(WorkflowParseError, match="invalid YAML"):
        parse_workflow_text("---\ngithub: [unclosed\n---\nBody")


def test_non_mapping_front_matter_is_rejected() -> None:
    with pytest.raises(FrontMatterNotAMap, match="mapping"):
        parse_workflow_text("---\n- a\n- b\n---\nBody")


# --- error types -----------------------------------------------------------------


def test_error_codes() -> None:
    assert WorkflowParseError("x").code == "workflow_parse_error"
    assert FrontMatterNotAMap("x").code == "workflow_front_matter_not_a_map"
    missing_env = MissingEnvironmentVariable(variable="V", field="f")
    assert missing_env.code == "missing_environment_variable"
    assert SettingsValidationError([]).code == "invalid_settings"


def test_error_str_includes_path_when_present() -> None:
    err = WorkflowParseError("bad", path=Path("/w/WORKFLOW.md"))
    assert str(err) == "/w/WORKFLOW.md: bad"
    assert isinstance(err, ConfigError)
    assert str(WorkflowParseError("bad")) == "bad"


def test_missing_environment_variable_message() -> None:
    err = MissingEnvironmentVariable(variable="MY_TOKEN", field="github.token")
    assert err.variable == "MY_TOKEN"
    assert err.field == "github.token"
    assert str(err) == "github.token references $MY_TOKEN, which is unset or empty"


def test_settings_validation_error_lists_fields() -> None:
    err = SettingsValidationError(
        [("polling.interval_ms", "too small"), ("agnet", "Extra inputs are not permitted")],
        path=Path("/w/WORKFLOW.md"),
    )
    assert str(err) == (
        "/w/WORKFLOW.md: 2 invalid setting(s)\n"
        "  polling.interval_ms: too small\n"
        "  agnet: Extra inputs are not permitted"
    )


# --- load_workflow ---------------------------------------------------------------

GOOD = """---
github:
  repo: o/r
  token: $TOKEN
workspace:
  root: ws
---

Prompt body
"""


def test_load_workflow_builds_settings_and_prompt(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text(GOOD, encoding="utf-8")
    wf = load_workflow(wf_path, environ={"TOKEN": "t"})
    assert isinstance(wf, Workflow)
    assert isinstance(wf.config, Settings)
    assert wf.config.github.repo == "o/r"
    assert wf.config.github.token is not None
    assert wf.config.github.token.get_secret_value() == "t"
    assert wf.config.workspace.root == (tmp_path / "ws").resolve()
    assert wf.prompt_template == "Prompt body"
    assert wf.raw_config["github"]["token"] == "$TOKEN"
    assert wf.path == wf_path.resolve()
    assert wf.source_mtime_ns == wf_path.stat().st_mtime_ns


def test_load_workflow_records_the_source_identity(tmp_path: Path) -> None:
    """Which file the settings came from, so a watcher can tell it apart from another (#46)."""
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text(GOOD, encoding="utf-8")
    wf = load_workflow(wf_path, environ={"TOKEN": "t"})
    source = wf_path.stat()
    assert wf.source_identity == (source.st_dev, source.st_ino, source.st_mtime_ns)


def test_load_workflow_accepts_str_path(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text(GOOD, encoding="utf-8")
    assert load_workflow(str(wf_path), environ={"TOKEN": "t"}).config.github.repo == "o/r"


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(MissingWorkflowFile) as exc:
        load_workflow(tmp_path / "nope.md", environ={})
    assert exc.value.path == (tmp_path / "nope.md").resolve()
    assert "not found" in str(exc.value)


def test_undecodable_file_is_unreadable(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_bytes(b"\xff\xfe---\ngithub: {}\n---\nBody")
    with pytest.raises(MissingWorkflowFile) as exc:
        load_workflow(wf_path, environ={})
    assert exc.value.path == wf_path.resolve()
    assert "unreadable" in str(exc.value)


def test_parse_errors_carry_path(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text("---\ngithub: {}\nBody", encoding="utf-8")
    with pytest.raises(WorkflowParseError) as exc:
        load_workflow(wf_path, environ={})
    assert exc.value.path == wf_path.resolve()


def test_missing_env_reference_carries_path(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text(GOOD, encoding="utf-8")
    with pytest.raises(MissingEnvironmentVariable) as exc:
        load_workflow(wf_path, environ={})
    assert exc.value.path == wf_path.resolve()
    assert exc.value.variable == "TOKEN"


def test_invalid_settings_lists_fields(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text(
        "---\ngithub:\n  repo: o/r\npolling:\n  interval_ms: 5\nagnet: {}\n---\nBody",
        encoding="utf-8",
    )
    with pytest.raises(SettingsValidationError) as exc:
        load_workflow(wf_path, environ={})
    fields = {field for field, _ in exc.value.errors}
    assert "polling.interval_ms" in fields
    assert "agnet" in fields
    assert exc.value.path == wf_path.resolve()


def test_body_only_file_fails_on_missing_repo(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text("Just a prompt", encoding="utf-8")
    with pytest.raises(SettingsValidationError) as exc:
        load_workflow(wf_path, environ={})
    assert {field for field, _ in exc.value.errors} == {"github"}


def test_default_environ_is_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text("---\ngithub:\n  repo: o/r\n---\nBody", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "from-process")
    wf = load_workflow(wf_path)
    assert wf.config.github.token is not None
    assert wf.config.github.token.get_secret_value() == "from-process"


def test_workflow_is_frozen(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text("---\ngithub:\n  repo: o/r\n---\nBody", encoding="utf-8")
    wf = load_workflow(wf_path, environ={})
    with pytest.raises(AttributeError):
        wf.prompt_template = "changed"  # type: ignore[misc]


def test_empty_workspace_root_is_rejected(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text(
        '---\ngithub:\n  repo: o/r\nworkspace:\n  root: ""\n---\nBody', encoding="utf-8"
    )
    with pytest.raises(SettingsValidationError) as exc:
        load_workflow(wf_path, environ={})
    fields = {field for field, _ in exc.value.errors}
    assert "workspace.root" in fields
