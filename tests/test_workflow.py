"""Tests for WORKFLOW.md parsing and loading."""

from pathlib import Path

import pytest

from issuebot.config.errors import (
    ConfigError,
    FrontMatterNotAMap,
    MissingEnvironmentVariable,
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
