# Local overrides for `configs/WORKFLOW.md` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A git-ignored `configs/WORKFLOW.local.md`, found beside the workflow file, whose front matter is deep-merged over the tracked file's, so a deployment's settings live in an untracked file and `git pull` keeps delivering the prompt template with no merge.

**Architecture:** `load_workflow` derives the overlay path from the base path (`WORKFLOW.md` → `WORKFLOW.local.md`, never configured, so it is a sibling inside the mounted directory by construction), merges the two *raw* front-matter mappings under three rules (mappings merge, everything else replaces, an explicit `null` deletes), then resolves and validates the merged mapping once. `Workflow` grows the overlay's identity triple beside the base's; the orchestrator's `_reload_workflow` stats both files and reloads when either identity moves or the overlay's presence changes, and the #46 mount complaint runs over both. The overlay in force reaches `validate` (name and override count) and the runtime snapshot (`workflow_overlay_path`, which `issuebot status`, `/api/v1/state` and the dashboard's worker line show). No migration, no compose change, no example file.

**Tech Stack:** Python 3.14, `uv`, pydantic 2 (`Settings`, `extra="forbid"`), PyYAML, asyncio, FastAPI + Jinja2 for the one template line, pytest + pytest-asyncio (`asyncio_mode = "auto"`), ruff 0.16.6.

**Spec:** `docs/superpowers/specs/2026-09-09-workflow-local-overrides-design.md`.

**Pre-verified:** every task below was built and run on a scratch branch (`scratch/overlay-preverify`, five commits on top of the spec commit `01c56c6`) before this plan was written, and the plan's edit blocks and file contents are copied from those commits. The RED lines and test counts recorded here are the ones that run produced (baseline on the spec commit: 1045 passed, 52 skipped without a database). Treat a different count or RED line as a finding, not as noise. The spec's prose beats this plan's code when they disagree; report the disagreement rather than resolving it silently. The scratch branch can be deleted once the work is merged.

## Global Constraints

Every task's requirements include this section. Every implementer and reviewer dispatch must carry it verbatim.

- Python >=3.14, `uv run` for everything. **No new dependencies**; `pyproject.toml` and `uv.lock` do not change.
- Work on branch `issuebot/workflow-local-overrides`; the spec and this plan are its first two commits. Never push to `main`, never merge or close PRs, never `rm -rf`, `git reset --hard` or `git clean -fd`. Linux host: Bash, `&&` chaining.
- A Bash-level hook on this host blocks any shell command whose text contains the dot-env filename (the literal `.` + `env`, including `.example`, heredoc bodies and quoted anchors). Two edits in Task 5 (the `.gitignore` insertion sits above such lines; one README paragraph mentions the file) must be made with the **Edit tool**, never with `sed`, `python - <<EOF` or a heredoc; say "dot-env" in commit messages and reports.
- ruff rules E F I UP B N SIM RUF, target py314, line length 100. The ruff-format pre-commit hook reflows Python fences inside `docs/**/*.md`; the ```python fences in this plan are whole files copied from ruff-formatted sources and the Python fragments in edit blocks sit in untagged fences, so neither is rewritten. `except (OSError, UnicodeDecodeError) as exc:` keeps its parentheses (an `as` clause); the formatter drops them only without one. No quoted annotations (UP037). RUF022 keeps `__all__` sorted: every new name goes in its sorted place. Syntax-check Python with `uv run python`, never the system `python3`.
- Commit messages: conventional prefix plus the attribution trailer the harness requires as the last lines (blank line before them). Before every commit: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`; before pushing also `uv run pre-commit run --all-files`.
- Tests hermetic by default: `tmp_path`, `FakeGitHub`, the orchestrator `Harness`, `tests/fakes/web.py`, `tests/fakes/database.py`; no network, no Docker. The `db_url` tests skip without `DATABASE_URL`; nothing here touches the schema (the snapshot column is `jsonb` and `to_dict` walks fields generically), so run the suite without a database at every task and once against `test-db` at the end: `docker compose --profile test up -d --wait test-db`, then `DATABASE_URL=postgresql://issuebot:issuebot@$(docker compose port test-db 5432)/issuebot uv run pytest -q`, then `docker compose rm -sf test-db` (never `compose down`, which is project-wide). Expect 1134 passed and no skips at the end.
- Frozen inputs, used as they are: `issuebot.config.settings` (no field changes), `issuebot.config.resolve` (its `base_dir` rule needs no exception, §3), `issuebot.db` (no migration), `compose.yaml`, the `Dockerfile` (it never copies `configs/`, so nothing leaks into the image), `MOUNT_ADVICE` and `_pinned_mount_complaint` (they already name the file they are about, which is all §4 asks), `EVENT_KINDS`.
- Package rules: `issuebot.config` imports nothing from the rest of the package; `orchestrator` imports `overlay_path_for` from `issuebot.config` and never `db` or `web`; `cli` imports `count_overrides`; `web` reads the snapshot's JSON defensively as it does every other key.
- `Workflow` is a frozen dataclass with positional fields up to `source_mtime_ns`; every new field has a default so the hand-built `Workflow` in `tests/test_agent_session.py` and every other constructor keeps compiling. `RuntimeSnapshot` is `kw_only`, so `workflow_overlay_path: str | None = None` can sit beside `workflow_mtime_ns`.
- Secrets: the overlay is where a deployment's `$VAR` references live, and they resolve exactly as in the base; no test writes a literal token, and no output prints one.

## Decisions the spec left open

Made once here so no task re-derives them. Each is a small addition, not a change of design; the spec section it serves is named.

