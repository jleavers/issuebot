"""The local overlay: ``WORKFLOW.local.md`` merged over ``WORKFLOW.md``."""

from pathlib import Path

import pytest

from issuebot.config import (
    ConfigError,
    MissingWorkflowFile,
    SettingsValidationError,
    WorkflowParseError,
    count_overrides,
    load_workflow,
    merge_front_matter,
    overlay_path_for,
)

BASE = """---
github:
  repo: o/r
  labels:
    todo: issuebot/todo
hooks:
  after_create: git fetch --unshallow
agent:
  max_concurrent_agents: 2
claude:
  model: opus
  model_labels:
    issuebot/model/sonnet: sonnet
    issuebot/model/fable: claude-fable-5-1
notifications:
  slack:
    events: [state_changed, blocked]
---

Base prompt {{ issue.number }}
"""


def write(tmp_path: Path, base: str = BASE, overlay: str | None = None) -> Path:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(base, encoding="utf-8")
    if overlay is not None:
        overlay_path_for(path).write_text(overlay, encoding="utf-8")
    return path


# --- merge rules -----------------------------------------------------------------------


def test_nested_mappings_merge_key_by_key() -> None:
    merged = merge_front_matter(
        {"github": {"repo": "o/r", "labels": {"todo": "t"}}},
        {"github": {"labels": {"review": "r"}}},
    )
    assert merged == {"github": {"repo": "o/r", "labels": {"todo": "t", "review": "r"}}}


def test_scalars_replace() -> None:
    merged = merge_front_matter({"agent": {"max_turns": 5}}, {"agent": {"max_turns": 9}})
    assert merged == {"agent": {"max_turns": 9}}


def test_a_list_replaces_as_a_whole() -> None:
    """An event allow-list is a choice, not an accumulation: fewer kinds must be possible."""
    merged = merge_front_matter({"events": ["state_changed", "blocked"]}, {"events": ["blocked"]})
    assert merged == {"events": ["blocked"]}


def test_a_mapping_over_a_scalar_and_a_scalar_over_a_mapping_both_replace() -> None:
    assert merge_front_matter({"x": 1}, {"x": {"y": 2}}) == {"x": {"y": 2}}
    assert merge_front_matter({"x": {"y": 2}}, {"x": 1}) == {"x": 1}


def test_null_deletes_the_key_and_a_null_for_an_unset_key_is_a_no_op() -> None:
    merged = merge_front_matter(
        {"claude": {"model": "opus", "max_budget_usd": 3.0}},
        {"claude": {"model": None, "nothing": None}},
    )
    assert merged == {"claude": {"max_budget_usd": 3.0}}


def test_a_null_inside_a_section_the_base_lacks_is_still_a_no_op() -> None:
    """Rule 3 holds at depth: a new subtree sheds its nulls rather than carrying None."""
    assert merge_front_matter({}, {"server": {"port": None}}) == {"server": {}}
    assert merge_front_matter({"x": 1}, {"x": {"y": None, "z": 2}}) == {"x": {"z": 2}}


def test_null_in_a_new_section_takes_the_settings_default(tmp_path: Path) -> None:
    base = "---\ngithub:\n  repo: o/r\n---\nBody"
    wf = load_workflow(
        write(tmp_path, base=base, overlay="---\nagent:\n  max_turns: null\n---\n"), environ={}
    )
    assert wf.config.agent.max_turns == 5


def test_merge_leaves_both_inputs_alone() -> None:
    base = {"claude": {"model_labels": {"a": "b"}}}
    overlay = {"claude": {"model_labels": {"c": "d"}}}
    merged = merge_front_matter(base, overlay)
    merged["claude"]["model_labels"]["e"] = "f"
    assert base == {"claude": {"model_labels": {"a": "b"}}}
    assert overlay == {"claude": {"model_labels": {"c": "d"}}}


def test_count_overrides_counts_leaves_including_deletes() -> None:
    overlay = {
        "github": {"repo": "acme/frontend"},
        "claude": {"model": None, "model_labels": {"issuebot/model/haiku": "haiku"}},
        "notifications": {"slack": {"events": ["blocked"]}},
    }
    assert count_overrides(overlay) == 4
    assert count_overrides({}) == 0
    assert count_overrides({"claude": {}}) == 0


# --- discovery -------------------------------------------------------------------------


