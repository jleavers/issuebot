"""Tests for $VAR, ~ and relative-path resolution of designated config fields."""

from pathlib import Path

import pytest

from issuebot.config.errors import MissingEnvironmentVariable
from issuebot.config.resolve import resolve_config, resolve_env_value

BASE = Path("/srv/workflows")


def test_explicit_reference_resolves_from_environ() -> None:
    out = resolve_config(
        {"github": {"repo": "o/r", "token": "$MY_TOKEN"}},
        environ={"MY_TOKEN": "abc"},
        base_dir=BASE,
    )
    assert out["github"]["token"] == "abc"


def test_explicit_reference_to_unset_variable_fails() -> None:
    with pytest.raises(MissingEnvironmentVariable) as exc:
        resolve_config({"github": {"token": "$MY_TOKEN"}}, environ={}, base_dir=BASE)
    assert exc.value.variable == "MY_TOKEN"
    assert exc.value.field == "github.token"


def test_explicit_reference_to_empty_variable_fails() -> None:
    with pytest.raises(MissingEnvironmentVariable):
        resolve_config({"github": {"token": "$MY_TOKEN"}}, environ={"MY_TOKEN": ""}, base_dir=BASE)


def test_omitted_secret_uses_fallback_variable() -> None:
    out = resolve_config({"github": {"repo": "o/r"}}, environ={"GH_TOKEN": "fb"}, base_dir=BASE)
    assert out["github"]["token"] == "fb"


def test_omitted_secret_without_fallback_is_absent() -> None:
    out = resolve_config({"github": {"repo": "o/r"}}, environ={}, base_dir=BASE)
    assert "token" not in out["github"]
    assert "database" not in out
    assert "notifications" not in out


def test_all_three_secret_fallbacks() -> None:
    out = resolve_config(
        {"github": {"repo": "o/r"}},
        environ={"GH_TOKEN": "t", "DATABASE_URL": "d", "SLACK_WEBHOOK_URL": "s"},
        base_dir=BASE,
    )
    assert out["github"]["token"] == "t"
    assert out["database"]["url"] == "d"
    assert out["notifications"]["slack"]["webhook_url"] == "s"


def test_literal_secret_passes_through() -> None:
    out = resolve_config({"github": {"token": "ghp_literal"}}, environ={}, base_dir=BASE)
    assert out["github"]["token"] == "ghp_literal"


def test_embedded_reference_is_not_interpolated() -> None:
    out = resolve_config({"github": {"token": "prefix-$X"}}, environ={"X": "1"}, base_dir=BASE)
    assert out["github"]["token"] == "prefix-$X"


def test_workspace_root_default_when_unset() -> None:
    out = resolve_config({}, environ={}, base_dir=BASE)
    assert out["workspace"]["root"] == "/workspaces"


def test_workspace_root_fallback_variable() -> None:
    out = resolve_config({}, environ={"ISSUEBOT_WORKSPACE_ROOT": "/data/ws"}, base_dir=BASE)
    assert out["workspace"]["root"] == "/data/ws"


def test_workspace_root_explicit_reference() -> None:
    out = resolve_config({"workspace": {"root": "$WS"}}, environ={"WS": "/mnt/ws"}, base_dir=BASE)
    assert out["workspace"]["root"] == "/mnt/ws"


def test_relative_workspace_root_resolves_against_workflow_dir() -> None:
    out = resolve_config({"workspace": {"root": "ws"}}, environ={}, base_dir=BASE)
    assert out["workspace"]["root"] == "/srv/workflows/ws"


def test_tilde_expands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/tester")
    out = resolve_config({"workspace": {"root": "~/ws"}}, environ={}, base_dir=BASE)
    assert out["workspace"]["root"] == "/home/tester/ws"


def test_hook_scripts_are_untouched() -> None:
    raw = {"hooks": {"before_run": "echo $HOME && ls ~"}}
    out = resolve_config(raw, environ={"HOME": "/x"}, base_dir=BASE)
    assert out["hooks"]["before_run"] == "echo $HOME && ls ~"


def test_input_mapping_is_not_mutated() -> None:
    raw = {"github": {"repo": "o/r"}}
    resolve_config(raw, environ={"GH_TOKEN": "t"}, base_dir=BASE)
    assert raw == {"github": {"repo": "o/r"}}


def test_non_mapping_section_is_left_for_validation() -> None:
    out = resolve_config({"github": "oops"}, environ={"GH_TOKEN": "t"}, base_dir=BASE)
    assert out["github"] == "oops"


def test_resolve_env_value_non_string_passes_through() -> None:
    assert resolve_env_value(42, field="f", fallback=None, environ={}) == 42


def test_empty_workspace_root_is_left_for_validation() -> None:
    out = resolve_config({"workspace": {"root": ""}}, environ={}, base_dir=BASE)
    assert out["workspace"]["root"] == ""


def test_relative_mcp_config_path_resolves_against_workflow_dir() -> None:
    """The clone is the session's cwd and the session's to write, so a relative path must not
    be read from there (#109): it names a file beside the workflow, the operator's."""
    out = resolve_config(
        {"claude": {"mcp_config": ["mcp.json", "servers/a.json", "/etc/issuebot/mcp.json"]}},
        environ={},
        base_dir=BASE,
    )
    assert out["claude"]["mcp_config"] == [
        "/srv/workflows/mcp.json",
        "/srv/workflows/servers/a.json",
        "/etc/issuebot/mcp.json",
    ]


def test_mcp_config_tilde_expands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/tester")
    out = resolve_config({"claude": {"mcp_config": ["~/mcp.json"]}}, environ={}, base_dir=BASE)
    assert out["claude"]["mcp_config"] == ["/home/tester/mcp.json"]


def test_mcp_config_json_documents_pass_through() -> None:
    documents = ['{"mcpServers": {}}', '  [{"a": 1}]', "{}"]
    out = resolve_config({"claude": {"mcp_config": documents}}, environ={}, base_dir=BASE)
    assert out["claude"]["mcp_config"] == documents


def test_mcp_config_unusable_entries_are_left_for_validation() -> None:
    out = resolve_config({"claude": {"mcp_config": ["", 3, None, "  "]}}, environ={}, base_dir=BASE)
    assert out["claude"]["mcp_config"] == ["", 3, None, "  "]
    out = resolve_config({"claude": {"mcp_config": "mcp.json"}}, environ={}, base_dir=BASE)
    assert out["claude"]["mcp_config"] == "mcp.json"
    out = resolve_config({"claude": {}}, environ={}, base_dir=BASE)
    assert "mcp_config" not in out["claude"]