1. **`raw_config` is the merged mapping**, and `Workflow` gains a fifth field, `overlay_config`, holding the overlay's own raw front matter (`{}` without one). §6's override count needs the overlay's mapping, and `cli._token_check` reads `raw_config["github"]["token"]` to tell `$VAR` from a literal, which must see the overlay's value when the overlay sets it. `count_overrides(mapping)` is the pure counter (§6: leaf keypaths, a `null` counting as one; a mapping recurses, so an empty one counts nothing).
2. **The "(+ WORKFLOW.local.md)" header lives on `ConfigError`**, as `overlay: Path | None`, not only on `SettingsValidationError`. A `MissingEnvironmentVariable` from a `$VAR` the overlay set is about the merge too, and this way one `__str__` serves both. A parse error in the overlay's own YAML carries `path = the overlay` and no suffix: that error is about one file (§3).
3. **An overlay that is not a regular file** raises `MissingWorkflowFile` with `path` = the base and the message `workflow overlay is not a regular file: <overlay>`, so `[FAIL] workflow: <base>: workflow overlay is not a regular file: <overlay>` names it (§1). `MissingWorkflowFile` is already the class for "workflow file unreadable".
4. **`.gitignore` gets `configs/*.local.md`**, a superset of the spec's `configs/WORKFLOW.local.md`: the README's host-run multi-repository arrangement uses `--workflow frontend.md`, whose overlay is `frontend.local.md`, and the spec's reasoning (machine-local by nature) applies to it equally (§5).
5. **`issuebot status` prints the overlay's full path** (`workflow: /configs/WORKFLOW.md + /configs/WORKFLOW.local.md (config valid)`), because it reads JSON a worker on another machine wrote and has no `Path` to take a name from; `validate` prints the name, as the spec shows, because it holds the `Path` (§6).
6. **The dashboard draws the overlay as a fourth `.fact` chip**, `overlay /configs/WORKFLOW.local.md`, only when there is one; the CSS needs no change (a chip is `nowrap` and bordered already, #47). `dashboard_context` lists its keys explicitly rather than through `_WORKER_KEYS`, so both are edited (§6).
7. **`workflow_reloaded` logs `overlay=`** (the path or `None`) beside `path=` and `changed=`, so a reload caused by the overlay is legible in the log (§4).

## File map

| Path | Responsibility | Task |
|---|---|---|
| `src/issuebot/config/errors.py` | `ConfigError(..., overlay=)`, the `(+ name)` header | 1 |
| `src/issuebot/config/workflow.py` | `overlay_path_for`, `merge_front_matter`, `count_overrides`, the `Workflow` overlay fields, `load_workflow(overlay=)`, `_read_overlay` | 1 |
| `src/issuebot/config/__init__.py` | re-exports | 1 |
| `tests/test_workflow_overlay.py` | merge rules, discovery, merged settings, resolution, the prompt body | 1 |
| `tests/test_workflow_default.py` | loads with `overlay=False` | 1 |
| `src/issuebot/orchestrator/state.py` | `RuntimeSnapshot.workflow_overlay_path` | 2 |
| `src/issuebot/orchestrator/orchestrator.py` | `_reload_workflow` over both files, `_identity`, `_overlay_name`, the snapshot and log fields | 2 |
| `tests/test_orchestrator.py`, `tests/test_orchestrator_state.py` | `Harness.write_overlay`; create/edit/delete, invalid, mount point, unstatable; `to_dict` | 2 |
| `src/issuebot/cli.py` | `_workflow_detail` for `validate`; the `status` workflow line | 3 |
| `tests/test_cli.py` | the `validate` line with an overlay (plural and singular), an invalid overlay, the `status` line | 3 |
| `src/issuebot/web/views.py`, `templates/partials/dashboard.html` | `_WORKER_KEYS`, `dashboard_context`, the chip | 4 |
| `tests/fakes/web.py`, `tests/test_web_app.py`, `tests/test_web_pages.py` | `snapshot(workflow_overlay_path=)`; the state document; the worker line | 4 |
| `.gitignore`, `README.md`, `CLAUDE.md` | the ignore rule; the eight README passages; the four CLAUDE.md bullets | 5 |

Test counts along the way (`uv run pytest -q` without a database):

| After task | Count |
|---|---|
| (the spec commit) | 1045 passed, 52 skipped |
| 1 | 1073 passed, 52 skipped |
| 2 | 1077 passed, 52 skipped |
| 3 | 1080 passed, 52 skipped |
| 4 | 1082 passed, 52 skipped |
| 5 | 1082 passed, 52 skipped (docs only); 1134 passed with `test-db` |

---

### Task 1: The loader: discovery, merge, identity and the error header

**Files:**
- Modify: `src/issuebot/config/errors.py`, `src/issuebot/config/workflow.py`, `src/issuebot/config/__init__.py`, `tests/test_workflow_default.py`
- Create: `tests/test_workflow_overlay.py`

**Interfaces:**
- Consumes: `parse_workflow_text`, `resolve_config`, `Settings` (unchanged).
- Produces: `overlay_path_for(path: Path) -> Path`; `merge_front_matter(base: Mapping, overlay: Mapping) -> dict[str, Any]`; `count_overrides(overlay: Mapping) -> int`; `load_workflow(path, *, environ=None, overlay: bool = True) -> Workflow`; `Workflow.overlay_path: Path | None = None`, `overlay_mtime_ns`/`overlay_dev`/`overlay_ino: int = 0`, `overlay_config: dict[str, Any] = {}`, `Workflow.overlay_identity -> tuple[int, int, int]`; `ConfigError(message, *, path=None, overlay=None)` with `.overlay` and the `"<path> (+ <overlay.name>): <message>"` header; `SettingsValidationError(errors, *, path=None, overlay=None)`. All five names exported from `issuebot.config`.

Spec: §1, §2, §3, §7 (the loader bullets).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workflow_overlay.py`:

```python
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
```

In `tests/test_workflow_default.py` (edit 1 of 1) replace

```
def load() -> Workflow:
    return load_workflow(WORKFLOW, environ={"GH_TOKEN": "t"})
```

with

```
def load() -> Workflow:
    # `overlay=False`: `configs/` is where a developer working on issuebot keeps their own
    # `WORKFLOW.local.md`, and it must not be able to fail the suite for them alone.
    return load_workflow(WORKFLOW, environ={"GH_TOKEN": "t"}, overlay=False)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_workflow_overlay.py tests/test_workflow_default.py`
Expected: the new file fails at collection with

```
ImportError: cannot import name 'count_overrides' from 'issuebot.config'
```

and, run on its own (`uv run pytest -q tests/test_workflow_default.py`), the default-workflow file reports `10 failed`, every one with

```
E       TypeError: load_workflow() got an unexpected keyword argument 'overlay'
```

- [ ] **Step 3: The error header, the loader and the exports**

In `src/issuebot/config/errors.py` (edit 1 of 2) replace

```
    def __init__(self, message: str, *, path: Path | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.path = path

    def __str__(self) -> str:
        prefix = f"{self.path}: " if self.path is not None else ""
        return f"{prefix}{self.message}"
```

with

```
    def __init__(
        self, message: str, *, path: Path | None = None, overlay: Path | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.path = path
        # The local overlay merged over ``path`` when the error is about the merged result
        # (a resolution or validation failure), so the header names both files a reader
        # would have to look in. An error about one file alone carries only ``path``.
        self.overlay = overlay

    def __str__(self) -> str:
        if self.path is None:
            return self.message
        suffix = f" (+ {self.overlay.name})" if self.overlay is not None else ""
        return f"{self.path}{suffix}: {self.message}"
```

In `src/issuebot/config/errors.py` (edit 2 of 2) replace

```
    def __init__(self, errors: list[tuple[str, str]], *, path: Path | None = None) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} invalid setting(s)", path=path)
```

with

```
    def __init__(
        self,
        errors: list[tuple[str, str]],
        *,
        path: Path | None = None,
        overlay: Path | None = None,
    ) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} invalid setting(s)", path=path, overlay=overlay)
```

Replace the whole of `src/issuebot/config/workflow.py` with:

```python
"""WORKFLOW.md: YAML front matter plus a Markdown prompt body, and its local overlay."""

import copy
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from issuebot.config.errors import (
    ConfigError,
    FrontMatterNotAMap,
    MissingWorkflowFile,
    SettingsValidationError,
    WorkflowParseError,
)
from issuebot.config.resolve import resolve_config
from issuebot.config.settings import Settings

FRONT_MATTER_DELIMITER = "---"


@dataclass(frozen=True)
class Workflow:
    """A loaded WORKFLOW.md: typed settings plus the prompt template.

    ``raw_config`` is the front matter the settings were validated from: the base file's
    with the overlay's merged over it when there is one, since that is the mapping that
    explains ``config``. ``overlay_config`` is the overlay's own front matter, ``{}`` without
    one; ``overlay_path`` is the file it came from, ``None`` without one.
    """

    path: Path
    config: Settings
    prompt_template: str
    raw_config: dict[str, Any]
    source_mtime_ns: int
    # The device and inode the settings were read from. A watcher that keys only on the
    # mtime misses a file replaced by an atomic save (write a temporary file, rename it
    # over the original) that carries an mtime it already had -- a restore from an archive
    # or a checkout that preserves timestamps. Default 0 so a hand-built Workflow in a test
    # need not invent one; 0 for both is "unknown", and never equal to a real stat.
    source_dev: int = 0
    source_ino: int = 0
    # The overlay's identity, the same shape for the same reasons; all zero when there is
    # no overlay, and never equal to a real stat.
    overlay_path: Path | None = None
    overlay_mtime_ns: int = 0
    overlay_dev: int = 0
    overlay_ino: int = 0
    overlay_config: dict[str, Any] = field(default_factory=dict)

    @property
    def source_identity(self) -> tuple[int, int, int]:
        """``(dev, ino, mtime_ns)`` of the file this was read from.

        The whole triple, not the mtime alone: the file the path names now is the same
        file only if all three match.
        """
        return (self.source_dev, self.source_ino, self.source_mtime_ns)

    @property
    def overlay_identity(self) -> tuple[int, int, int]:
        """``(dev, ino, mtime_ns)`` of the overlay, or all zero when there was none."""
        return (self.overlay_dev, self.overlay_ino, self.overlay_mtime_ns)


def overlay_path_for(path: Path) -> Path:
    """The local overlay's path: ``WORKFLOW.md`` -> ``WORKFLOW.local.md``, beside it.

    Derived, never configured. A sibling of a file inside a mounted directory is reached
    through the same directory entry, so it live-reloads for the same reason the base does
    (#46); an overlay that could be pointed anywhere could be pinned to an inode again.
    There is no chaining: an overlay's own overlay would be ``WORKFLOW.local.local.md``.
    """
    return path.with_name(f"{path.stem}.local{path.suffix}")


def merge_front_matter(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """The overlay merged over the base, as a new mapping; neither argument is touched.

    Three rules. Two mappings merge key by key, recursively. Anything else replaces: a
    scalar, a list as a whole (an event allow-list is a choice, not an accumulation), and
    a mapping on one side where the other holds a scalar or a list. An explicit ``null`` in
    the overlay deletes the key, so the setting falls back to its ``Settings`` default; a
    ``null`` naming a key the base does not set is a no-op.
    """
    merged: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = merge_front_matter(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def count_overrides(overlay: Mapping[str, Any]) -> int:
    """How many leaf keypaths the overlay sets, a ``null`` (a delete) counting as one."""
    return sum(
        count_overrides(value) if isinstance(value, Mapping) else 1 for value in overlay.values()
    )


def parse_workflow_text(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into ``(front_matter_mapping, stripped_body)``.

    CRLF is normalised, a leading BOM is dropped, and a file without a leading ``---``
    line is treated as body only with an empty mapping.
    """
    text = text.lstrip("﻿").replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != FRONT_MATTER_DELIMITER:
        return {}, text.strip()

    end = next(
        (i for i in range(1, len(lines)) if lines[i].rstrip() == FRONT_MATTER_DELIMITER),
        None,
    )
    if end is None:
        raise WorkflowParseError("front matter opened with '---' but never closed")

    front_matter = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        raw = yaml.safe_load(front_matter)
    except yaml.YAMLError as exc:
        raise WorkflowParseError(f"invalid YAML front matter: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise FrontMatterNotAMap(f"front matter must be a mapping, got {type(raw).__name__}")
    return raw, body.strip()


def load_workflow(
    path: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
    overlay: bool = True,
) -> Workflow:
    """Read, parse, resolve and validate a WORKFLOW.md, with its local overlay if one exists.

    The overlay is the sibling ``overlay_path_for`` names. Its front matter is merged over
    the base's (``merge_front_matter``) before resolution, so a fallback ``resolve_config``
    fills in for an absent field can never clobber a value the other file set, and the
    merged mapping is validated once, so a typo in either file fails the same way. Its
    body replaces the base's only when it has one. ``overlay=False`` ignores it: for a test
    of the repository's own ``configs/WORKFLOW.md``, which is exactly where a developer
    keeps their overlay.

    Every failure is a ``ConfigError`` subclass carrying the absolute path of the file it
    is about, or of the base with the overlay named beside it when it is about the merge.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    resolved_path = Path(path).expanduser().resolve()
    try:
        # Stat before read: a racing rewrite then yields content at least as new as
        # the recorded mtime, so the next reload sees the change.
        source = resolved_path.stat()
        text = resolved_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise MissingWorkflowFile(
            f"workflow file not found: {resolved_path}", path=resolved_path
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise MissingWorkflowFile(f"workflow file unreadable: {exc}", path=resolved_path) from exc

    try:
        raw, body = parse_workflow_text(text)
    except ConfigError as exc:
        exc.path = resolved_path
        raise

    local_path: Path | None = None
    local: os.stat_result | None = None
    local_raw: dict[str, Any] = {}
    if overlay:
        local_path, local, local_raw, local_body = _read_overlay(
            overlay_path_for(resolved_path), base=resolved_path
        )
        if local_body:
            body = local_body
    merged = merge_front_matter(raw, local_raw) if local_path is not None else raw

    try:
        resolved = resolve_config(merged, environ=env, base_dir=resolved_path.parent)
    except ConfigError as exc:
        exc.path = resolved_path
        exc.overlay = local_path
        raise

    try:
        settings = Settings.model_validate(resolved)
    except ValidationError as exc:
        raise SettingsValidationError(
            _format_errors(exc), path=resolved_path, overlay=local_path
        ) from exc

    return Workflow(
        path=resolved_path,
        config=settings,
        prompt_template=body,
        raw_config=merged,
        source_mtime_ns=source.st_mtime_ns,
        source_dev=source.st_dev,
        source_ino=source.st_ino,
        overlay_path=local_path,
        overlay_mtime_ns=local.st_mtime_ns if local is not None else 0,
        overlay_dev=local.st_dev if local is not None else 0,
        overlay_ino=local.st_ino if local is not None else 0,
        overlay_config=local_raw,
    )


def _read_overlay(
    overlay_path: Path, *, base: Path
) -> tuple[Path | None, os.stat_result | None, dict[str, Any], str]:
    """``(path, stat, front_matter, body)`` of the overlay; ``(None, None, {}, "")`` without one.

    A missing overlay is the normal case. One that exists but is not a regular file is an
    error naming it, rather than a "workflow file unreadable" from reading a directory.
    """
    try:
        local = overlay_path.stat()
    except FileNotFoundError:
        return None, None, {}, ""
    except OSError as exc:
        raise MissingWorkflowFile(f"workflow overlay unreadable: {exc}", path=base) from exc
    if not stat.S_ISREG(local.st_mode):
        raise MissingWorkflowFile(
            f"workflow overlay is not a regular file: {overlay_path}", path=base
        )
    try:
        text = overlay_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MissingWorkflowFile(f"workflow overlay unreadable: {exc}", path=base) from exc
    try:
        raw, body = parse_workflow_text(text)
    except ConfigError as exc:
        exc.path = overlay_path
        raise
    return overlay_path, local, raw, body


def _format_errors(exc: ValidationError) -> list[tuple[str, str]]:
    return [
        (".".join(str(part) for part in error["loc"]) or "<root>", error["msg"])
        for error in exc.errors()
    ]
```

One thing to check by eye after pasting: `parse_workflow_text` is unchanged from the existing file, and its `text.lstrip(...)` argument is a literal U+FEFF (the BOM), invisible in most editors. It survives a copy from this plan; if it does not, `"﻿"` is the same string. `test_bom_is_ignored` in `tests/test_workflow.py` catches a lost one.

In `src/issuebot/config/__init__.py` (edit 1 of 2) replace

```
from issuebot.config.workflow import Workflow, load_workflow, parse_workflow_text
```

with

```
from issuebot.config.workflow import (
    Workflow,
    count_overrides,
    load_workflow,
    merge_front_matter,
    overlay_path_for,
    parse_workflow_text,
)
```

In `src/issuebot/config/__init__.py` (edit 2 of 2) replace

```
    "WorkspaceSettings",
    "load_workflow",
    "parse_workflow_text",
]
```

with

```
    "WorkspaceSettings",
    "count_overrides",
    "load_workflow",
    "merge_front_matter",
    "overlay_path_for",
    "parse_workflow_text",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`
Expected: `All checks passed!`, every file already formatted, and `1073 passed, 52 skipped` (28 new: 27 tests in the new file, one of them parametrised twice).

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/config/errors.py src/issuebot/config/workflow.py src/issuebot/config/__init__.py tests/test_workflow_overlay.py tests/test_workflow_default.py
git commit -m "feat(config): merge a local WORKFLOW.local.md overlay over the workflow file"
```

(with the attribution trailer the harness requires as the last lines of the message).

---

### Task 2: The orchestrator: reload on either file, report the overlay in the snapshot

**Files:**
- Modify: `src/issuebot/orchestrator/state.py`, `src/issuebot/orchestrator/orchestrator.py`
- Test: `tests/test_orchestrator.py`, `tests/test_orchestrator_state.py`

**Interfaces:**
- Consumes: Task 1's `overlay_path_for`, `Workflow.overlay_path`, `Workflow.overlay_identity`; the existing `_pinned_mount_complaint(path, stat)`, `_report_reload_failure`, `_changed_sections`.
- Produces: `RuntimeSnapshot.workflow_overlay_path: str | None = None` (and so the key in `to_dict()` and the stored `jsonb`); `_reload_workflow` reloading on either identity or the overlay's presence, with `workflow overlay unreadable: <exc>` as the reload failure for a non-`FileNotFoundError` stat; the complaint over both files; log `workflow_reloaded` with `overlay=`; module helpers `_identity(stat) -> tuple[int, int, int]` and `_overlay_name(workflow) -> str | None`; `Harness.write_overlay(text) -> Path` in the tests.

Spec: §4, §6 (the snapshot), §7 (the orchestrator bullets).

- [ ] **Step 1: Write the failing tests**

In `tests/test_orchestrator.py` (edit 1 of 3) replace

```
from issuebot.config import Settings, load_workflow
```

with

```
from issuebot.config import Settings, load_workflow, overlay_path_for
```

In `tests/test_orchestrator.py` (edit 2 of 3) replace

```
    def claude_extra(self) -> str:
        lines = [f"  model: {self.model}\n"] if self.model else []
```

with

```
    def write_overlay(self, text: str) -> Path:
        """The local overlay beside the workflow file, with a forced distinct mtime."""
        overlay = overlay_path_for(self.path)
        overlay.write_text(text, encoding="utf-8")
        previous = getattr(self, "_overlay_mtime", 1_700_000_000)
        self._overlay_mtime = previous + 1
        os.utime(overlay, ns=(self._overlay_mtime * 1_000_000_000,) * 2)
        return overlay

    def claude_extra(self) -> str:
        lines = [f"  model: {self.model}\n"] if self.model else []
```

In `tests/test_orchestrator.py` (edit 3 of 3) replace

```
async def test_missing_workflow_file_is_reported_not_fatal(tmp_path: Path) -> None:
```

with

```
async def test_reload_follows_the_overlay_being_created_edited_and_deleted(
    tmp_path: Path,
) -> None:
    """The overlay reloads on the same terms as the base: presence and identity."""
    h = Harness(tmp_path)
    await h.tick()
    assert h.snapshots[-1].max_concurrent_agents == 2
    assert h.snapshots[-1].workflow_overlay_path is None

    overlay = h.write_overlay("---\nagent:\n  max_concurrent_agents: 4\n---\n")
    with capture_logs() as logs:
        await h.tick()
    assert h.snapshots[-1].max_concurrent_agents == 4
    assert h.snapshots[-1].workflow_overlay_path == str(overlay)
    assert h.snapshots[-1].config_valid is True
    reloaded = next(entry for entry in logs if entry["event"] == "workflow_reloaded")
    assert (reloaded["overlay"], reloaded["changed"]) == (str(overlay), ["agent"])

    h.write_overlay("---\nagent:\n  max_concurrent_agents: 5\n---\n")
    await h.tick()
    assert h.snapshots[-1].max_concurrent_agents == 5

    overlay.unlink()
    await h.tick()
    assert h.snapshots[-1].max_concurrent_agents == 2
    assert h.snapshots[-1].workflow_overlay_path is None
    assert h.snapshots[-1].config_valid is True


async def test_an_invalid_overlay_keeps_the_last_good_workflow_and_names_it(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    await h.tick()
    h.write_overlay("---\nagent:\n  bogus: 1\n---\n")
    await h.tick()
    snapshot = h.snapshots[-1]
    assert snapshot.config_valid is False
    assert snapshot.config_error is not None
    assert "(+ WORKFLOW.local.md)" in snapshot.config_error and "bogus" in snapshot.config_error
    assert h.orchestrator.workflow is h.workflow
    assert snapshot.workflow_overlay_path is None


async def test_an_overlay_mounted_singly_is_reported_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overlay on a different device from its directory is a mount point too (#46)."""
    h = Harness(tmp_path)
    await h.tick()
    overlay = h.write_overlay("---\nagent:\n  max_concurrent_agents: 4\n---\n")
    real_stat = Path.stat

    def mounted_stat(self: Path, **kwargs: Any) -> Any:
        source = real_stat(self, **kwargs)
        if self != overlay:
            return source
        return SimpleNamespace(
            st_nlink=1,
            st_dev=source.st_dev + 1,
            st_ino=source.st_ino,
            st_mtime_ns=source.st_mtime_ns,
            st_mode=source.st_mode,
        )

    # Mounted from the start: the overlay is loaded with the mount's device, as a mounted
    # file would be, and on the next tick nothing has changed and the complaint runs.
    monkeypatch.setattr(Path, "stat", mounted_stat)
    await h.tick()
    assert h.snapshots[-1].max_concurrent_agents == 4
    assert h.snapshots[-1].config_valid is True
    with capture_logs() as logs:
        await h.tick()
    complaint = next(entry for entry in logs if entry["event"] == "workflow_reload_failed")
    assert complaint["log_level"] == "error"
    assert "single-file mount" in complaint["error"]
    assert str(overlay) in complaint["error"]
    assert str(h.path) not in complaint["error"]
    assert "single-file mount" in (h.snapshots[-1].config_error or "")
    # Advisory: the settings in force are still the overlay's.
    assert h.snapshots[-1].max_concurrent_agents == 4


async def test_an_overlay_that_cannot_be_stated_is_a_reload_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a missing overlay means "no overlay"; any other answer is reported."""
    h = Harness(tmp_path)
    await h.tick()
    overlay = overlay_path_for(h.path)
    real_stat = Path.stat

    def forbidden_stat(self: Path, **kwargs: Any) -> Any:
        if self == overlay:
            raise PermissionError(13, "Permission denied", str(overlay))
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", forbidden_stat)
    await h.tick()
    snapshot = h.snapshots[-1]
    assert snapshot.config_valid is False
    assert "workflow overlay unreadable" in (snapshot.config_error or "")
    assert h.orchestrator.workflow is h.workflow


async def test_missing_workflow_file_is_reported_not_fatal(tmp_path: Path) -> None:
```

In `tests/test_orchestrator_state.py` (edit 1 of 1) replace

```
    data = snapshot.to_dict()
    assert json.dumps(data)
    row = data["running"][0]
```

with

```
    data = snapshot.to_dict()
    assert json.dumps(data)
    # The overlay rides the snapshot like the other worker facts; None without one, and a
    # snapshot built without naming it (every earlier caller) says so.
    assert data["workflow_overlay_path"] is None
    row = data["running"][0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_orchestrator.py -k overlay tests/test_orchestrator_state.py::test_snapshot_rows_and_to_dict`
Expected: five failures. The four orchestrator tests fail with lines of these three shapes,

```
E       AttributeError: 'RuntimeSnapshot' object has no attribute 'workflow_overlay_path'
E       AssertionError: assert True is False
E       AssertionError: assert 2 == 4
```

(the created overlay is never loaded, so the slot count stays 2 and no complaint is raised), and the state test fails with `KeyError: 'workflow_overlay_path'`.

- [ ] **Step 3: The snapshot field and the two-file reload**

In `src/issuebot/orchestrator/state.py` (edit 1 of 1) replace

```
    at: datetime
    workflow_path: str
    workflow_mtime_ns: int
    config_valid: bool
```

with

```
    at: datetime
    workflow_path: str
    workflow_mtime_ns: int
    # The local overlay in force, so "is the worker running my overrides?" has an answer
    # in `issuebot status`, `/api/v1/state` and the dashboard's worker line; None without one.
    workflow_overlay_path: str | None = None
    config_valid: bool
```

In `src/issuebot/orchestrator/orchestrator.py` (edit 1 of 4) replace

```
from issuebot.config import ConfigError, GitHubSettings, Settings, Workflow, load_workflow
```

with

```
from issuebot.config import (
    ConfigError,
    GitHubSettings,
    Settings,
    Workflow,
    load_workflow,
    overlay_path_for,
)
```

In `src/issuebot/orchestrator/orchestrator.py` (edit 2 of 4) replace

```
            workflow_path=str(self._workflow.path),
            workflow_mtime_ns=self._workflow.source_mtime_ns,
            config_valid=self._config_error is None,
```

with

```
            workflow_path=str(self._workflow.path),
            workflow_mtime_ns=self._workflow.source_mtime_ns,
            workflow_overlay_path=_overlay_name(self._workflow),
            config_valid=self._config_error is None,
```

In `src/issuebot/orchestrator/orchestrator.py` (edit 3 of 4) replace the whole method

```
    def _reload_workflow(self) -> None:
        path = self._workflow.path
        try:
            source = path.stat()
        except OSError as exc:
            self._report_reload_failure(f"workflow file unreadable: {exc}")
            return
        if (source.st_dev, source.st_ino, source.st_mtime_ns) == self._workflow.source_identity:
            # Nothing to load. The one thing left worth saying is that this deployment may
            # not be in a position to tell (#46).
            complaint = _pinned_mount_complaint(path, source)
            if complaint is not None:
                self._report_reload_failure(complaint)
            return
        try:
            workflow = load_workflow(path, environ=self._environ)
        except ConfigError as exc:
            self._report_reload_failure(str(exc))
            return
        changed = _changed_sections(self._workflow, workflow)
        self._workflow = workflow
        self._config_error = None
        self._reported_reload_error = None
        self._adapter = self._adapter_factory(workflow.config.github)
        self._workspaces = self._workspaces_factory(workflow.config)
        self._log.info("workflow_reloaded", path=str(path), changed=changed)
```

with

```
    def _reload_workflow(self) -> None:
        workflow = self._workflow
        path = workflow.path
        try:
            source = path.stat()
        except OSError as exc:
            self._report_reload_failure(f"workflow file unreadable: {exc}")
            return
        overlay_path = overlay_path_for(path)
        try:
            overlay: os.stat_result | None = overlay_path.stat()
        except FileNotFoundError:
            # The normal case: no overlay.
            overlay = None
        except OSError as exc:
            # A file that exists and cannot be stat'ed is not something to guess about.
            self._report_reload_failure(f"workflow overlay unreadable: {exc}")
            return
        loaded = (
            workflow.source_identity,
            workflow.overlay_identity if workflow.overlay_path is not None else None,
        )
        found = (
            (source.st_dev, source.st_ino, source.st_mtime_ns),
            _identity(overlay) if overlay is not None else None,
        )
        if loaded == found:
            # Nothing to load: neither identity has moved and the overlay's presence has not
            # changed. The one thing left worth saying is that this deployment may not be
            # in a position to tell (#46), about either file.
            complaint = _pinned_mount_complaint(path, source)
            if complaint is None and overlay is not None:
                complaint = _pinned_mount_complaint(overlay_path, overlay)
            if complaint is not None:
                self._report_reload_failure(complaint)
            return
        try:
            workflow = load_workflow(path, environ=self._environ)
        except ConfigError as exc:
            self._report_reload_failure(str(exc))
            return
        changed = _changed_sections(self._workflow, workflow)
        self._workflow = workflow
        self._config_error = None
        self._reported_reload_error = None
        self._adapter = self._adapter_factory(workflow.config.github)
        self._workspaces = self._workspaces_factory(workflow.config)
        self._log.info(
            "workflow_reloaded", path=str(path), overlay=_overlay_name(workflow), changed=changed
        )
```

In `src/issuebot/orchestrator/orchestrator.py` (edit 4 of 4) replace

```
def _changed_sections(old: Workflow, new: Workflow) -> list[str]:
```

with

```
def _identity(source: os.stat_result) -> tuple[int, int, int]:
    return (source.st_dev, source.st_ino, source.st_mtime_ns)


def _overlay_name(workflow: Workflow) -> str | None:
    return str(workflow.overlay_path) if workflow.overlay_path is not None else None


def _changed_sections(old: Workflow, new: Workflow) -> list[str]:
```

`os` is already imported at the top of the module.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`
Expected: clean, and `1077 passed, 52 skipped`. The three existing #46 tests (`test_a_stale_single_file_mount_is_reported_at_error`, `test_a_file_mounted_singly_is_reported_before_it_goes_stale`, `test_a_loose_link_count_never_suppresses_a_real_reload`) still pass unchanged: their fake stats answer only for the base or its parent, and the overlay's stat raises `FileNotFoundError`, which is "no overlay".

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/orchestrator/state.py src/issuebot/orchestrator/orchestrator.py tests/test_orchestrator.py tests/test_orchestrator_state.py
git commit -m "feat(orchestrator): reload on the overlay and report it in the snapshot"
```

---

### Task 3: `validate` and `status` name the overlay

**Files:**
- Modify: `src/issuebot/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1's `count_overrides`, `Workflow.overlay_path`, `Workflow.overlay_config`; Task 2's `workflow_overlay_path` key in the snapshot JSON.
- Produces: `cli._workflow_detail(workflow) -> str` (`<path>` or `<path> + <overlay name> (<n> override[s])`) as the `workflow` check's detail; `render_status`'s `workflow:` line reading `<base> + <overlay path> (<config>)` when the snapshot carries one.

Spec: §6, §7 (the CLI bullet).

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py` (edit 1 of 2) replace

```
def test_validate_token_from_fallback_variable(
```

with

```
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
    assert out.rstrip().endswith("13 checks: 0 failed, 1 warnings")

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
```

In `tests/test_cli.py` (edit 2 of 2) replace

```
def test_render_status_names_a_held_dispatch() -> None:
```

with

```
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


def test_render_status_names_a_held_dispatch() -> None:
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_cli.py -k overlay`
Expected: 2 failed, 1 passed. `test_validate_reports_an_invalid_overlay_naming_both_files` already passes (Task 1 produces the header); the other two fail with

```
E       AssertionError: assert '[ OK ] workflow: .../WORKFLOW.md + WORKFLOW.local.md (2 overrides)' in '[ OK ] workflow: .../WORKFLOW.md\n[ OK ] github.repo...
```

and

```
E         - workflow: /configs/WORKFLOW.md + /configs/WORKFLOW.local.md (config valid)
E         + workflow: /configs/WORKFLOW.md (config valid)
```

- [ ] **Step 3: The check detail and the status line**

In `src/issuebot/cli.py` (edit 1 of 4) replace, inside the `from issuebot.config import (` block,

```
    Workflow,
    load_workflow,
)
from issuebot.config.resolve import ENV_REF
```

with

```
    Workflow,
    count_overrides,
    load_workflow,
)
from issuebot.config.resolve import ENV_REF
```

In `src/issuebot/cli.py` (edit 2 of 4) replace

```
    checks = [
        Check("workflow", "ok", str(workflow.path)),
```

with

```
    checks = [
        Check("workflow", "ok", _workflow_detail(workflow)),
```

In `src/issuebot/cli.py` (edit 3 of 4) replace

```
def _github_checks(adapter: GitHubAdapter | None, model_labels: Sequence[str] = ()) -> list[Check]:
```

with

```
def _workflow_detail(workflow: Workflow) -> str:
    """The path, and with an overlay its name and how many settings it overrides.

    The overlay creates one new question, "is the worker running my overrides?", and this
    is the first of the two places that answer it (the other is the runtime snapshot).
    """
    if workflow.overlay_path is None:
        return str(workflow.path)
    count = count_overrides(workflow.overlay_config)
    noun = "override" if count == 1 else "overrides"
    return f"{workflow.path} + {workflow.overlay_path.name} ({count} {noun})"


def _github_checks(adapter: GitHubAdapter | None, model_labels: Sequence[str] = ()) -> list[Check]:
```

In `src/issuebot/cli.py` (edit 4 of 4), in `render_status`, replace

```
    counters = dict(data.get("counters") or {})
    lines = [
        f"snapshot: {_stamp(row.at)} (written {_stamp(row.written_at)}, {age:.0f} s ago)",
        f"workflow: {data.get('workflow_path')} ({config})",
```

with

```
    counters = dict(data.get("counters") or {})
    overlay = data.get("workflow_overlay_path")
    workflow = f"{data.get('workflow_path')} + {overlay}" if overlay else data.get("workflow_path")
    lines = [
        f"snapshot: {_stamp(row.at)} (written {_stamp(row.written_at)}, {age:.0f} s ago)",
        f"workflow: {workflow} ({config})",
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`
Expected: clean, and `1080 passed, 52 skipped`. `test_validate_good_workflow_exits_zero` still asserts `[ OK ] workflow: {GOOD.resolve()}` with no suffix: the fixture directory has no overlay.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/cli.py tests/test_cli.py
git commit -m "feat(cli): name the overlay in validate and status"
```

---

### Task 4: The state API and the dashboard's worker line

**Files:**
- Modify: `src/issuebot/web/views.py`, `src/issuebot/web/templates/partials/dashboard.html`
- Test: `tests/fakes/web.py`, `tests/test_web_app.py`, `tests/test_web_pages.py`

**Interfaces:**
- Consumes: Task 2's `RuntimeSnapshot.workflow_overlay_path` (through `tests/fakes/web.py`'s `snapshot()` builder and the stored JSON).
- Produces: `worker.workflow_overlay_path` in `/api/v1/state` (through `_WORKER_KEYS`); `workflow_overlay_path` in `dashboard_context`'s `worker` mapping; the chip `<span class="fact overlay">overlay <path></span>` on the worker line, drawn only when the path is set; `snapshot(workflow_overlay_path=None)` in the web fakes.

Spec: §6, §7 (the web bullet).

- [ ] **Step 1: Write the failing tests**

In `tests/fakes/web.py` (edit 1 of 1) replace

```
    credential: str = "subscription",
    rate_limits: RateLimits | None = None,
) -> SnapshotRow:
    data = RuntimeSnapshot(
        at=NOW - timedelta(seconds=age_s + 1),
        workflow_path="/configs/WORKFLOW.md",
        workflow_mtime_ns=1,
        config_valid=True,
```

with

```
    credential: str = "subscription",
    rate_limits: RateLimits | None = None,
    workflow_overlay_path: str | None = None,
) -> SnapshotRow:
    data = RuntimeSnapshot(
        at=NOW - timedelta(seconds=age_s + 1),
        workflow_path="/configs/WORKFLOW.md",
        workflow_mtime_ns=1,
        workflow_overlay_path=workflow_overlay_path,
        config_valid=True,
```

In `tests/test_web_app.py` (edit 1 of 2), inside `test_state_reshapes_the_snapshot`, replace

```
        "workflow_path": "/configs/WORKFLOW.md",
        "config_valid": True,
        "config_error": None,
        "dispatch_hold": None,
        "status": "ok",
        "stale": False,
    }
```

with

```
        "workflow_path": "/configs/WORKFLOW.md",
        "workflow_overlay_path": None,
        "config_valid": True,
        "config_error": None,
        "dispatch_hold": None,
        "status": "ok",
        "stale": False,
    }
```

In `tests/test_web_app.py` (edit 2 of 2) replace

```
def test_state_without_a_snapshot_is_empty_not_missing(h: Harness) -> None:
```

with

```
def test_state_names_the_overlay_in_force(h: Harness) -> None:
    """The API's answer to "is the worker running my overrides?"."""
    h.queries.snapshot_row = snapshot(workflow_overlay_path="/configs/WORKFLOW.local.md")
    worker = h.client.get("/api/v1/state").json()["worker"]
    assert worker["workflow_overlay_path"] == "/configs/WORKFLOW.local.md"


def test_state_without_a_snapshot_is_empty_not_missing(h: Harness) -> None:
```

In `tests/test_web_pages.py` (edit 1 of 1) replace

```
def test_an_alerting_verdict_is_prose_in_the_bad_token(h: Harness) -> None:
```

with

```
def test_the_worker_line_names_the_overlay_in_force(h: Harness) -> None:
    """A fourth fact, drawn only when there is one: the dashboard's answer to "is the worker
    running my overrides?"."""
    h.queries.snapshot_row = snapshot(workflow_overlay_path="/configs/WORKFLOW.local.md")
    text = html(h.client.get("/partials/dashboard"))
    line = text[text.index('<section class="panel worker') :]
    line = line[: line.index("</section>")]
    assert '<span class="fact overlay">overlay /configs/WORKFLOW.local.md</span>' in line

    h.queries.snapshot_row = snapshot()
    text = html(h.client.get("/partials/dashboard"))
    assert "overlay" not in text[text.index('<section class="panel worker') :]


def test_an_alerting_verdict_is_prose_in_the_bad_token(h: Harness) -> None:
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_web_app.py tests/test_web_pages.py -k "overlay or reshapes"`
Expected: 3 failed, with

```
E         Right contains 1 more item:
E         {'workflow_overlay_path': None}
E       KeyError: 'workflow_overlay_path'
E       assert '<span class="fact overlay">overlay /configs/WORKFLOW.local.md</span>' in '<section class="panel worker ok">...
```

- [ ] **Step 3: The key, the context and the chip**

In `src/issuebot/web/views.py` (edit 1 of 2) replace

```
    "workflow_path",
    "config_valid",
    "config_error",
)
```

with

```
    "workflow_path",
    "workflow_overlay_path",
    "config_valid",
    "config_error",
)
```

In `src/issuebot/web/views.py` (edit 2 of 2), inside `dashboard_context`, replace

```
            max_concurrent_agents=data.get("max_concurrent_agents"),
            config_valid=data.get("config_valid"),