def test_overlay_path_is_the_local_sibling() -> None:
    assert overlay_path_for(Path("/configs/WORKFLOW.md")) == Path("/configs/WORKFLOW.local.md")
    assert overlay_path_for(Path("/x/frontend.md")) == Path("/x/frontend.local.md")


def test_overlay_is_found_beside_the_base_and_recorded(tmp_path: Path) -> None:
    path = write(tmp_path, overlay="---\ngithub:\n  repo: acme/frontend\n---\n")
    wf = load_workflow(path, environ={})
    assert wf.config.github.repo == "acme/frontend"
    assert wf.overlay_path == overlay_path_for(path.resolve())
    local = wf.overlay_path.stat()
    assert wf.overlay_identity == (local.st_dev, local.st_ino, local.st_mtime_ns)
    assert wf.overlay_config == {"github": {"repo": "acme/frontend"}}
    # raw_config is the mapping the settings were validated from: the merge.
    assert wf.raw_config["github"] == {"repo": "acme/frontend", "labels": {"todo": "issuebot/todo"}}


def test_a_missing_overlay_is_the_normal_case(tmp_path: Path) -> None:
    wf = load_workflow(write(tmp_path), environ={})
    assert wf.config.github.repo == "o/r"
    assert wf.overlay_path is None
    assert wf.overlay_identity == (0, 0, 0)
    assert wf.overlay_config == {}


def test_overlay_false_ignores_a_present_overlay(tmp_path: Path) -> None:
    path = write(tmp_path, overlay="---\ngithub:\n  repo: acme/frontend\n---\n")
    wf = load_workflow(path, environ={}, overlay=False)
    assert wf.config.github.repo == "o/r"
    assert wf.overlay_path is None


def test_an_overlay_that_is_not_a_regular_file_is_an_error_naming_it(tmp_path: Path) -> None:
    path = write(tmp_path)
    overlay_path_for(path).mkdir()
    with pytest.raises(MissingWorkflowFile) as exc:
        load_workflow(path, environ={})
    assert str(exc.value) == (
        f"{path.resolve()}: workflow overlay is not a regular file: "
        f"{overlay_path_for(path.resolve())}"
    )


def test_no_chaining(tmp_path: Path) -> None:
    """An overlay's own overlay is ``WORKFLOW.local.local.md``, which nothing reads."""
    path = write(tmp_path, overlay="---\ngithub:\n  repo: acme/frontend\n---\n")
    (tmp_path / "WORKFLOW.local.local.md").write_text(
        "---\ngithub:\n  repo: acme/ignored\n---\n", encoding="utf-8"
    )
    assert load_workflow(path, environ={}).config.github.repo == "acme/frontend"


# --- merged settings ---------------------------------------------------------------------


def test_the_four_line_deployment_overlay(tmp_path: Path) -> None:
    """The README's example: repo and budget in the overlay, everything else inherited."""
    overlay = "---\ngithub:\n  repo: acme/frontend\nclaude:\n  max_budget_usd: 3.0\n---\n"
    wf = load_workflow(write(tmp_path, overlay=overlay), environ={})
    assert wf.config.github.repo == "acme/frontend"
    assert wf.config.claude.max_budget_usd == 3.0
    assert wf.config.claude.model == "opus"
    assert wf.config.agent.max_concurrent_agents == 2
    assert wf.config.github.labels.todo == "issuebot/todo"


def test_model_labels_merge_and_a_null_clears_one(tmp_path: Path) -> None:
    overlay = (
        "---\nclaude:\n  model_labels:\n    issuebot/model/haiku: haiku\n"
        "    issuebot/model/fable: null\n---\n"
    )
    wf = load_workflow(write(tmp_path, overlay=overlay), environ={})
    assert wf.config.claude.model_labels == {
        "issuebot/model/sonnet": "sonnet",
        "issuebot/model/haiku": "haiku",
    }


def test_null_falls_back_to_the_settings_default(tmp_path: Path) -> None:
    overlay = "---\nhooks:\n  after_create: null\nclaude:\n  model: null\n---\n"
    wf = load_workflow(write(tmp_path, overlay=overlay), environ={})
    assert wf.config.hooks.after_create is None
    assert wf.config.claude.model is None


def test_a_list_in_the_overlay_replaces_the_base_list(tmp_path: Path) -> None:
    overlay = "---\nnotifications:\n  slack:\n    events: [blocked]\n---\n"
    wf = load_workflow(write(tmp_path, overlay=overlay), environ={})
    assert wf.config.notifications.slack.events == ["blocked"]


