"""Tests for the typed settings models."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from issuebot.config.settings import GitHubLabels, Settings

MINIMAL = {"github": {"repo": "owner/repo"}}


def _locs(exc: ValidationError) -> set[str]:
    return {".".join(str(p) for p in e["loc"]) for e in exc.errors()}


def test_minimal_config_applies_every_default() -> None:
    s = Settings.model_validate(MINIMAL)
    assert s.github.repo == "owner/repo"
    assert s.github.token is None
    assert s.github.labels.as_tuple() == (
        "issuebot/todo",
        "issuebot/in-progress",
        "issuebot/review",
        "issuebot/rework",
        "issuebot/complete",
    )
    assert s.github.labels.markers() == ("issuebot/no-fault",)
    assert s.github.request_timeout_ms == 30_000
    assert s.polling.interval_ms == 30_000
    assert s.workspace.root == Path("/workspaces")
    assert s.hooks.after_create is None
    assert s.hooks.before_run is None
    assert s.hooks.after_run is None
    assert s.hooks.before_remove is None
    assert s.hooks.timeout_ms == 60_000
    assert s.agent.max_concurrent_agents == 3
    assert s.agent.max_turns == 5
    assert s.agent.max_attempts == 3
    assert s.agent.max_retry_backoff_ms == 300_000
    assert s.agent.max_conflict_reworks == 3
    assert s.agent.max_issue_cost_usd == 0.0
    assert s.claude.command == "claude"
    assert s.claude.model is None
    assert s.claude.permission_mode == "auto"
    assert s.claude.max_budget_usd == 5.0
    assert s.claude.turn_timeout_ms == 3_600_000
    assert s.claude.stall_timeout_ms == 300_000
    assert s.claude.allowed_tools == []
    assert s.claude.disallowed_tools == []
    assert s.claude.append_system_prompt is None
    assert s.claude.model_labels == {}
    assert s.database.url is None
    assert s.notifications.slack.webhook_url is None
    assert s.notifications.slack.events == ["state_changed", "blocked"]


def test_github_repo_is_required() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({})
    assert "github" in _locs(exc.value)


@pytest.mark.parametrize("repo", ["owner", "owner/", "/repo", "owner/repo/extra", "a b/c"])
def test_github_repo_must_be_owner_slash_name(repo: str) -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({"github": {"repo": repo}})
    assert "github.repo" in _locs(exc.value)


def test_secrets_are_secretstr() -> None:
    s = Settings.model_validate(
        {
            "github": {"repo": "o/r", "token": "tok"},
            "database": {"url": "postgresql://u:p@h/db"},
            "notifications": {"slack": {"webhook_url": "https://hooks/x"}},
        }
    )
    assert isinstance(s.github.token, SecretStr)
    assert s.github.token.get_secret_value() == "tok"
    assert "tok" not in repr(s.github.token)
    assert isinstance(s.database.url, SecretStr)
    assert isinstance(s.notifications.slack.webhook_url, SecretStr)


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({**MINIMAL, "agnet": {}})
    assert "agnet" in _locs(exc.value)


def test_unknown_nested_key_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({"github": {"repo": "o/r", "tokne": "x"}})
    assert "github.tokne" in _locs(exc.value)


def test_a_server_block_is_no_longer_accepted() -> None:
    with pytest.raises(ValidationError, match="server"):
        Settings.model_validate({"github": {"repo": "a/b"}, "server": {"port": 1}})


def test_state_labels_must_be_distinct() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        GitHubLabels(todo="same", review="same")


def test_label_must_not_be_empty() -> None:
    with pytest.raises(ValidationError) as exc:
        GitHubLabels(todo="")
    assert "todo" in _locs(exc.value)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("polling", "interval_ms", 999),
        ("hooks", "timeout_ms", 0),
        ("agent", "max_concurrent_agents", 0),
        ("agent", "max_turns", 0),
        ("agent", "max_attempts", 0),
        ("agent", "max_retry_backoff_ms", 999),
        ("agent", "max_conflict_reworks", -1),
        ("agent", "max_issue_cost_usd", -0.01),
        ("claude", "command", ""),
        ("claude", "max_budget_usd", 0),
        ("claude", "turn_timeout_ms", 0),
        ("claude", "permission_mode", "plan"),
        ("claude", "permission_mode", "manual"),
    ],
)
def test_constraints_reject_out_of_range_values(section: str, field: str, value: object) -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({**MINIMAL, section: {field: value}})
    assert f"{section}.{field}" in _locs(exc.value)


def test_zero_conflict_reworks_is_the_off_switch() -> None:
    s = Settings.model_validate({**MINIMAL, "agent": {"max_conflict_reworks": 0}})
    assert s.agent.max_conflict_reworks == 0


def test_the_per_issue_spend_ceiling_is_a_float_and_zero_is_off() -> None:
    s = Settings.model_validate({**MINIMAL, "agent": {"max_issue_cost_usd": 25}})
    assert s.agent.max_issue_cost_usd == 25.0
    assert Settings.model_validate({**MINIMAL}).agent.max_issue_cost_usd == 0.0


@pytest.mark.parametrize("mode", ["auto", "acceptEdits", "dontAsk", "bypassPermissions"])
def test_permission_mode_accepts_unattended_values(mode: str) -> None:
    s = Settings.model_validate({**MINIMAL, "claude": {"permission_mode": mode}})
    assert s.claude.permission_mode == mode


def test_stall_timeout_accepts_zero_and_negative_to_disable() -> None:
    s = Settings.model_validate({**MINIMAL, "claude": {"stall_timeout_ms": 0}})
    assert s.claude.stall_timeout_ms == 0
    s = Settings.model_validate({**MINIMAL, "claude": {"stall_timeout_ms": -1}})
    assert s.claude.stall_timeout_ms == -1


def test_workspace_root_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        Settings.model_validate({**MINIMAL, "workspace": {"root": "relative/path"}})


def test_hook_scripts_are_kept_verbatim() -> None:
    script = "git fetch origin && echo $HOME\n"
    s = Settings.model_validate({**MINIMAL, "hooks": {"before_run": script}})
    assert s.hooks.before_run == script


def test_slack_events_must_be_known_kinds() -> None:
    with pytest.raises(ValidationError, match="unknown event kinds: nope") as exc:
        Settings.model_validate(
            {**MINIMAL, "notifications": {"slack": {"events": ["state_changed", "nope"]}}}
        )
    assert "notifications.slack.events" in _locs(exc.value)


def test_request_timeout_lower_bound() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({"github": {"repo": "o/r", "request_timeout_ms": 999}})
    assert "github.request_timeout_ms" in _locs(exc.value)


def test_settings_are_frozen() -> None:
    s = Settings.model_validate(MINIMAL)
    with pytest.raises(ValidationError):
        s.polling.interval_ms = 1


def test_phase_three_defaults() -> None:
    s = Settings.model_validate(MINIMAL)
    assert s.agent.self_review is True
    assert s.claude.setting_sources is None


def test_self_review_can_be_disabled() -> None:
    s = Settings.model_validate({**MINIMAL, "agent": {"self_review": False}})
    assert s.agent.self_review is False


def test_setting_sources_accepts_known_sources() -> None:
    s = Settings.model_validate({**MINIMAL, "claude": {"setting_sources": ["project", "local"]}})
    assert s.claude.setting_sources == ["project", "local"]


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ([], "at least one source"),
        (["project", "project"], "repeat"),
        (["global"], "user"),
    ],
)
def test_setting_sources_rejects_bad_values(value: list[str], needle: str) -> None:
    with pytest.raises(ValidationError, match=needle) as exc:
        Settings.model_validate({**MINIMAL, "claude": {"setting_sources": value}})
    assert any(loc.startswith("claude.setting_sources") for loc in _locs(exc.value))


def test_state_labels_distinctness_is_case_insensitive() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        GitHubLabels(todo="Issuebot/Todo", review="issuebot/todo")


def test_the_marker_label_must_not_collide_with_a_state_label() -> None:
    """A marker that is also a state name would be stripped by every `clear_state`."""
    with pytest.raises(ValidationError, match="distinct"):
        GitHubLabels(no_fault="Issuebot/Review")


@pytest.mark.parametrize(("value", "needle"), [("a,b", "','"), ("-marker", "'-'")])
def test_marker_label_names_that_break_gh_are_rejected(value: str, needle: str) -> None:
    with pytest.raises(ValidationError, match=needle):
        GitHubLabels(no_fault=value)


@pytest.mark.parametrize(("value", "needle"), [("a,b", "','"), ("-todo", "'-'")])
def test_state_label_names_that_break_gh_are_rejected(value: str, needle: str) -> None:
    with pytest.raises(ValidationError, match=needle) as exc:
        GitHubLabels(review=value)
    assert "review" in _locs(exc.value)


def test_model_labels_map_a_label_name_to_a_model() -> None:
    s = Settings.model_validate(
        {**MINIMAL, "claude": {"model_labels": {"issuebot/model/sonnet": "sonnet"}}}
    )
    assert s.claude.model_labels == {"issuebot/model/sonnet": "sonnet"}


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ({"  ": "sonnet"}, "label name"),
        ({"issuebot/model/sonnet": " "}, "model name"),
        ({"issuebot/model/sonnet": "sonnet", "Issuebot/Model/Sonnet": "opus"}, "distinct"),
    ],
)
def test_model_labels_rejects_unusable_entries(value: dict[str, str], needle: str) -> None:
    with pytest.raises(ValidationError, match=needle) as exc:
        Settings.model_validate({**MINIMAL, "claude": {"model_labels": value}})
    assert any(loc.startswith("claude.model_labels") for loc in _locs(exc.value))