```

with

```
            max_concurrent_agents=data.get("max_concurrent_agents"),
            workflow_overlay_path=data.get("workflow_overlay_path"),
            config_valid=data.get("config_valid"),
```

In `src/issuebot/web/templates/partials/dashboard.html` (edit 1 of 1) replace

```
    <span class="fact">{{ worker.max_concurrent_agents }} slots</span>
  </span>
```

with

```
    <span class="fact">{{ worker.max_concurrent_agents }} slots</span>
    {% if worker.workflow_overlay_path %}
    <span class="fact overlay">overlay {{ worker.workflow_overlay_path }}</span>
    {% endif %}
  </span>
```

No CSS change: `.worker .fact` already draws a bordered, `nowrap` chip (#47), and `test_the_worker_facts_are_bounded_rather_than_run_together` keeps guarding that.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`
Expected: clean, and `1082 passed, 52 skipped`.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/web/views.py src/issuebot/web/templates/partials/dashboard.html tests/fakes/web.py tests/test_web_app.py tests/test_web_pages.py
git commit -m "feat(web): name the overlay in force on the worker line and in the state API"
```

---

### Task 5: The ignore rule and the documentation

**Files:**
- Modify: `.gitignore`, `README.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: everything above, as documented behaviour.
- Produces: `configs/*.local.md` ignored; the README's "Local overrides" block, Step 1, "Configuration changes", "More than one repository" and "Upgrades" passages; the `issuebot.config`, `issuebot.orchestrator`, `issuebot.web` and `issuebot.cli` bullets in `CLAUDE.md`.