def test_an_unknown_key_in_the_overlay_fails_validation_naming_both_files(
    tmp_path: Path,
) -> None:
    path = write(tmp_path, overlay="---\nclaude:\n  max_budget: 3.0\n---\n")
    with pytest.raises(SettingsValidationError) as exc:
        load_workflow(path, environ={})
    assert exc.value.path == path.resolve()
    assert exc.value.overlay == overlay_path_for(path.resolve())
    assert str(exc.value) == (
        f"{path.resolve()} (+ WORKFLOW.local.md): 1 invalid setting(s)\n"
        "  claude.max_budget: Extra inputs are not permitted"
    )


def test_an_error_without_an_overlay_reads_as_before(tmp_path: Path) -> None:
    path = write(tmp_path, base="---\ngithub:\n  repo: o/r\nagnet: {}\n---\nBody")
    with pytest.raises(SettingsValidationError) as exc:
        load_workflow(path, environ={})
    assert str(exc.value).startswith(f"{path.resolve()}: 1 invalid setting(s)")


def test_a_parse_error_in_the_overlay_names_the_overlay(tmp_path: Path) -> None:
    path = write(tmp_path, overlay="---\ngithub: [unclosed\n---\n")
    with pytest.raises(WorkflowParseError) as exc:
        load_workflow(path, environ={})
    assert exc.value.path == overlay_path_for(path.resolve())
    assert exc.value.overlay is None


# --- resolution ------------------------------------------------------------------------


def test_env_reference_in_the_overlay_resolves(tmp_path: Path) -> None:
    overlay = "---\ngithub:\n  token: $FRONTEND_TOKEN\n---\n"
    wf = load_workflow(write(tmp_path, overlay=overlay), environ={"FRONTEND_TOKEN": "t"})
    assert wf.config.github.token is not None
    assert wf.config.github.token.get_secret_value() == "t"


def test_a_missing_env_reference_in_the_overlay_names_both_files(tmp_path: Path) -> None:
    path = write(tmp_path, overlay="---\ngithub:\n  token: $FRONTEND_TOKEN\n---\n")
    with pytest.raises(ConfigError) as exc:
        load_workflow(path, environ={})
    assert str(exc.value) == (
        f"{path.resolve()} (+ WORKFLOW.local.md): github.token references $FRONTEND_TOKEN, "
        "which is unset or empty"
    )


def test_relative_workspace_root_in_the_overlay_resolves_against_the_shared_directory(
    tmp_path: Path,
) -> None:
    wf = load_workflow(write(tmp_path, overlay="---\nworkspace:\n  root: ws\n---\n"), environ={})
    assert wf.config.workspace.root == (tmp_path / "ws").resolve()


def test_merging_raw_mappings_keeps_the_base_workspace_root(tmp_path: Path) -> None:
    """Resolving each file separately would hand the overlay a manufactured /workspaces."""
    base = "---\ngithub:\n  repo: o/r\nworkspace:\n  root: here\n---\nBody"
    wf = load_workflow(
        write(tmp_path, base=base, overlay="---\nclaude:\n  model: sonnet\n---\n"), environ={}
    )
    assert wf.config.workspace.root == (tmp_path / "here").resolve()


# --- the prompt body ---------------------------------------------------------------------


@pytest.mark.parametrize("trailer", ["", "\n\n"])
def test_prompt_is_inherited_when_the_overlay_has_no_body(tmp_path: Path, trailer: str) -> None:
    """Nothing but front matter, or front matter and blank lines, inherits the base prompt."""
    overlay = "---\nclaude:\n  model: sonnet\n---\n" + trailer
    wf = load_workflow(write(tmp_path, overlay=overlay), environ={})
    assert wf.prompt_template == "Base prompt {{ issue.number }}"


def test_prompt_is_replaced_when_the_overlay_has_one(tmp_path: Path) -> None:
    overlay = "---\nclaude:\n  model: sonnet\n---\n\nTuned prompt {{ issue.title }}\n"
    wf = load_workflow(write(tmp_path, overlay=overlay), environ={})
    assert wf.prompt_template == "Tuned prompt {{ issue.title }}"


def test_an_overlay_that_is_only_a_body_replaces_the_prompt_and_no_setting(
    tmp_path: Path,
) -> None:
    wf = load_workflow(write(tmp_path, overlay="Only a prompt\n"), environ={})
    assert wf.prompt_template == "Only a prompt"
    assert wf.config.github.repo == "o/r"
    assert wf.overlay_config == {}