Spec: §5, §8.

Reminder from the constraints: the `.gitignore` edit and README edit 5 of 7 sit next to, or contain, the dot-env filename. Make every edit in this task with the **Edit tool**; a heredoc or `sed` carrying that text is blocked by the host hook and aborts the whole command.

- [ ] **Step 1: The ignore rule**

In `.gitignore` (edit 1 of 1) replace

```
.claude/worktrees/
.claude/settings.local.json

```

with

```
.claude/worktrees/
.claude/settings.local.json

# The deployment's own settings: `github.repo`, the budget, the model, whatever this
# checkout overrides in the tracked `configs/WORKFLOW.md`. Machine-local by nature,
# like the worktrees above: one checkout watches one repository, and tracking the file
# would put that repository's name in issuebot's own history and make every `git pull`
# a merge on the one file the operator has rewritten. `*.local.md` rather than the one
# name so a second workflow file on a host (`--workflow frontend.md`) has its
# `frontend.local.md` ignored too.
configs/*.local.md

```

Run: `touch configs/WORKFLOW.local.md configs/frontend.local.md && git check-ignore -v configs/WORKFLOW.local.md configs/frontend.local.md && git status --short && rm configs/WORKFLOW.local.md configs/frontend.local.md`
Expected: two `.gitignore:NN:configs/*.local.md` lines, and `git status --short` shows only `.gitignore` modified (the two files never appear).

- [ ] **Step 2: The README**

In `README.md` (edit 1 of 7), under "What is configured where", replace

```
  repository. Secrets never go in the file: a field is either omitted (and the well-known
  variable is used) or set to `$VAR`.
```

with

```
  repository. Secrets never go in the file: a field is either omitted (and the well-known
  variable is used) or set to `$VAR`.
- **Local overrides.** `configs/WORKFLOW.local.md`, beside the tracked file and git-ignored,
  holds this deployment's own settings. Its front matter is merged over the tracked file's,
  so a deployment's whole configuration can be four lines:

  ```yaml
  ---
  github:
    repo: acme/frontend
  claude:
    max_budget_usd: 3.0
  ---
  ```

  Everything else, the prompt included, keeps coming from the tracked file, so `git pull`
  brings prompt improvements and new defaults with no merge and `git status` stays clean.
  The merge has three rules: a mapping merges key by key, anything else replaces (a list as
  a whole, so a deployment can subscribe to *fewer* Slack event kinds), and an explicit
  `null` deletes the key so the setting falls back to its default (`hooks.after_create:
  null` drops the shipped hook; `claude.model: null` takes Claude Code's default). Anything
  after the overlay's front matter replaces the prompt; leave it out to inherit. `validate`
  names the overlay and counts its overrides, and a running worker reports the one in force
  in `issuebot status`, on the dashboard's worker line and in `/api/v1/state`.
```

In `README.md` (edit 2 of 7), in Step 1, replace

```
Then edit the front matter of `configs/WORKFLOW.md`. The one required change is
`github.repo`; the checked-in file points at this repository. Unknown keys are rejected, so
a typo fails at `validate` rather than being silently ignored.
```

with

```
Then create `configs/WORKFLOW.local.md` and set `github.repo` in it, the one required
change; the checked-in `configs/WORKFLOW.md` points at this repository and stays as it is.
Every key below can be set in the overlay, which is where a deployment's settings belong
(see "Local overrides" above); the tracked file holds the defaults and the prompt. Unknown
keys are rejected in either file, so a typo fails at `validate` rather than being silently
ignored.
```

In `README.md` (edit 3 of 7), under "Configuration changes", replace

```
- **Configuration changes.** A running worker re-reads `configs/WORKFLOW.md` when it
  changes, within one `polling.interval_ms`, and logs `workflow_reloaded` naming the sections
  that moved.
```

with

```
- **Configuration changes.** A running worker re-reads `configs/WORKFLOW.md` and
  `configs/WORKFLOW.local.md` when either changes, or the overlay appears or disappears,
  within one `polling.interval_ms`, and logs `workflow_reloaded` naming the sections
  that moved.
```

In `README.md` (edit 4 of 7), later in the same bullet, replace

```
  mounted individually from anywhere else has the same problem, in Compose or anywhere else
  that mounts one file (a Kubernetes `subPath`, say).
```

with

```
  mounted individually from anywhere else has the same problem, in Compose or anywhere else
  that mounts one file (a Kubernetes `subPath`, say). The overlay is looked for beside
  `WORKFLOW.md` and nowhere else for exactly this reason: a sibling inside the mounted
  directory reloads on the same terms as the base, and a `WORKFLOW.local.md` mounted on its
  own would be pinned the same way, holding the settings you change most often.
```

In `README.md` (edit 5 of 7), under "More than one repository" (**Edit tool only**: the paragraph names the dot-env file), replace the three lines that begin

```
directory set `github.repo` in `configs/WORKFLOW.md`, and give its
```

and run to `for the other).` with

```
directory set `github.repo` in `configs/WORKFLOW.local.md`, never in the tracked file, and
give its `.env` distinct `ISSUEBOT_DB_PORT` and `ISSUEBOT_WEB_PORT` values (say 5432 and
8080 for one, 5433 and 8081 for the other).
```

(the middle line keeps the original sentence's `` `.env` distinct `` wording; only the wrapping and the file name change).

In `README.md` (edit 6 of 7), in the same section, replace

```
On the host, run one `issuebot worker` and one `issuebot web` per repository, each with its
own `--workflow` file (or `ISSUEBOT_WORKFLOW`), its own `workspace.root`, its own database
```

with

```
On the host, run one `issuebot worker` and one `issuebot web` per repository, each with its
own `--workflow` file (or `ISSUEBOT_WORKFLOW`) and that file's own overlay beside it
(`frontend.md` reads `frontend.local.md`), its own `workspace.root`, its own database
```

In `README.md` (edit 7 of 7), under "Upgrades", replace

```
- **Upgrades.** `configs/` is mounted into the container, but the code is baked into the
  image: after pulling a new version of issuebot, run `docker compose build` (or
  `docker compose up --build -d`) before anything else.
```

with

```
- **Upgrades.** With your settings in `configs/WORKFLOW.local.md` and the tracked
  `configs/WORKFLOW.md` untouched, a clean `git pull` is the expected experience: the prompt
  and the defaults update, your overrides stay. If you edited the tracked file before the
  overlay existed, move those edits into the overlay and `git checkout configs/WORKFLOW.md`
  first. `configs/` is mounted into the container, but the code is baked into the
  image: after pulling a new version of issuebot, run `docker compose build` (or
  `docker compose up --build -d`) before anything else.
```

- [ ] **Step 3: CLAUDE.md**

In `CLAUDE.md` (edit 1 of 6), at the end of the `issuebot.config` bullet, replace

```
  (`settings.py`, `extra="forbid"`). Errors are `ConfigError` subclasses with a `code`.
- `issuebot.log`:
```

with

```
  (`settings.py`, `extra="forbid"`). Errors are `ConfigError` subclasses with a `code`.
  The local overlay: `load_workflow(path, overlay=True)` also reads the sibling
  `overlay_path_for(path)` names (`WORKFLOW.md` → `WORKFLOW.local.md`, git-ignored, derived
  and never configured so it lives in the mounted directory and reloads like the base) and
  merges its front matter over the base's *before* resolution, so a fallback `resolve_config`
  fills in for an absent field can never clobber the other file's value. `merge_front_matter`
  has three rules: two mappings merge key by key, recursively; anything else replaces, a list
  as a whole; an explicit `null` in the overlay deletes the key so the `Settings` default
  applies. The merged mapping is validated once, so `extra="forbid"` catches a typo in either
  file, and an error about the merge names both (`/configs/WORKFLOW.md (+ WORKFLOW.local.md):
  ...`, `ConfigError.overlay`), while a parse error in the overlay carries the overlay's path.
  The overlay's body replaces the prompt only when it is non-empty. `Workflow` carries
  `overlay_path` (`None` without one), `overlay_identity` (the same triple, all zero without
  one) and `overlay_config` (its raw mapping, which `count_overrides` counts for `validate`);
  `raw_config` is the *merged* mapping. A missing overlay is the normal case; one that exists
  and is not a regular file is a `MissingWorkflowFile` naming it. `overlay=False` exists for
  `tests/test_workflow_default.py`, which loads the repository's own `configs/WORKFLOW.md`,
  exactly where a developer working on issuebot keeps their overlay.
- `issuebot.log`:
```

In `CLAUDE.md` (edit 2 of 6), in the `issuebot.orchestrator` bullet, replace

```
  `_reload_workflow` compares `source_identity`, and when it matches asks
  `_pinned_mount_complaint(path, stat)` why this process might not be able to tell (#46):
```

with

```
  `_reload_workflow` compares `source_identity`, and the overlay's `overlay_identity` and
  presence beside it (a `FileNotFoundError` on the overlay is "no overlay"; any other
  `OSError` is a reload failure, since a file that exists and cannot be stat'ed is not
  something to guess about), reloading when either identity moves or the overlay appears or
  disappears, and when nothing has changed asks
  `_pinned_mount_complaint(path, stat)` of the base and then of the overlay why this process
  might not be able to tell (#46):
```

In `CLAUDE.md` (edit 3 of 6), later in the same bullet, replace

```
  defaults to the same), so a lookup goes through the host's directory entry.
  `request_refresh()`, `request_stop()`, `snapshot()`;
```

with

```
  defaults to the same), so a lookup goes through the host's directory entry. The snapshot
  carries `workflow_overlay_path` (`None` without one), which is how `issuebot status`,
  `/api/v1/state` and the dashboard answer "is the worker running my overrides?".
  `request_refresh()`, `request_stop()`, `snapshot()`;
```

In `CLAUDE.md` (edit 4 of 6), in the `issuebot.web` bullet, replace

```
  filter survives the ten-second swap that would collapse an expander or reset a scroll. A snapshot's
  `dispatch_hold` reaches `/api/v1/state` and the dashboard's worker line through
```

with

```
  filter survives the ten-second swap that would collapse an expander or reset a scroll. A snapshot's
  `workflow_overlay_path` reaches `/api/v1/state` through `_WORKER_KEYS` and the worker line
  as a fourth `.fact` chip, drawn only when there is one. A snapshot's
  `dispatch_hold` reaches `/api/v1/state` and the dashboard's worker line through
```

In `CLAUDE.md` (edit 5 of 6), in the `issuebot.cli` bullet, replace

```
- `issuebot.cli`: argparse; `validate` (thirteen checks: three network probes through the
  adapter,
```

with

```
- `issuebot.cli`: argparse; `validate` (thirteen checks: the `workflow` check naming the
  overlay and counting its overrides (`/configs/WORKFLOW.md + WORKFLOW.local.md (3
  overrides)`), three network probes through the
  adapter,
```

In `CLAUDE.md` (edit 6 of 6), in the same bullet, replace

```
  `status` (the snapshot as text, with a `dispatch: held (<kind>) since ...` line while
  dispatch is held),
```

with

```
  `status` (the snapshot as text, its `workflow:` line reading `<base> + <overlay>` when one
  is in force, with a `dispatch: held (<kind>) since ...` line while
  dispatch is held),
```

- [ ] **Step 4: Verify, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files && uv run pytest -q`
Expected: every hook `Passed` (or `Skipped` for `check yaml` when no YAML changed), and `1082 passed, 52 skipped`.

Then, from the repository root: `docker compose --profile test up -d --wait test-db && DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:$(docker compose port test-db 5432 | cut -d: -f2)/issuebot uv run pytest -q; docker compose rm -sf test-db`
Expected: `1134 passed` with no skips, then the container removed. Do not run `docker compose down`.

- [ ] **Step 5: Commit**

```bash
git add .gitignore README.md CLAUDE.md
git commit -m "docs: ignore the overlay and document local overrides"
```

---

### Task 6: Push and open the pull request

**Files:** none.

- [ ] **Step 1: Push the branch**

Run: `git push -u origin issuebot/workflow-local-overrides`

- [ ] **Step 2: Open the PR through the REST API**

Per the global instructions, never `gh pr create` (it aborts on the deprecated `projectCards` field here, and a hook blocks it). Write the body with the Write tool to a scratch file (say `/tmp/claude-1001/.../scratchpad/pr-body.md`), in a separate call from the API one, with this content and the harness's attribution lines at the end:

```
Implements docs/superpowers/specs/2026-09-09-workflow-local-overrides-design.md.

A git-ignored `configs/WORKFLOW.local.md`, found beside the workflow file, whose front
matter is deep-merged over the tracked file's (mappings merge, anything else replaces,
an explicit `null` deletes), so a deployment's settings live in an untracked file and
`git pull` keeps delivering the prompt template with no merge.

- `load_workflow(path, overlay=True)`: derived sibling, raw merge before resolution,
  one validation; errors about the merge name both files
- the orchestrator reloads when either file's identity moves or the overlay appears or
  disappears, and the #46 mount complaint covers both files
- `validate` names the overlay and counts its overrides; the snapshot carries
  `workflow_overlay_path` to `issuebot status`, `/api/v1/state` and the worker line
- `configs/*.local.md` ignored; README and CLAUDE.md updated; no migration, no compose
  change, no example file

Plan: docs/superpowers/plans/2026-09-09-workflow-local-overrides.md
```

Then:

```bash
gh api repos/jleavers/issuebot/pulls -X POST \
  -f title='feat: local overrides for the workflow file' \
  -f head='issuebot/workflow-local-overrides' -f base='main' \
  -F body=@<the scratch file>
```

Expected: a PR URL in the response; `gh pr view --json body --jq '.body'` reads the body back. CI (lint, tests with the postgres service, the Docker build) is green; the operator merges.
