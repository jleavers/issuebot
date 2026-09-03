# Phase 4: Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The long-running worker: poll GitHub, claim `todo` and `rework` issues, run worker sessions concurrently up to a slot limit, retry failures with backoff, escalate exhausted runs to `review` with a workpad note, follow human label moves, turn merged pull requests into `complete`, resume after a restart, and stop cleanly on SIGTERM; `issuebot worker` and a real compose `worker` service.

**Architecture:** A new `issuebot.orchestrator` package in three modules. `state.py` holds the runtime records (`RunningEntry`, `RetryEntry`, `RuntimeSnapshot`) and the pure rules (backoff, candidate sort, transition observation). `actions.py` holds the GitHub-writing actions (claim, blocked escape, terminal finish). `orchestrator.py` is one asyncio task that owns all scheduling state, spawns workers as child tasks calling the frozen `run_session`, and waits on a queue of worker exits with a deadline-driven poll; every timing rule goes through an injectable clock. A first task hardens four Phase 3 internals the orchestrator relies on without changing any signature.

**Tech Stack:** Python 3.14, asyncio, the Phase 2 `GitHubAdapter`/`FakeGitHub`, the Phase 3 `run_session`/`WorkspaceManager`/`ClaudeRunner`, pydantic settings, structlog, pytest + pytest-asyncio (`asyncio_mode = "auto"`), ruff 0.16.5. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md` (parent: `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`; Phase 3: `docs/superpowers/specs/2026-09-03-phase-3-agent-runner-design.md`).

## Global Constraints

Every task's requirements include this section. Every implementer and reviewer dispatch must carry it verbatim.

- Python >=3.14, `uv run` for everything; no new dependency unless the spec names it (it names none: `pyproject.toml` and `uv.lock` do not change in this phase).
- Work on branch `phase-4-orchestrator`; the spec and this plan are its first two commits. Never push to `main`, never merge or close PRs, never `rm -rf`, `git reset --hard` or `git clean -fd`; the SDD workspaces under `.superpowers/sdd/` are left for the operator to delete. Linux host: Bash, `&&` chaining.
- A Bash-level hook on this host blocks any shell command whose text contains the dot-env filename (the literal `.` + `env`, including `.example` and heredoc bodies); such files are written with Write/Edit, staged with `git add --all` after `git status --short`; say "dot-env" in commit messages.
- The ruff-format pre-commit hook (v0.16.5) reflows Python fences inside docs/**/*.md; write fences pre-formatted (double quotes, line length 100, trailing commas) and re-`git add` after `pre-commit run --all-files`.
- ruff rules E F I UP B N SIM RUF, target py314. SIM300 ranks literal > ALL_CAPS name > other expression and flags a comparison whose left side ranks higher, so `MIN_CLAUDE_VERSION == (2, 1, 259)` and `{...} == ACTIVE_STATES`; apply ruff's fix, never suppress, never a per-file ignore. N818: exception classes end in `Error` or carry `# noqa: N818` by ruling (`OrchestratorStartupError` has the suffix). RUF022 sorts `__all__`; RUF006 stores create_task results. The formatter writes `except A, B:` without parentheses (PEP 758). UP037: no quoted annotations (py314 defers them).
- astral-sh/setup-uv is pinned to an exact version (v10.0.1) in CI; no floating major. CI is not touched in this phase.
- Commit messages: conventional prefix plus the attribution trailer the harness requires as the last lines. Before every commit: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`; before every push also `uv run pre-commit run --all-files`.
- Tests hermetic: FakeGitHub, tests/fakes/claude, tests/fakes/gh on tmp_path; hooks in tests through ("bash", "-c"); no real gh, claude or login shell. Tests that spawn the fakes or send signals are `skipif(sys.platform == "win32")`.
- Frozen inputs, used as they are: `run_session`, `RunResult`, `WorkspaceManager`, `ClaudeRunner`, `TurnObserver`, `TurnEvent`, `SessionRecord`, `issuebot.events` (`EVENT_KINDS` unchanged) and the `GitHubAdapter` protocol. Task 1 changes internals of `workspace.py` and `runner.py` only; no signature changes anywhere in Phase 3 code.
- Package rules: `issuebot.orchestrator` imports `agent`, `github`, `config`, `events` and `log` only; nothing imports `orchestrator` except `cli`. `state.py` has no I/O; `actions.py` never takes the orchestrator; `orchestrator.py` is the only place that mutates `running` and `retries`.
- Secrets are never logged: the orchestrator logs settings summaries, never the token or the environment.
- The live-check task runs against jleavers/issuebot-scratch (exists: issue #1 in review, PR #2 open; host dirs ~/issuebot-scratch and ~/issuebot-workspaces) with `export GH_TOKEN=$(gh auth token)` in the same command, spends real Claude budget under the operator's subscription login, and never prints the token. The operator, not the executor, merges the scratch PR that the live check needs merged.

---

## File map

| Path | Responsibility | Task |
|---|---|---|
| `src/issuebot/agent/workspace.py` | reuse needs `.git` and `.issuebot` (created last); `OSError` → `workspace_error` | 1 |
| `src/issuebot/agent/runner.py` | `_terminate` kills the group after the leader exited; pre-set cancel spawns nothing | 1 |
| `tests/fakes/claude` | `orphan` scenario | 1 |
| `tests/test_agent_workspace.py`, `tests/test_agent_runner.py` | hardening tests | 1 |
| `src/issuebot/orchestrator/__init__.py` | package re-exports (completed in Task 6) | 2, 3, 6 |
| `src/issuebot/orchestrator/state.py` | records, snapshot, `backoff_ms`, `sort_candidates`, `claimed_snapshot`, `observe_transition` | 2 |
| `tests/test_orchestrator_state.py` | pure-function tests | 2 |
| `src/issuebot/orchestrator/actions.py` | `claim`, `blocked_block`, `blocked_escape`, `finish_terminal`, `remove_workspace` | 3 |
| `tests/test_orchestrator_actions.py` | actions against `FakeGitHub` | 3 |
| `src/issuebot/orchestrator/orchestrator.py` | `Orchestrator`, `RunObserver`, `preflight`, `OrchestratorStartupError` | 4, 5, 6 |
| `tests/test_orchestrator.py` | harness, the §17.4 matrix, loop tests, one end-to-end run | 4, 5, 6 |
| `src/issuebot/cli.py`, `tests/test_cli.py` | `issuebot worker` | 7 |
| `compose.yaml` | the `worker` service becomes real | 7 |
| `WORKFLOW.md`, `tests/test_workflow_default.py` | `after_create` unshallow hook; attempt wording | 8 |
| `CLAUDE.md`, `README.md`, roadmap, Phase 3 spec | documentation | 9 |
| (scratch repository) | live check | 10 |

---

### Task 1: Harden the Phase 3 internals the orchestrator relies on

**Files:**
- Modify: `src/issuebot/agent/workspace.py` (`create_or_reuse`, `remove`, new `_remove_path`)
- Modify: `src/issuebot/agent/runner.py` (`run_turn` pre-set cancel check, `_terminate`)
- Modify: `tests/fakes/claude` (the `orphan` scenario)
- Test: `tests/test_agent_workspace.py`, `tests/test_agent_runner.py`

**Interfaces:**
- Consumes: the Phase 3 `WorkspaceManager` and `ClaudeRunner` as they are.
- Produces: no signature changes. Behaviour the later tasks depend on: `create_or_reuse` reuses a workspace only when both `path/.git` and `path/.issuebot` are directories and creates `.issuebot` as its last step; `remove()` and the two `mkdir` calls raise `AgentError("workspace_error")` instead of a raw `OSError`; `_terminate` always ends with `os.killpg(..., SIGKILL)`; `run_turn` returns a `cancelled` `TurnResult` without spawning, without creating the log directory and without emitting any event when `cancel.is_set()` right after the workspace preflight.

Spec: §10 (the four rows) and the note that an `after_create` hook writing under `.issuebot/` must now create that directory itself.

- [ ] **Step 1: Write the failing workspace tests**

The existing test `test_create_clones_and_prepares_the_repository` uses an `after_create` hook that writes into `.issuebot/`; that directory no longer exists while the hook runs. Change its hook line (around line 137 of `tests/test_agent_workspace.py`) to:

```python
    hook = "mkdir -p .issuebot && echo created > .issuebot/hook.txt"
```

Add `import shutil` after `import os` in the imports of `tests/test_agent_workspace.py`, then append to the file:

```python
# --- Phase 4 hardening: reuse marker and OSError conversion ---------------------------


@posix
async def test_git_without_issuebot_marker_is_recreated(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, gh = make_manager(tmp_path)
    issue = make_issue(identifier="example-42")
    first = await manager.create_or_reuse(issue)
    shutil.rmtree(first.path / ".issuebot")
    (first.path / "stale").write_text("x")
    second = await manager.create_or_reuse(issue)
    assert second.created
    assert len(gh.calls) == 2
    assert not (second.path / "stale").exists()
    assert (second.path / ".issuebot").is_dir()


@posix
async def test_issuebot_marker_is_created_after_the_after_create_hook(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, _ = make_manager(
        tmp_path, hooks={"after_create": "test ! -e .issuebot && touch hook-ran"}
    )
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert (ws.path / "hook-ran").exists()
    assert (ws.path / ".issuebot").is_dir()


@posix
async def test_marker_creation_failure_is_workspace_error(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, _ = make_manager(tmp_path, hooks={"after_create": "touch .issuebot"})
    with pytest.raises(AgentError) as exc:
        await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert exc.value.category == "workspace_error"
    assert ".issuebot" in exc.value.message
    assert not (manager.root / "example-42").exists()


async def test_root_creation_failure_is_workspace_error(
    tmp_path: Path, make_issue: Callable[..., Issue]
) -> None:
    manager, gh = make_manager(tmp_path)
    manager.root.parent.mkdir(parents=True, exist_ok=True)
    manager.root.write_text("not a directory")
    with pytest.raises(AgentError) as exc:
        await manager.create_or_reuse(make_issue(identifier="example-42"))
    assert exc.value.category == "workspace_error"
    assert "workspace root" in exc.value.message
    assert gh.calls == []


@posix
async def test_remove_failure_is_workspace_error(
    tmp_path: Path, make_issue: Callable[..., Issue], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = make_manager(tmp_path)
    ws = await manager.create_or_reuse(make_issue(identifier="example-42"))

    def refuse(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError(f"{path}: refused")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    with pytest.raises(AgentError) as exc:
        await manager.remove("example-42")
    assert exc.value.category == "workspace_error"
    assert "refused" in exc.value.message
    assert ws.path.is_dir()
```

- [ ] **Step 2: Write the failing runner tests**

Append to `tests/test_agent_runner.py`:

```python
# --- Phase 4 hardening: group kill after the leader exited, pre-set cancel ------------


@posix
async def test_orphaned_grandchild_is_killed_with_the_group(
    workspace: Path, tmp_path: Path
) -> None:
    pidfile = tmp_path / "grandchild.pid"
    runner = runner_for(
        workspace,
        scenario="orphan",
        turn_timeout_ms=500,
        extra_env={"CLAUDE_FAKE_PIDFILE": str(pidfile)},
    )
    recorder = Recorder()
    turn = await run(runner, workspace, observer=recorder)
    grandchild = await wait_for_file(pidfile)
    assert turn.error_category == "turn_timeout"
    assert turn.exit_code == 0
    await assert_gone(grandchild)
    assert recorder.kinds[-2:] == ["process_exit", "turn_timeout"]


@posix
async def test_preset_cancel_spawns_nothing(workspace: Path, tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    runner = runner_for(workspace, extra_env={"CLAUDE_FAKE_RECORD": str(record)})
    cancel = asyncio.Event()
    cancel.set()
    recorder = Recorder()
    log_dir = workspace / ".issuebot" / "runs" / "run-1"
    turn = await run(runner, workspace, observer=recorder, cancel=cancel, log_dir=log_dir)
    assert turn.error_category == "cancelled"
    assert turn.exit_code is None
    assert turn.session_id is None
    assert not record.exists()
    assert not log_dir.exists()
    assert recorder.kinds == []
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_agent_workspace.py tests/test_agent_runner.py -q -k "hardening or issuebot_marker or marker_creation or root_creation or remove_failure or orphaned_grandchild or preset_cancel"`
Expected: `test_git_without_issuebot_marker_is_recreated` fails (`second.created` is False), `test_issuebot_marker_is_created_after_the_after_create_hook` fails (`test ! -e .issuebot` exits 1), `test_marker_creation_failure_is_workspace_error` fails (the hook succeeds and the `mkdir` never runs), `test_root_creation_failure_is_workspace_error` raises `FileExistsError`, `test_remove_failure_is_workspace_error` raises `PermissionError`, `test_orphaned_grandchild_is_killed_with_the_group` fails on `assert_gone` (the `sleep` outlives the turn), `test_preset_cancel_spawns_nothing` fails (`record.json` exists; the fake ran).

- [ ] **Step 4: Apply the workspace and runner changes**

Apply this diff to `src/issuebot/agent/workspace.py`, `src/issuebot/agent/runner.py` and `tests/fakes/claude` (the `tests/fakes/claude` hunk adds `import subprocess` and the `orphan` scenario before the `unknown CLAUDE_FAKE_SCENARIO` line):

```diff
diff --git a/src/issuebot/agent/runner.py b/src/issuebot/agent/runner.py
index bfcf940..d2e7f1c 100644
--- a/src/issuebot/agent/runner.py
+++ b/src/issuebot/agent/runner.py
@@ -377,6 +377,8 @@ class ClaudeRunner:
         if not (resolved.is_dir() and inside):
             message = f"{workspace} is not a directory inside {self._root}"
             return finish("invalid_workspace_cwd", message, None)
+        if cancel is not None and cancel.is_set():
+            return finish("cancelled", "cancelled before the turn started", None)
         log_dir.mkdir(parents=True, exist_ok=True)
         (log_dir / f"turn-{turn_number}.prompt.md").write_text(prompt, encoding="utf-8")
         argv = self.build_argv(session_id=session_id, resume=resume)
@@ -487,17 +489,19 @@ class ClaudeRunner:
                     emit(event)

     async def _terminate(self, process: asyncio.subprocess.Process) -> None:
-        """SIGTERM, wait for the grace period, then SIGKILL the whole process group."""
-        if process.returncode is not None:
-            return
-        with contextlib.suppress(ProcessLookupError):
-            process.terminate()
-        try:
-            await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_S)
-        except TimeoutError:
+        """SIGTERM the leader, wait for the grace period, then SIGKILL the whole group.
+
+        The group is killed even when the leader has already exited: a grandchild that
+        inherited stdout would otherwise outlive the turn and hold the pipe open.
+        """
+        if process.returncode is None:
             with contextlib.suppress(ProcessLookupError):
-                os.killpg(process.pid, signal.SIGKILL)
-            await process.wait()
+                process.terminate()
+            with contextlib.suppress(TimeoutError):
+                await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_S)
+        with contextlib.suppress(ProcessLookupError):
+            os.killpg(process.pid, signal.SIGKILL)
+        await process.wait()


 class _Emitter:
diff --git a/src/issuebot/agent/workspace.py b/src/issuebot/agent/workspace.py
index 3e9b74e..f9f79bf 100644
--- a/src/issuebot/agent/workspace.py
+++ b/src/issuebot/agent/workspace.py
@@ -130,28 +130,33 @@ class WorkspaceManager:

     async def create_or_reuse(self, issue: Issue) -> Workspace:
         path = self.path_for(issue.identifier)
-        if (path / ".git").is_dir():
+        if (path / ".git").is_dir() and (path / ".issuebot").is_dir():
             self._log.debug("workspace_reused", workspace=str(path))
             return Workspace(key=path.name, path=path, created=False)
         if path.exists():
             self._log.warning("workspace_remnant_removed", workspace=str(path))
-            try:
-                if path.is_dir():
-                    shutil.rmtree(path)
-                else:
-                    path.unlink()
-            except OSError as exc:
-                raise AgentError("workspace_error", f"cannot remove remnant {path}: {exc}") from exc
-        self.root.mkdir(parents=True, exist_ok=True)
+            _remove_path(path, "cannot remove remnant")
+        try:
+            self.root.mkdir(parents=True, exist_ok=True)
+        except OSError as exc:
+            raise AgentError(
+                "workspace_error", f"cannot create workspace root {self.root}: {exc}"
+            ) from exc
         try:
             await self._clone(path)
             post = await self._run_script("post_clone", POST_CLONE_SCRIPT, path)
             if not post.ok:
                 raise AgentError("workspace_error", f"post-clone setup failed: {post.summary}")
-            (path / ".issuebot").mkdir(exist_ok=True)
             hook = await self.run_hook("after_create", path)
             if hook is not None and not hook.ok:
                 raise AgentError("workspace_error", f"after_create hook failed: {hook.summary}")
+            # Created last: its presence marks a workspace whose creation completed.
+            try:
+                (path / ".issuebot").mkdir(exist_ok=True)
+            except OSError as exc:
+                raise AgentError(
+                    "workspace_error", f"cannot create {path / '.issuebot'}: {exc}"
+                ) from exc
         except AgentError:
             shutil.rmtree(path, ignore_errors=True)
             raise
@@ -178,7 +183,7 @@ class WorkspaceManager:
         if not path.exists():
             return False
         await self.run_hook("before_remove", path)
-        shutil.rmtree(path)
+        _remove_path(path, "cannot remove workspace")
         self._log.info("workspace_removed", workspace=str(path))
         return True

@@ -323,6 +328,17 @@ def _as_int(value: object) -> int:
     return value


+def _remove_path(path: Path, what: str) -> None:
+    """Delete a directory tree or a plain file; every OSError becomes a workspace_error."""
+    try:
+        if path.is_dir():
+            shutil.rmtree(path)
+        else:
+            path.unlink()
+    except OSError as exc:
+        raise AgentError("workspace_error", f"{what} {path}: {exc}") from exc
+
+
 def _elapsed_ms(started: float) -> int:
     return round((time.monotonic() - started) * 1000)

diff --git a/tests/fakes/claude b/tests/fakes/claude
index f57a0a8..446e60b 100755
--- a/tests/fakes/claude
+++ b/tests/fakes/claude
@@ -11,6 +11,7 @@ prefix because only that prefix passes through the runner's environment filter.
 import json
 import os
 import signal
+import subprocess
 import sys
 import time
 from pathlib import Path
@@ -117,5 +118,12 @@ if scenario in ("silent", "stubborn"):
     emit(lines("success")[0])
     time.sleep(30)
     sys.exit(0)
+if scenario == "orphan":
+    # A grandchild in the same session inherits stdout and outlives the leader.
+    emit(lines("success")[0])
+    grandchild = subprocess.Popen(["sleep", "30"])
+    if pidfile:
+        Path(pidfile).write_text(str(grandchild.pid))
+    sys.exit(0)
 sys.stderr.write(f"unknown CLAUDE_FAKE_SCENARIO {scenario!r}\n")
 sys.exit(3)
```

- [ ] **Step 5: Run the affected suites**

Run: `uv run pytest tests/test_agent_workspace.py tests/test_agent_runner.py tests/test_agent_session.py tests/test_cli.py -q`
Expected: all pass (147 tests in those files). Then `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`: 412 passed.

- [ ] **Step 6: Commit**

```bash
git add src/issuebot/agent/workspace.py src/issuebot/agent/runner.py tests/fakes/claude tests/test_agent_workspace.py tests/test_agent_runner.py
git commit -m "fix: harden workspace reuse, removal errors and claude termination for the orchestrator"
```

(with the attribution trailer the harness requires as the last lines of the message).

---

### Task 2: Runtime records and pure scheduling rules (`state.py`)

**Files:**
- Create: `src/issuebot/orchestrator/__init__.py`, `src/issuebot/orchestrator/state.py`
- Test: `tests/test_orchestrator_state.py`

**Interfaces:**
- Consumes: `issuebot.agent.RunResult`, `issuebot.config.GitHubLabels`, `issuebot.events.{Event, PrOpened, StateChanged}`, `issuebot.github.{Issue, StateLabel}`.
- Produces (used by Tasks 3 to 6): `RetryKind`, `StopCause`, the constants `CONTINUATION_DELAY_MS = 1_000`, `BACKOFF_BASE_MS = 10_000`, `TERMINAL_SWEEP_EVERY_TICKS = 10`, `REVIEW_GRACE_TICKS = 1`; `backoff_ms(attempt, max_backoff_ms) -> int`; `sort_candidates(issues) -> list[Issue]`; `state_label_name(issue) -> str | None`; `pr_url(issue) -> str | None`; `claimed_snapshot(issue, labels) -> Issue`; `observe_transition(previous, current) -> list[Event]`; the dataclasses `RunningEntry` (mutable, `stop(cause, detail)`, `issue_id`, `identifier`), `BlockedContext`, `RetryEntry`, `ClaudeTotals` (`add(result)`, `total_tokens`), `Counters` (`bump(**deltas)`), `RunningRow.from_entry`, `RetryRow.from_entry`, `RuntimeSnapshot` (`to_dict()`).

Spec: §4 (all of it).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_orchestrator_state.py`:

```python
"""Tests for the orchestrator's pure records and rules."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from issuebot.agent import RunResult
from issuebot.config import GitHubLabels
from issuebot.events import PrOpened, StateChanged
from issuebot.github import Issue, LinkedPr, StateLabel
from issuebot.orchestrator.state import (
    BlockedContext,
    ClaudeTotals,
    Counters,
    RetryEntry,
    RetryRow,
    RunningEntry,
    RunningRow,
    RuntimeSnapshot,
    backoff_ms,
    claimed_snapshot,
    observe_transition,
    sort_candidates,
    state_label_name,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def make_result(**overrides: object) -> RunResult:
    fields: dict[str, object] = {
        "run_id": "run-1",
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "attempt": 1,
        "session_id": "sess",
        "outcome": "succeeded",
        "stop_reason": "issue_moved",
        "error_category": None,
        "error": None,
        "turns": 2,
        "input_tokens": 100,
        "output_tokens": 10,
        "cost_usd": 0.5,
        "duration_s": 12.5,
        "final_state": StateLabel.REVIEW,
        "final_issue": None,
        "workspace_path": Path("/workspaces/repo-42"),
        "log_dir": Path("/workspaces/repo-42/.issuebot/runs/run-1"),
    }
    fields.update(overrides)
    return RunResult(**fields)  # type: ignore[arg-type]


# --- rules ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "cap", "expected"),
    [(1, 300_000, 10_000), (2, 300_000, 20_000), (3, 300_000, 40_000), (6, 300_000, 300_000)],
)
def test_backoff_doubles_from_ten_seconds_and_caps(attempt: int, cap: int, expected: int) -> None:
    assert backoff_ms(attempt, cap) == expected


def test_backoff_cap_is_the_configured_maximum() -> None:
    assert backoff_ms(2, 15_000) == 15_000
    assert backoff_ms(0, 300_000) == 10_000


def test_sort_candidates_ranks_orphans_then_rework_then_todo(
    make_issue: Callable[..., Issue],
) -> None:
    old = datetime(2026, 9, 1, tzinfo=UTC)
    new = datetime(2026, 9, 2, tzinfo=UTC)
    todo_old = make_issue(id="1", number=1, state=StateLabel.TODO, created_at=old)
    todo_new = make_issue(id="2", number=2, state=StateLabel.TODO, created_at=new)
    rework = make_issue(id="3", number=3, state=StateLabel.REWORK, created_at=new)
    orphan = make_issue(id="4", number=4, state=StateLabel.IN_PROGRESS, created_at=new)
    tie = make_issue(id="5", number=5, state=StateLabel.TODO, created_at=old)
    ordered = sort_candidates([tie, todo_new, rework, todo_old, orphan])
    assert [issue.number for issue in ordered] == [4, 3, 1, 5, 2]


def test_state_label_name_is_the_raw_label_or_none(make_issue: Callable[..., Issue]) -> None:
    assert state_label_name(make_issue(state_labels=("Issuebot/Todo",))) == "Issuebot/Todo"
    assert state_label_name(make_issue(state=None, state_labels=())) is None


def test_claimed_snapshot_replaces_only_the_state_labels(
    make_issue: Callable[..., Issue],
) -> None:
    issue = make_issue(labels=("bug", "issuebot/todo"), state_labels=("issuebot/todo",))
    claimed = claimed_snapshot(issue, GitHubLabels())
    assert claimed.state is StateLabel.IN_PROGRESS
    assert claimed.state_labels == ("issuebot/in-progress",)
    assert claimed.labels == ("bug", "issuebot/in-progress")
    assert claimed.dispatchable is True
    assert (claimed.number, claimed.title, claimed.url) == (issue.number, issue.title, issue.url)


def test_claimed_snapshot_uses_configured_names(make_issue: Callable[..., Issue]) -> None:
    labels = GitHubLabels(in_progress="Bot/Working", todo="bot/queue")
    issue = make_issue(labels=("bot/queue",), state_labels=("bot/queue",))
    assert claimed_snapshot(issue, labels).labels == ("bot/working",)


def test_observe_transition_agent_review(make_issue: Callable[..., Issue]) -> None:
    url = "https://github.com/example/repo/pull/7"
    pr = LinkedPr(number=7, url=url, state="open", merged_at=None)
    before = make_issue(state=StateLabel.IN_PROGRESS, state_labels=("issuebot/in-progress",))
    after = make_issue(state=StateLabel.REVIEW, state_labels=("issuebot/review",), linked_pr=pr)
    events = observe_transition(before, after)
    assert [type(event) for event in events] == [StateChanged, PrOpened]
    changed = events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label, changed.actor) == (
        "issuebot/in-progress",
        "issuebot/review",
        "agent",
    )
    assert changed.pr_url == pr.url
    opened = events[1]
    assert isinstance(opened, PrOpened)
    assert (opened.pr_number, opened.pr_url) == (7, pr.url)


@pytest.mark.parametrize(
    ("before_state", "before_label", "after_state", "after_label"),
    [
        (StateLabel.IN_PROGRESS, "issuebot/in-progress", StateLabel.TODO, "issuebot/todo"),
        (StateLabel.REVIEW, "issuebot/review", StateLabel.REWORK, "issuebot/rework"),
        (StateLabel.IN_PROGRESS, "issuebot/in-progress", None, None),
    ],
)
def test_observe_transition_other_moves_are_human(
    make_issue: Callable[..., Issue],
    before_state: StateLabel,
    before_label: str,
    after_state: StateLabel | None,
    after_label: str | None,
) -> None:
    before = make_issue(state=before_state, state_labels=(before_label,))
    after = make_issue(
        state=after_state, state_labels=(after_label,) if after_label else (), dispatchable=False
    )
    events = observe_transition(before, after)
    assert len(events) == 1
    changed = events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label, changed.actor) == (
        before_label,
        after_label,
        "human",
    )


def test_observe_transition_no_change_is_empty(make_issue: Callable[..., Issue]) -> None:
    issue = make_issue()
    assert observe_transition(issue, issue) == []


# --- records --------------------------------------------------------------------------


def test_running_entry_stop_keeps_the_first_cause(make_issue: Callable[..., Issue]) -> None:
    entry = RunningEntry(
        issue=make_issue(),
        attempt=1,
        rework=False,
        resumed=False,
        run_id="run-1",
        started_mono=0.0,
        started_at=NOW,
        cancel=asyncio.Event(),
    )
    assert (entry.issue_id, entry.identifier) == ("42", "repo-42")
    entry.stop("stalled", "no activity for 301 s")
    entry.stop("shutdown", "worker stopping")
    assert (entry.stop_cause, entry.stop_detail) == ("stalled", "no activity for 301 s")
    assert entry.cancel.is_set()


def test_totals_add_and_counters_bump() -> None:
    totals = ClaudeTotals().add(make_result()).add(make_result(input_tokens=1, cost_usd=0.25))
    assert (totals.input_tokens, totals.output_tokens) == (101, 20)
    assert totals.total_tokens == 121
    assert totals.cost_usd == 0.75
    assert totals.seconds_running == 25.0
    counters = Counters().bump(runs_started=1).bump(runs_started=1, blocked=1)
    assert (counters.runs_started, counters.blocked, counters.runs_ended) == (2, 1, 0)


def test_snapshot_rows_and_to_dict(make_issue: Callable[..., Issue]) -> None:
    entry = RunningEntry(
        issue=make_issue(state=StateLabel.IN_PROGRESS),
        attempt=2,
        rework=True,
        resumed=False,
        run_id="run-1",
        started_mono=10.0,
        started_at=NOW,
        cancel=asyncio.Event(),
    )
    entry.session_id = "sess"
    entry.last_event = "turn_activity:Read"
    entry.turns = 1
    retry = RetryEntry(
        issue_id="7",
        identifier="repo-7",
        issue_number=7,
        issue_url="https://github.com/example/repo/issues/7",
        attempt=2,
        kind="failure",
        due_mono=30.0,
        due_at=NOW,
        error="process_exit: boom",
        escape=BlockedContext(reason="r", run_id="run-0", attempt=1, turns=1, log_dir=None),
    )
    snapshot = RuntimeSnapshot(
        at=NOW,
        workflow_path="/app/WORKFLOW.md",
        workflow_mtime_ns=5,
        config_valid=True,
        config_error=None,
        poll_interval_ms=30_000,
        max_concurrent_agents=2,
        tick_count=3,
        last_tick_at=NOW,
        running=(RunningRow.from_entry(entry),),
        retrying=(RetryRow.from_entry(retry),),
        totals=ClaudeTotals(input_tokens=5, output_tokens=6, cost_usd=0.1, seconds_running=2.0),
        counters=Counters(runs_started=1),
    )
    data = snapshot.to_dict()
    assert json.dumps(data)
    row = data["running"][0]
    assert row["state"] == "in_progress"
    assert (row["attempt"], row["rework"], row["session_id"], row["turns"]) == (2, True, "sess", 1)
    assert row["started_at"] == NOW.isoformat()
    assert row["last_activity_at"] is None
    assert data["retrying"][0]["kind"] == "failure"
    assert data["retrying"][0]["due_at"] == NOW.isoformat()
    assert data["totals"] == {
        "input_tokens": 5,
        "output_tokens": 6,
        "cost_usd": 0.1,
        "seconds_running": 2.0,
        "total_tokens": 11,
    }
    assert data["counters"]["runs_started"] == 1
    assert data["at"] == NOW.isoformat()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_orchestrator_state.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'issuebot.orchestrator'`.

- [ ] **Step 3: Create the package and the state module**

Create `src/issuebot/orchestrator/__init__.py` (Task 6 completes the re-exports):

```python
"""Coordination: the poll loop, claims, dispatch, retries, reconciliation and recovery."""

from issuebot.orchestrator.state import (
    BACKOFF_BASE_MS,
    CONTINUATION_DELAY_MS,
    REVIEW_GRACE_TICKS,
    TERMINAL_SWEEP_EVERY_TICKS,
    BlockedContext,
    ClaudeTotals,
    Counters,
    RetryEntry,
    RetryKind,
    RetryRow,
    RunningEntry,
    RunningRow,
    RuntimeSnapshot,
    StopCause,
    backoff_ms,
    claimed_snapshot,
    observe_transition,
    pr_url,
    sort_candidates,
    state_label_name,
)

__all__ = [
    "BACKOFF_BASE_MS",
    "CONTINUATION_DELAY_MS",
    "REVIEW_GRACE_TICKS",
    "TERMINAL_SWEEP_EVERY_TICKS",
    "BlockedContext",
    "ClaudeTotals",
    "Counters",
    "RetryEntry",
    "RetryKind",
    "RetryRow",
    "RunningEntry",
    "RunningRow",
    "RuntimeSnapshot",
    "StopCause",
    "backoff_ms",
    "claimed_snapshot",
    "observe_transition",
    "pr_url",
    "sort_candidates",
    "state_label_name",
]
```

Create `src/issuebot/orchestrator/state.py`:

```python
"""Runtime records and pure scheduling rules for the orchestrator. No I/O."""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from issuebot.agent import RunResult
from issuebot.config import GitHubLabels
from issuebot.events import Event, PrOpened, StateChanged
from issuebot.github import Issue, StateLabel

RetryKind = Literal["continuation", "failure", "escape", "slots"]
StopCause = Literal["stalled", "moved", "closed", "missing", "shutdown"]

CONTINUATION_DELAY_MS = 1_000
BACKOFF_BASE_MS = 10_000
TERMINAL_SWEEP_EVERY_TICKS = 10
REVIEW_GRACE_TICKS = 1

_RANK: dict[StateLabel | None, int] = {
    StateLabel.IN_PROGRESS: 0,
    StateLabel.REWORK: 1,
    StateLabel.TODO: 2,
}


def backoff_ms(attempt: int, max_backoff_ms: int) -> int:
    """``min(10000 * 2 ** (attempt - 1), max_backoff_ms)``; ``attempt`` is the one about to run."""
    exponent = max(attempt - 1, 0)
    return min(BACKOFF_BASE_MS * 2**exponent, max_backoff_ms)


def sort_candidates(issues: Iterable[Issue]) -> list[Issue]:
    """Orphaned in_progress first, then rework, then todo; oldest created_at, then number."""

    def key(issue: Issue) -> tuple[int, datetime, int]:
        return (_RANK.get(issue.state, len(_RANK)), issue.created_at, issue.number)

    return sorted(issues, key=key)


def state_label_name(issue: Issue) -> str | None:
    """The raw name of the issue's state label, for StateChanged.from_label/to_label."""
    return issue.state_labels[0] if issue.state_labels else None


def pr_url(issue: Issue) -> str | None:
    return issue.linked_pr.url if issue.linked_pr is not None else None


def claimed_snapshot(issue: Issue, labels: GitHubLabels) -> Issue:
    """The issue as it looks after set_state(IN_PROGRESS): state labels replaced, rest kept."""
    state_names = {name.lower() for name in labels.as_tuple()}
    target = labels.in_progress.lower()
    kept = tuple(name for name in issue.labels if name.lower() not in state_names)
    return replace(
        issue,
        state=StateLabel.IN_PROGRESS,
        state_labels=(target,),
        labels=(*kept, target),
        dispatchable=issue.github_state == "open",
    )


def observe_transition(previous: Issue, current: Issue) -> list[Event]:
    """Events for what changed between two snapshots of one open issue.

    ``in_progress`` to ``review`` is the agent's transition; every other observed move is a
    human's. A linked pull request appearing is ``PrOpened``.
    """
    events: list[Event] = []
    before, after = state_label_name(previous), state_label_name(current)
    if before != after:
        agent_move = previous.state is StateLabel.IN_PROGRESS and current.state is StateLabel.REVIEW
        events.append(
            StateChanged(
                issue_number=current.number,
                issue_identifier=current.identifier,
                from_label=before,
                to_label=after,
                actor="agent" if agent_move else "human",
                pr_url=pr_url(current),
            )
        )
    if previous.linked_pr is None and current.linked_pr is not None:
        events.append(
            PrOpened(
                issue_number=current.number,
                issue_identifier=current.identifier,
                pr_number=current.linked_pr.number,
                pr_url=current.linked_pr.url,
            )
        )
    return events


# --- records --------------------------------------------------------------------------


@dataclass(kw_only=True)
class RunningEntry:
    """One worker task and everything the orchestrator knows about it."""

    issue: Issue
    attempt: int
    rework: bool
    resumed: bool
    run_id: str
    started_mono: float
    started_at: datetime
    cancel: asyncio.Event
    task: asyncio.Task[RunResult] | None = None
    session_id: str | None = None
    last_activity_mono: float | None = None
    last_activity_at: datetime | None = None
    last_event: str | None = None
    turns: int = 0
    stop_cause: StopCause | None = None
    stop_detail: str | None = None
    review_seen_tick: int | None = None
    terminal_issue: Issue | None = None

    @property
    def issue_id(self) -> str:
        return self.issue.id

    @property
    def identifier(self) -> str:
        return self.issue.identifier

    def stop(self, cause: StopCause, detail: str) -> None:
        """Record the first cause only, then set the cancel event."""
        if self.stop_cause is None:
            self.stop_cause = cause
            self.stop_detail = detail
        self.cancel.set()


@dataclass(frozen=True, kw_only=True, slots=True)
class BlockedContext:
    """What the blocked escape writes; also carried by an escape retry."""

    reason: str
    run_id: str
    attempt: int
    turns: int
    log_dir: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class RetryEntry:
    issue_id: str
    identifier: str
    issue_number: int
    issue_url: str
    attempt: int
    kind: RetryKind
    due_mono: float
    due_at: datetime
    error: str | None
    escape: BlockedContext | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class ClaudeTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    seconds_running: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, result: RunResult) -> ClaudeTotals:
        return ClaudeTotals(
            input_tokens=self.input_tokens + result.input_tokens,
            output_tokens=self.output_tokens + result.output_tokens,
            cost_usd=round(self.cost_usd + result.cost_usd, 6),
            seconds_running=round(self.seconds_running + result.duration_s, 3),
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class Counters:
    runs_started: int = 0
    runs_ended: int = 0
    issues_completed: int = 0
    issues_cancelled: int = 0
    blocked: int = 0

    def bump(self, **deltas: int) -> Counters:
        changes = {name: getattr(self, name) + delta for name, delta in deltas.items()}
        return replace(self, **changes)


# --- snapshot -------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class RunningRow:
    issue_number: int
    identifier: str
    title: str
    url: str
    state: str | None
    attempt: int
    rework: bool
    resumed: bool
    run_id: str
    session_id: str | None
    started_at: datetime
    last_activity_at: datetime | None
    last_event: str | None
    turns: int
    stop_cause: StopCause | None

    @classmethod
    def from_entry(cls, entry: RunningEntry) -> RunningRow:
        return cls(
            issue_number=entry.issue.number,
            identifier=entry.identifier,
            title=entry.issue.title,
            url=entry.issue.url,
            state=entry.issue.state.value if entry.issue.state is not None else None,
            attempt=entry.attempt,
            rework=entry.rework,
            resumed=entry.resumed,
            run_id=entry.run_id,
            session_id=entry.session_id,
            started_at=entry.started_at,
            last_activity_at=entry.last_activity_at,
            last_event=entry.last_event,
            turns=entry.turns,
            stop_cause=entry.stop_cause,
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class RetryRow:
    issue_number: int
    identifier: str
    url: str
    attempt: int
    kind: RetryKind
    due_at: datetime
    error: str | None

    @classmethod
    def from_entry(cls, entry: RetryEntry) -> RetryRow:
        return cls(
            issue_number=entry.issue_number,
            identifier=entry.identifier,
            url=entry.issue_url,
            attempt=entry.attempt,
            kind=entry.kind,
            due_at=entry.due_at,
            error=entry.error,
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class RuntimeSnapshot:
    """The worker's runtime state at one instant; the shape of the future ``/api/v1/state``."""

    at: datetime
    workflow_path: str
    workflow_mtime_ns: int
    config_valid: bool
    config_error: str | None
    poll_interval_ms: int
    max_concurrent_agents: int
    tick_count: int
    last_tick_at: datetime | None
    running: tuple[RunningRow, ...]
    retrying: tuple[RetryRow, ...]
    totals: ClaudeTotals
    counters: Counters

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe: datetimes as ISO 8601, enums as values, tuples as lists."""
        data = _jsonable(self)
        data["totals"]["total_tokens"] = self.totals.total_tokens
        return data


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    return value
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator_state.py -q`
Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/orchestrator tests/test_orchestrator_state.py
git commit -m "feat: add the orchestrator's runtime records and scheduling rules"
```

---

### Task 3: GitHub-writing actions (`actions.py`)

**Files:**
- Create: `src/issuebot/orchestrator/actions.py`
- Test: `tests/test_orchestrator_actions.py`

**Interfaces:**
- Consumes: Task 2's `BlockedContext`, `claimed_snapshot`, `pr_url`, `state_label_name`; `issuebot.github.{WORKPAD_MARKER, GitHubAdapter, GitHubError, Issue, StateLabel, classify_closed}`; `issuebot.agent.{AgentError, WorkspaceManager}`; `issuebot.events.{Blocked, EventBus, IssueCancelled, IssueCompleted, StateChanged}`.
- Produces (used by Tasks 4 to 6): `EscapeOutcome = Literal["applied", "skipped", "failed"]`; `FinishOutcome = Literal["complete", "cancelled", "unchanged", "failed"]`; `CANCEL_REASON`; `async claim(adapter, bus, issue) -> Issue | None`; `blocked_block(context, now, labels) -> str`; `async blocked_escape(adapter, bus, issue_id, context, *, now) -> EscapeOutcome`; `async finish_terminal(adapter, bus, workspaces, issue) -> FinishOutcome`; `async remove_workspace(workspaces, identifier) -> bool`.

Spec: §5. Two return types are richer than the spec's prose: `blocked_escape` returns `"applied" | "skipped" | "failed"` (the spec's `True` is `applied` or `skipped`; `False` is `failed`) so the caller counts only real escapes, and `finish_terminal` returns `"unchanged"` for an issue already `complete` and `"failed"` on a `GitHubError` (the spec's `None`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_orchestrator_actions.py`:

```python
"""Tests for the orchestrator's GitHub-writing actions against FakeGitHub."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from issuebot.agent import AgentError, WorkspaceManager
from issuebot.config import Settings
from issuebot.events import (
    Blocked,
    Event,
    EventBus,
    IssueCancelled,
    IssueCompleted,
    StateChanged,
)
from issuebot.github import WORKPAD_MARKER, FakeGitHub, StateLabel
from issuebot.orchestrator.actions import (
    CANCEL_REASON,
    blocked_block,
    blocked_escape,
    claim,
    finish_terminal,
    remove_workspace,
)
from issuebot.orchestrator.state import BlockedContext

NOW = datetime(2026, 9, 3, 14, 2, 11, tzinfo=UTC)
CONTEXT = BlockedContext(
    reason="Turn budget exhausted: 5 turns in attempt 2 without reaching `issuebot/review`.",
    run_id="20260903T135501Z-a1b2c3",
    attempt=2,
    turns=5,
    log_dir="/workspaces/example-42/.issuebot/runs/20260903T135501Z-a1b2c3",
)


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings.model_validate(
            {"github": {"repo": "example/repo"}, "workspace": {"root": str(tmp_path / "ws")}}
        )
        self.github = FakeGitHub(self.settings.github)
        self.recorder = Recorder()
        self.bus = EventBus([self.recorder])
        environ = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
        self.workspaces = WorkspaceManager(
            self.settings, environ=environ, hook_shell=("bash", "-c")
        )

    def workspace_dir(self, identifier: str) -> Path:
        path = self.workspaces.root / identifier
        (path / ".git").mkdir(parents=True)
        (path / ".issuebot").mkdir()
        return path

    def calls(self, name: str) -> list[tuple[object, ...]]:
        return [args for called, args in self.github.calls if called == name]


# --- claim ----------------------------------------------------------------------------


async def test_claim_sets_in_progress_and_publishes(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    issue = h.github.add_issue("Task", labels=("bug", "issuebot/todo"), number=42)
    claimed = await claim(h.github, h.bus, issue)
    assert claimed is not None
    assert claimed.state is StateLabel.IN_PROGRESS
    assert claimed.labels == ("bug", "issuebot/in-progress")
    assert h.github.issue(42).state is StateLabel.IN_PROGRESS
    assert h.calls("set_state") == [(42, StateLabel.IN_PROGRESS)]
    assert h.recorder.kinds == ["state_changed"]
    event = h.recorder.events[0]
    assert isinstance(event, StateChanged)
    assert (event.from_label, event.to_label, event.actor) == (
        "issuebot/todo",
        "issuebot/in-progress",
        "issuebot",
    )


async def test_claim_failure_returns_none_without_events(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    issue = h.github.add_issue("Task", labels=("issuebot/rework",), number=42)
    h.github.fail_next("transport")
    assert await claim(h.github, h.bus, issue) is None
    assert h.github.issue(42).state is StateLabel.REWORK
    assert h.recorder.events == []


# --- blocked escape -------------------------------------------------------------------


def test_blocked_block_content() -> None:
    labels = Settings.model_validate({"github": {"repo": "a/b"}}).github.labels
    text = blocked_block(CONTEXT, NOW, labels)
    assert text.splitlines() == [
        "### Issuebot blocked (2026-09-03T14:02:11Z)",
        "",
        "Turn budget exhausted: 5 turns in attempt 2 without reaching `issuebot/review`.",
        "Run `20260903T135501Z-a1b2c3` (attempt 2, 5 turns); "
        "logs: `/workspaces/example-42/.issuebot/runs/20260903T135501Z-a1b2c3`.",
        "Moved to `issuebot/review` for a human to look at.",
    ]


def test_blocked_block_singular_turn_without_logs() -> None:
    context = BlockedContext(reason="r.", run_id="run-1", attempt=1, turns=1, log_dir=None)
    labels = Settings.model_validate({"github": {"repo": "a/b"}}).github.labels
    assert "Run `run-1` (attempt 1, 1 turn)." in blocked_block(context, NOW, labels)


async def test_escape_appends_to_the_workpad_and_sets_review(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=42)
    h.github.open_pr(42, pr_number=43)
    workpad = await h.github.comment(42, f"{WORKPAD_MARKER}\n\n### Plan\n\n- [ ] 1. Do it\n")
    h.github.calls.clear()
    assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "applied"
    comments = h.github.comments_for(42)
    assert len(comments) == 1
    body = comments[0].body
    assert body.startswith(
        f"{WORKPAD_MARKER}\n\n### Plan\n\n- [ ] 1. Do it\n\n### Issuebot blocked"
    )
    assert body.endswith("Moved to `issuebot/review` for a human to look at.\n")
    assert h.calls("update_comment")[0][0] == workpad.id
    assert h.calls("comment") == []
    assert h.github.issue(42).state is StateLabel.REVIEW
    assert h.recorder.kinds == ["state_changed", "blocked"]
    changed = h.recorder.events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label, changed.actor) == (
        "issuebot/in-progress",
        "issuebot/review",
        "issuebot",
    )
    assert changed.pr_url == "https://github.com/example/repo/pull/43"
    blocked = h.recorder.events[1]
    assert isinstance(blocked, Blocked)
    assert blocked.reason == CONTEXT.reason


async def test_escape_creates_the_workpad_when_missing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=42)
    assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "applied"
    comments = h.github.comments_for(42)
    assert len(comments) == 1
    assert comments[0].body.startswith(f"{WORKPAD_MARKER}\n\n### Issuebot blocked (")
    assert h.calls("update_comment") == []
    assert h.github.issue(42).state is StateLabel.REVIEW


@pytest.mark.parametrize("state", [StateLabel.REVIEW, StateLabel.TODO])
async def test_escape_is_a_no_op_when_the_issue_moved(tmp_path: Path, state: StateLabel) -> None:
    h = Harness(tmp_path)
    label = getattr(h.settings.github.labels, state.value)
    h.github.add_issue("Task", labels=(label,), number=42)
    assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "skipped"
    assert h.github.comments_for(42) == []
    assert h.calls("set_state") == []
    assert h.recorder.events == []


async def test_escape_is_a_no_op_for_closed_or_missing_issues(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=42)
    h.github.close_issue(42)
    assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "skipped"
    assert await blocked_escape(h.github, h.bus, "99", CONTEXT, now=NOW) == "skipped"
    assert h.recorder.events == []


def fail_on(h: Harness, method: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the next call of one adapter method raise, leaving the calls before it alone."""
    original = getattr(h.github, method)

    async def failing(*args: object, **kwargs: object) -> object:
        h.github.fail_next("transport")
        return await original(*args, **kwargs)

    monkeypatch.setattr(h.github, method, failing)


async def test_escape_is_idempotent_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=42)
    await h.github.comment(42, f"{WORKPAD_MARKER}\n\nnotes\n")
    # First run: the workpad is updated but the label write fails.
    with monkeypatch.context() as patch:
        fail_on(h, "set_state", patch)
        assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "failed"
    body_after_first = h.github.comments_for(42)[0].body
    assert body_after_first.count("### Issuebot blocked") == 1
    assert h.github.issue(42).state is StateLabel.IN_PROGRESS
    assert h.recorder.events == []
    # Second run: no second block, label set, events published.
    h.github.calls.clear()
    assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "applied"
    assert h.github.comments_for(42)[0].body == body_after_first
    assert h.calls("update_comment") == []
    assert h.github.issue(42).state is StateLabel.REVIEW
    assert h.recorder.kinds == ["state_changed", "blocked"]


async def test_escape_failure_on_the_workpad_write_leaves_the_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=42)
    fail_on(h, "comment", monkeypatch)
    assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "failed"
    assert h.github.issue(42).state is StateLabel.IN_PROGRESS
    assert h.github.comments_for(42) == []
    assert h.recorder.events == []


# --- finish_terminal ------------------------------------------------------------------


async def test_finish_terminal_completes_a_merged_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/review",), number=42)
    h.github.open_pr(42, pr_number=43)
    h.github.merge_pr(43)
    workspace = h.workspace_dir("repo-42")
    issue = h.github.issue(42)
    assert issue.github_state == "closed"
    assert await finish_terminal(h.github, h.bus, h.workspaces, issue) == "complete"
    assert h.github.issue(42).state is StateLabel.COMPLETE
    assert h.recorder.kinds == ["state_changed", "issue_completed"]
    changed = h.recorder.events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label, changed.actor) == (
        "issuebot/review",
        "issuebot/complete",
        "issuebot",
    )
    completed = h.recorder.events[1]
    assert isinstance(completed, IssueCompleted)
    assert completed.pr_url == "https://github.com/example/repo/pull/43"
    assert not workspace.exists()


async def test_finish_terminal_cancels_an_unmerged_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress", "bug"), number=42)
    h.github.close_issue(42)
    workspace = h.workspace_dir("repo-42")
    assert await finish_terminal(h.github, h.bus, h.workspaces, h.github.issue(42)) == "cancelled"
    assert h.github.issue(42).state is None
    assert h.github.issue(42).labels == ("bug",)
    assert h.recorder.kinds == ["state_changed", "issue_cancelled"]
    changed = h.recorder.events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label) == ("issuebot/in-progress", None)
    cancelled = h.recorder.events[1]
    assert isinstance(cancelled, IssueCancelled)
    assert cancelled.reason == CANCEL_REASON
    assert not workspace.exists()


async def test_finish_terminal_leaves_a_completed_issue_alone(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/complete",), number=42)
    h.github.close_issue(42)
    workspace = h.workspace_dir("repo-42")
    h.github.calls.clear()
    assert await finish_terminal(h.github, h.bus, h.workspaces, h.github.issue(42)) == "unchanged"
    assert h.calls("set_state") == []
    assert h.calls("clear_state") == []
    assert h.recorder.events == []
    assert not workspace.exists()


async def test_finish_terminal_reports_github_failure_and_still_removes(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=42)
    h.github.close_issue(42)
    workspace = h.workspace_dir("repo-42")
    issue = h.github.issue(42)
    h.github.fail_next("transport")
    assert await finish_terminal(h.github, h.bus, h.workspaces, issue) == "failed"
    assert h.github.issue(42).state is StateLabel.IN_PROGRESS
    assert h.recorder.events == []
    assert not workspace.exists()


async def test_remove_workspace_contains_agent_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    assert await remove_workspace(h.workspaces, "repo-42") is False
    h.workspace_dir("repo-42")
    assert await remove_workspace(h.workspaces, "repo-42") is True

    async def refuse(identifier: str) -> bool:
        raise AgentError("workspace_error", f"cannot remove {identifier}")

    monkeypatch.setattr(h.workspaces, "remove", refuse)
    assert await remove_workspace(h.workspaces, "repo-42") is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_orchestrator_actions.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'issuebot.orchestrator.actions'`.

- [ ] **Step 3: Create the actions module**

Create `src/issuebot/orchestrator/actions.py`:

```python
"""GitHub-writing actions the orchestrator takes: claim, blocked escape, terminal finish."""

from datetime import UTC, datetime
from typing import Literal

from issuebot.agent import AgentError, WorkspaceManager
from issuebot.config import GitHubLabels
from issuebot.events import Blocked, EventBus, IssueCancelled, IssueCompleted, StateChanged
from issuebot.github import (
    WORKPAD_MARKER,
    GitHubAdapter,
    GitHubError,
    Issue,
    StateLabel,
    classify_closed,
)
from issuebot.log import get_logger
from issuebot.orchestrator.state import (
    BlockedContext,
    claimed_snapshot,
    pr_url,
    state_label_name,
)

EscapeOutcome = Literal["applied", "skipped", "failed"]
FinishOutcome = Literal["complete", "cancelled", "unchanged", "failed"]

CANCEL_REASON = "closed without a merged pull request"


async def claim(adapter: GitHubAdapter, bus: EventBus, issue: Issue) -> Issue | None:
    """Set ``in_progress`` and publish the claim; ``None`` (logged) when GitHub refuses."""
    log = get_logger(__name__)
    try:
        await adapter.set_state(issue.number, StateLabel.IN_PROGRESS)
    except GitHubError as exc:
        log.warning(
            "dispatch_claim_failed",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            error=str(exc),
        )
        return None
    bus.publish(
        StateChanged(
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            from_label=state_label_name(issue),
            to_label=adapter.labels.in_progress,
            actor="issuebot",
            pr_url=pr_url(issue),
        )
    )
    return claimed_snapshot(issue, adapter.labels)


def blocked_block(context: BlockedContext, now: datetime, labels: GitHubLabels) -> str:
    """The Markdown block the escape appends to the workpad."""
    stamp = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    turns = "turn" if context.turns == 1 else "turns"
    logs = f"; logs: `{context.log_dir}`" if context.log_dir else ""
    return (
        f"### Issuebot blocked ({stamp})\n\n"
        f"{context.reason}\n"
        f"{_run_marker(context)} (attempt {context.attempt}, {context.turns} {turns}){logs}.\n"
        f"Moved to `{labels.review}` for a human to look at."
    )


def _run_marker(context: BlockedContext) -> str:
    return f"Run `{context.run_id}`"


async def blocked_escape(
    adapter: GitHubAdapter,
    bus: EventBus,
    issue_id: str,
    context: BlockedContext,
    *,
    now: datetime,
) -> EscapeOutcome:
    """Roadmap §1's blocked escape: workpad block, then ``review``; retried by the caller."""
    log = get_logger(__name__)
    try:
        issues = await adapter.fetch_issues_by_ids([issue_id])
        if not issues:
            log.info("blocked_escape_skipped", issue_id=issue_id, reason="issue missing")
            return "skipped"
        issue = issues[0]
        if issue.github_state == "closed" or issue.state is not StateLabel.IN_PROGRESS:
            state = "closed" if issue.github_state == "closed" else (issue.state or "unlabelled")
            log.info(
                "blocked_escape_skipped",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                reason=f"issue is {state}",
            )
            return "skipped"
        block = blocked_block(context, now, adapter.labels)
        workpad = await adapter.find_workpad_comment(issue.number)
        if workpad is None:
            await adapter.comment(issue.number, f"{WORKPAD_MARKER}\n\n{block}\n")
        elif _run_marker(context) not in workpad.body:
            body = workpad.body.rstrip("\n") + "\n\n" + block + "\n"
            await adapter.update_comment(workpad.id, body)
        await adapter.set_state(issue.number, StateLabel.REVIEW)
    except GitHubError as exc:
        log.warning(
            "blocked_escape_failed", issue_id=issue_id, run_id=context.run_id, error=str(exc)
        )
        return "failed"
    bus.publish(
        StateChanged(
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            from_label=state_label_name(issue),
            to_label=adapter.labels.review,
            actor="issuebot",
            pr_url=pr_url(issue),
        )
    )
    bus.publish(
        Blocked(issue_number=issue.number, issue_identifier=issue.identifier, reason=context.reason)
    )
    log.info(
        "blocked_escape_applied",
        issue_number=issue.number,
        issue_identifier=issue.identifier,
        run_id=context.run_id,
        reason=context.reason,
    )
    return "applied"


async def finish_terminal(
    adapter: GitHubAdapter,
    bus: EventBus,
    workspaces: WorkspaceManager,
    issue: Issue,
) -> FinishOutcome:
    """A closed issue: ``complete`` or cancelled, the events, then the workspace removed."""
    log = get_logger(__name__)
    outcome: FinishOutcome
    if issue.state is StateLabel.COMPLETE:
        outcome = "unchanged"
    else:
        outcome = classify_closed(issue)
        try:
            if outcome == "complete":
                await adapter.set_state(issue.number, StateLabel.COMPLETE)
                bus.publish(
                    StateChanged(
                        issue_number=issue.number,
                        issue_identifier=issue.identifier,
                        from_label=state_label_name(issue),
                        to_label=adapter.labels.complete,
                        actor="issuebot",
                        pr_url=pr_url(issue),
                    )
                )
                bus.publish(
                    IssueCompleted(
                        issue_number=issue.number,
                        issue_identifier=issue.identifier,
                        pr_url=pr_url(issue),
                    )
                )
            else:
                await adapter.clear_state(issue.number)
                bus.publish(
                    StateChanged(
                        issue_number=issue.number,
                        issue_identifier=issue.identifier,
                        from_label=state_label_name(issue),
                        to_label=None,
                        actor="issuebot",
                        pr_url=pr_url(issue),
                    )
                )
                bus.publish(
                    IssueCancelled(
                        issue_number=issue.number,
                        issue_identifier=issue.identifier,
                        reason=CANCEL_REASON,
                    )
                )
        except GitHubError as exc:
            log.warning(
                "issue_finish_failed",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                outcome=outcome,
                error=str(exc),
            )
            outcome = "failed"
        else:
            log.info(
                "issue_finished",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                outcome=outcome,
                pr_url=pr_url(issue),
            )
    await remove_workspace(workspaces, issue.identifier)
    return outcome


async def remove_workspace(workspaces: WorkspaceManager, identifier: str) -> bool:
    """``workspaces.remove`` with the AgentError contained and logged."""
    try:
        return await workspaces.remove(identifier)
    except AgentError as exc:
        get_logger(__name__).warning(
            "workspace_remove_failed", issue_identifier=identifier, error=exc.message
        )
        return False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator_actions.py -q`
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/orchestrator/actions.py tests/test_orchestrator_actions.py
git commit -m "feat: add the orchestrator's GitHub actions: claim, blocked escape, terminal finish"
```

---

### Task 4: Orchestrator, part one: construction, startup, tick, dispatch, reconcile, snapshot

**Files:**
- Create: `src/issuebot/orchestrator/orchestrator.py`
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: Tasks 2 and 3; `issuebot.agent.{AgentError, ClaudeRunner, RunResult, TurnEvent, TurnRunner, WorkspaceManager, new_run_id, run_session}`; `issuebot.agent.runner.TERMINATE_GRACE_S`; `issuebot.config.{ConfigError, GitHubSettings, Settings, Workflow, load_workflow}`; `issuebot.github.{ACTIVE_STATES, GhCliAdapter, GitHubAdapter, GitHubError, Issue, StateLabel}`.
- Produces: `OrchestratorStartupError(problems: list[str])`; `preflight(settings, *, which) -> list[str]`; `RunObserver(entry, *, clock, now)` satisfying `TurnObserver`; `RunSessionFn`; `CANDIDATE_STATES`; `SHUTDOWN_MARGIN_S = 10.0`; the class `Orchestrator(workflow, *, bus, adapter_factory=GhCliAdapter, workspaces_factory=WorkspaceManager, runner_factory=ClaudeRunner, run_session=run_session, which=shutil.which, clock=time.monotonic, now=_utcnow, environ=None, on_snapshot=None)` with the properties `workflow`, `running`, `retries`, `stopping`, the method `snapshot()`, and the coroutines `startup()`, `tick()`, `reconcile()`, `terminal_sweep()`. Internal names Task 5 and Task 6 build on: `_queue` (an `asyncio.Queue` of `_WorkerExited`, `_REFRESH`, `_STOP`), `_running`, `_retries`, `_totals`, `_counters`, `_tick_count`, `_slots()`, `_dispatch(issue, *, attempt, resume_session_id)`, `_finish(issue)`, `_stop_entry(entry, cause, detail)`, `_publish_snapshot()`.

Spec: §6 up to and including §6.6 (§6.1 observer, §6.2 startup, §6.3 tick, §6.4 dispatch, §6.5 reconcile, §6.6 terminal sweep) and §6.10 snapshot. Worker exits, retries, the loop and shutdown are Tasks 5 and 6; at the end of this task a worker that finishes posts `_WorkerExited` to the queue and nothing consumes it yet.

- [ ] **Step 1: Write the failing tests (harness, preflight, observer, startup, dispatch)**

Create `tests/test_orchestrator.py` with the harness and the sections that need only this task's code (preflight, observer and startup; dispatch; terminal sweep; reload and preflight). The harness helpers `exit`, `drain`, `fire` and `retry` are used from Task 5 on; they are part of the harness now so later tasks only insert test sections and a few imports.

```python
"""Tests for the orchestrator against FakeGitHub, a scripted run_session and a fake clock."""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from issuebot.agent import ClaudeRunner, RunResult, SessionRecord, TurnEvent, WorkspaceManager
from issuebot.agent.session import run_session
from issuebot.config import Settings, load_workflow
from issuebot.events import (
    Event,
    EventBus,
    IssueCompleted,
)
from issuebot.github import FakeGitHub, GhResult, Issue, StateLabel
from issuebot.orchestrator import orchestrator as orchestrator_module
from issuebot.orchestrator.orchestrator import (
    Orchestrator,
    OrchestratorStartupError,
    RunObserver,
    preflight,
)
from issuebot.orchestrator.state import RunningEntry

START = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
START_MONO = 1000.0
FAKE_CLAUDE = Path(__file__).parent / "fakes" / "claude"
posix = pytest.mark.skipif(sys.platform == "win32", reason="the fakes are POSIX scripts")

WORKFLOW_TEMPLATE = """---
github:
  repo: example/repo
polling:
  interval_ms: {interval_ms}
workspace:
  root: {root}
agent:
  max_concurrent_agents: {max_concurrent}
  max_turns: {max_turns}
  max_attempts: {max_attempts}
  max_retry_backoff_ms: {max_retry_backoff_ms}
claude:
  command: {claude}
  turn_timeout_ms: 30000
  stall_timeout_ms: {stall_timeout_ms}
hooks:
  timeout_ms: 5000
{hooks}---
{prompt}
"""


class FakeClock:
    def __init__(self, start: float = START_MONO) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]

    def of(self, kind: type[Event]) -> list[Any]:
        return [event for event in self.events if isinstance(event, kind)]


class StubGh:
    async def run(self, args: list[str], *, stdin: str | None = None) -> GhResult:
        if list(args)[:2] == ["repo", "clone"]:
            subprocess.run(["git", "init", "-q", list(args)[3]], check=True)
        return GhResult(returncode=0, stdout="", stderr="")


class PendingRun:
    """One scripted worker session: the test decides when and how it ends."""

    def __init__(self, issue: Issue, workflow: Any, kwargs: dict[str, Any]) -> None:
        self.issue = issue
        self.workflow = workflow
        self.kwargs = kwargs
        self.future: asyncio.Future[RunResult] = asyncio.get_running_loop().create_future()

    @property
    def cancel(self) -> asyncio.Event:
        return self.kwargs["cancel"]

    @property
    def observer(self) -> RunObserver:
        return self.kwargs["observer"]

    def result(self, **overrides: Any) -> RunResult:
        fields: dict[str, Any] = {
            "run_id": self.kwargs["run_id"],
            "issue_number": self.issue.number,
            "issue_identifier": self.issue.identifier,
            "attempt": self.kwargs["attempt"],
            "session_id": self.kwargs.get("resume_session_id") or "sess",
            "outcome": "succeeded",
            "stop_reason": "issue_moved",
            "error_category": None,
            "error": None,
            "turns": 1,
            "input_tokens": 100,
            "output_tokens": 10,
            "cost_usd": 0.5,
            "duration_s": 12.0,
            "final_state": StateLabel.REVIEW,
            "final_issue": None,
            "workspace_path": Path("/workspaces") / self.issue.identifier,
            "log_dir": Path("/workspaces") / self.issue.identifier / ".issuebot/runs/run",
        }
        fields.update(overrides)
        return RunResult(**fields)

    def finish(self, **overrides: Any) -> None:
        self.future.set_result(self.result(**overrides))

    def fail(self, exc: BaseException) -> None:
        self.future.set_exception(exc)


class ScriptedSessions:
    """A run_session substitute: every call registers a PendingRun the test completes."""

    def __init__(self) -> None:
        self.runs: list[PendingRun] = []

    async def __call__(
        self, issue: Issue, workflow: Any, adapter: Any, bus: Any, **kwargs: Any
    ) -> RunResult:
        run = PendingRun(issue, workflow, kwargs)
        self.runs.append(run)
        waiter = asyncio.create_task(run.cancel.wait())
        try:
            await asyncio.wait({run.future, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not waiter.done():
                waiter.cancel()
        if run.future.done():
            return run.future.result()
        return run.result(
            outcome="cancelled",
            stop_reason="cancelled",
            error_category="cancelled",
            error="cancelled",
            turns=0,
            final_state=issue.state,
            final_issue=None,
        )


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        max_concurrent: int = 2,
        max_attempts: int = 3,
        max_turns: int = 3,
        stall_timeout_ms: int = 300_000,
        interval_ms: int = 30_000,
        max_retry_backoff_ms: int = 300_000,
        hooks: dict[str, str] | None = None,
        prompt: str = "Task {{ issue.identifier }}",
        claude: str = "claude",
        real_sessions: bool = False,
    ) -> None:
        self.tmp_path = tmp_path
        self.path = tmp_path / "WORKFLOW.md"
        self.root = tmp_path / "workspaces"
        self.claude = claude
        self.environ = {
            "GH_TOKEN": "fake-token",
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        self.write_workflow(
            max_concurrent=max_concurrent,
            max_attempts=max_attempts,
            max_turns=max_turns,
            stall_timeout_ms=stall_timeout_ms,
            interval_ms=interval_ms,
            max_retry_backoff_ms=max_retry_backoff_ms,
            hooks=hooks,
            prompt=prompt,
        )
        self.workflow = load_workflow(self.path, environ=self.environ)
        self.clock = FakeClock()
        self.github = FakeGitHub(self.workflow.config.github, now=self.now)
        self.sessions = ScriptedSessions()
        self.recorder = Recorder()
        self.bus = EventBus([self.recorder])
        self.snapshots: list[Any] = []
        self.which_missing: set[str] = set()
        self.orchestrator = Orchestrator(
            self.workflow,
            bus=self.bus,
            adapter_factory=lambda _settings: self.github,
            workspaces_factory=self.make_workspaces,
            runner_factory=lambda settings: ClaudeRunner(settings, environ=self.environ),
            run_session=run_session if real_sessions else self.sessions,
            which=self.which,
            clock=self.clock,
            now=self.now,
            environ=self.environ,
            on_snapshot=self.snapshots.append,
        )

    # --- construction helpers ---------------------------------------------------------

    def now(self) -> datetime:
        return START + timedelta(seconds=self.clock.value - START_MONO)

    def which(self, name: str) -> str | None:
        return None if name in self.which_missing else f"/usr/bin/{name}"

    def make_workspaces(self, settings: Settings) -> WorkspaceManager:
        return WorkspaceManager(
            settings, gh=StubGh(), environ=self.environ, hook_shell=("bash", "-c")
        )

    def write_workflow(
        self,
        *,
        max_concurrent: int = 2,
        max_attempts: int = 3,
        max_turns: int = 3,
        stall_timeout_ms: int = 300_000,
        interval_ms: int = 30_000,
        max_retry_backoff_ms: int = 300_000,
        hooks: dict[str, str] | None = None,
        prompt: str = "Task {{ issue.identifier }}",
        text: str | None = None,
    ) -> None:
        hook_lines = "".join(f"  {name}: {script}\n" for name, script in (hooks or {}).items())
        content = text or WORKFLOW_TEMPLATE.format(
            interval_ms=interval_ms,
            root=self.root,
            max_concurrent=max_concurrent,
            max_turns=max_turns,
            max_attempts=max_attempts,
            max_retry_backoff_ms=max_retry_backoff_ms,
            claude=self.claude,
            stall_timeout_ms=stall_timeout_ms,
            hooks=hook_lines,
            prompt=prompt,
        )
        self.path.write_text(content, encoding="utf-8")
        # Force a distinct mtime so a rewrite within the same tick is noticed.
        previous = getattr(self, "_mtime", 1_700_000_000)
        self._mtime = previous + 1
        os.utime(self.path, ns=(self._mtime * 1_000_000_000, self._mtime * 1_000_000_000))

    @property
    def labels(self) -> Any:
        return self.workflow.config.github.labels

    def add_issue(self, number: int, state: str = "todo", *, title: str | None = None) -> Issue:
        label = getattr(self.labels, state)
        return self.github.add_issue(title or f"Issue {number}", labels=(label,), number=number)

    def workspace_dir(self, identifier: str) -> Path:
        path = self.root / identifier
        (path / ".git").mkdir(parents=True, exist_ok=True)
        (path / ".issuebot").mkdir(exist_ok=True)
        return path

    def write_session(
        self,
        identifier: str,
        *,
        issue_number: int,
        attempt: int = 1,
        session_id: str = "sess-1",
        last_outcome: str | None = None,
    ) -> None:
        path = self.workspace_dir(identifier)
        self.make_workspaces(self.workflow.config).write_session(
            path,
            SessionRecord(
                issue_number=issue_number,
                issue_identifier=identifier,
                run_id="run-old",
                session_id=session_id,
                attempt=attempt,
                turn_number=1,
                last_outcome=last_outcome,  # type: ignore[arg-type]
                updated_at=START,
            ),
        )

    # --- driving ----------------------------------------------------------------------

    async def tick(self) -> None:
        await self.orchestrator.tick()
        await asyncio.sleep(0)  # let freshly dispatched worker tasks start

    async def exit(self, run: PendingRun, **overrides: Any) -> None:
        """Finish a scripted run and let the orchestrator handle the exit."""
        run.finish(**overrides)
        await self.drain()

    async def drain(self) -> None:
        """Handle every worker exit that has been posted to the queue."""
        for _ in range(10):  # the finished session, its task and the done-callback each need a turn
            await asyncio.sleep(0)
        queue = self.orchestrator._queue
        while not queue.empty():
            message = queue.get_nowait()
            if isinstance(message, orchestrator_module._WorkerExited):
                await self.orchestrator.handle_worker_exit(message.issue_id)

    async def fire(self, seconds: float) -> None:
        self.clock.advance(seconds)
        await self.orchestrator.fire_due_retries()
        await asyncio.sleep(0)

    def run_for(self, number: int) -> PendingRun:
        return next(run for run in reversed(self.sessions.runs) if run.issue.number == number)

    def entry(self, number: int) -> RunningEntry:
        return self.orchestrator.running[str(number)]

    def retry(self, number: int) -> Any:
        return self.orchestrator.retries[str(number)]

    def calls(self, name: str) -> list[tuple[Any, ...]]:
        return [args for called, args in self.github.calls if called == name]

    def fail_on(self, method: str, monkeypatch: pytest.MonkeyPatch) -> None:
        original = getattr(self.github, method)

        async def failing(*args: Any, **kwargs: Any) -> Any:
            self.github.fail_next("transport")
            return await original(*args, **kwargs)

        monkeypatch.setattr(self.github, method, failing)


def activity(turn: int = 1, kind: str = "turn_activity", **fields: Any) -> TurnEvent:
    return TurnEvent(kind=kind, turn_number=turn, **fields)  # type: ignore[arg-type]


# --- preflight, observer, startup -------------------------------------------------------


def test_preflight_names_every_problem(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert preflight(h.workflow.config, which=h.which) == []
    h.which_missing = {"claude", "gh"}
    no_token = load_workflow(h.path, environ={"PATH": "/bin"}).config
    problems = preflight(no_token, which=h.which)
    assert problems == [
        "claude.command 'claude' not found on PATH",
        "'gh' not found on PATH",
        "github.token not set; export GH_TOKEN or set github.token: $VAR",
    ]


def test_run_observer_feeds_the_entry(tmp_path: Path, make_issue: Any) -> None:
    h = Harness(tmp_path)
    entry = RunningEntry(
        issue=make_issue(),
        attempt=1,
        rework=False,
        resumed=False,
        run_id="run-1",
        started_mono=h.clock(),
        started_at=h.now(),
        cancel=asyncio.Event(),
    )
    observer = RunObserver(entry, clock=h.clock, now=h.now)
    h.clock.advance(5)
    observer.on_turn_event(activity(kind="session_started", session_id="sess-9"))
    assert (entry.session_id, entry.last_event) == ("sess-9", "session_started")
    assert entry.last_activity_mono == START_MONO + 5
    assert entry.last_activity_at == START + timedelta(seconds=5)
    observer.on_turn_event(activity(tool_name="Read", message_type="assistant"))
    assert entry.last_event == "turn_activity:Read"
    observer.on_turn_event(activity(message_type="user"))
    assert entry.last_event == "turn_activity:user"
    observer.on_turn_event(activity(turn=2, kind="turn_completed"))
    assert (entry.turns, entry.last_event) == (2, "turn_completed")


async def test_startup_succeeds_and_logs(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.orchestrator.startup()
    assert [name for name, _ in h.github.calls] == ["auth_status", "missing_labels"]


async def test_startup_fails_on_preflight_auth_or_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.which_missing = {"gh"}
    with pytest.raises(OrchestratorStartupError) as exc:
        await h.orchestrator.startup()
    assert exc.value.problems == ["'gh' not found on PATH"]
    h.which_missing = set()
    with monkeypatch.context() as patch:
        h.fail_on("auth_status", patch)
        with pytest.raises(OrchestratorStartupError) as exc:
            await h.orchestrator.startup()
    assert exc.value.problems[0].startswith("gh auth: injected transport failure")
    assert "gh auth login" in exc.value.problems[0]
    h.github.repo_labels.pop("issuebot/review")
    with pytest.raises(OrchestratorStartupError) as exc:
        await h.orchestrator.startup()
    assert exc.value.problems == ["labels missing: issuebot/review; run issuebot labels ensure"]


# --- dispatch -----------------------------------------------------------------------------


async def test_dispatch_order_claims_and_slots(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_concurrent=2)
    h.add_issue(1, "todo")
    h.clock.advance(1)
    h.add_issue(2, "todo")
    h.add_issue(3, "rework")
    h.add_issue(4, "in_progress")
    await h.tick()
    assert sorted(h.orchestrator.running) == ["3", "4"]
    assert [run.issue.number for run in h.sessions.runs] == [4, 3]
    assert h.calls("set_state") == [(3, StateLabel.IN_PROGRESS)]
    assert h.github.issue(3).state is StateLabel.IN_PROGRESS
    assert h.run_for(3).kwargs["rework"] is True
    assert h.run_for(4).kwargs["rework"] is False
    assert h.run_for(3).issue.state is StateLabel.IN_PROGRESS
    assert h.recorder.kinds == ["state_changed"]
    assert h.snapshots[-1].counters.runs_started == 2
    await h.tick()
    assert len(h.sessions.runs) == 2
    assert h.github.issue(1).state is StateLabel.TODO
    assert h.github.issue(2).state is StateLabel.TODO


async def test_non_candidates_are_skipped(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_concurrent=5)
    h.add_issue(1, "review")
    h.github.add_issue("Unlabelled", number=2)
    h.github.add_issue("Conflict", labels=("issuebot/todo", "issuebot/rework"), number=3)
    h.add_issue(4, "todo")
    await h.tick()
    assert list(h.orchestrator.running) == ["4"]
    await h.tick()
    assert len(h.sessions.runs) == 1


async def test_claim_failure_aborts_the_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    with monkeypatch.context() as patch:
        h.fail_on("set_state", patch)
        await h.tick()
    assert h.orchestrator.running == {}
    assert h.sessions.runs == []
    assert h.github.issue(1).state is StateLabel.TODO
    await h.tick()
    assert list(h.orchestrator.running) == ["1"]


async def test_candidate_fetch_failure_skips_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    with monkeypatch.context() as patch:
        h.fail_on("fetch_issues_by_states", patch)
        await h.tick()
    assert h.sessions.runs == []
    assert h.snapshots[-1].tick_count == 1


async def test_worker_receives_the_dispatch_context(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    run = h.run_for(1)
    assert run.workflow is h.workflow
    assert run.issue.state is StateLabel.IN_PROGRESS
    assert run.issue.labels == ("issuebot/in-progress",)
    assert run.kwargs["attempt"] == 1
    assert run.kwargs["rework"] is False
    assert run.kwargs["resume_session_id"] is None
    assert isinstance(run.kwargs["cancel"], asyncio.Event)
    assert isinstance(run.kwargs["observer"], RunObserver)
    assert isinstance(run.kwargs["runner"], ClaudeRunner)
    assert isinstance(run.kwargs["workspaces"], WorkspaceManager)
    assert run.kwargs["run_id"].startswith("20260903T120000Z-")
    entry = h.entry(1)
    assert entry.run_id == run.kwargs["run_id"]
    assert (entry.attempt, entry.resumed, entry.started_at) == (1, False, START)


@pytest.mark.parametrize(
    ("last_outcome", "expected_attempt", "expected_resume"),
    [(None, 2, "sess-1"), ("cancelled", 2, "sess-1"), ("failed", 1, None), ("succeeded", 1, None)],
)
async def test_orphan_resume_follows_the_session_file(
    tmp_path: Path,
    last_outcome: str | None,
    expected_attempt: int,
    expected_resume: str | None,
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "in_progress")
    h.write_session("repo-1", issue_number=1, attempt=2, last_outcome=last_outcome)
    await h.tick()
    run = h.run_for(1)
    assert run.kwargs["attempt"] == expected_attempt
    assert run.kwargs["resume_session_id"] == expected_resume
    assert h.entry(1).resumed is (expected_resume is not None)
    assert h.calls("set_state") == []


async def test_orphan_with_a_foreign_session_file_starts_fresh(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "in_progress")
    h.write_session("repo-1", issue_number=99)
    await h.tick()
    assert h.run_for(1).kwargs["resume_session_id"] is None
    assert h.run_for(1).kwargs["attempt"] == 1


async def test_workspace_path_error_skips_the_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from issuebot.agent import AgentError

    h = Harness(tmp_path)
    h.add_issue(1, "in_progress")

    def refuse(identifier: str) -> Path:
        raise AgentError("workspace_error", "escapes the root")

    monkeypatch.setattr(h.orchestrator._workspaces, "path_for", refuse)
    await h.tick()
    assert h.sessions.runs == []


# --- terminal sweep -----------------------------------------------------------------------


async def test_terminal_sweep_runs_on_the_first_and_every_tenth_tick(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "review")
    h.github.open_pr(1, pr_number=5)
    h.github.merge_pr(5)
    h.workspace_dir("repo-1")
    await h.tick()
    assert h.github.issue(1).state is StateLabel.COMPLETE
    assert len(h.recorder.of(IssueCompleted)) == 1
    assert not (h.root / "repo-1").exists()
    h.add_issue(2, "in_progress")
    h.github.close_issue(2)
    h.github.calls.clear()
    for _ in range(9):
        await h.tick()
    assert h.calls("fetch_terminal_issues") == []
    assert h.github.issue(2).state is StateLabel.IN_PROGRESS
    await h.tick()
    assert len(h.calls("fetch_terminal_issues")) == 1
    assert h.github.issue(2).state is None
    assert h.calls("set_state") == []
    assert len(h.recorder.of(IssueCompleted)) == 1


async def test_terminal_sweep_failure_only_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    with monkeypatch.context() as patch:
        h.fail_on("fetch_terminal_issues", patch)
        await h.tick()
    assert list(h.orchestrator.running) == ["1"]


# --- reload and preflight -----------------------------------------------------------------


async def test_reload_applies_interval_slots_and_prompt(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_concurrent=1)
    h.add_issue(1, "todo")
    h.add_issue(2, "todo")
    await h.tick()
    assert len(h.sessions.runs) == 1
    h.write_workflow(max_concurrent=2, interval_ms=60_000, prompt="New {{ issue.number }}")
    await h.tick()
    assert len(h.sessions.runs) == 2
    assert h.run_for(2).workflow.prompt_template == "New {{ issue.number }}"
    assert h.run_for(1).workflow.prompt_template == "Task {{ issue.identifier }}"
    snapshot = h.snapshots[-1]
    assert (snapshot.poll_interval_ms, snapshot.max_concurrent_agents) == (60_000, 2)
    assert snapshot.config_valid is True
    assert h.orchestrator.workflow.config.polling.interval_ms == 60_000


async def test_invalid_reload_keeps_the_last_good_workflow(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    h.write_workflow(text="---\ngithub:\n  repo: example/repo\nagent:\n  bogus: 1\n---\nbody\n")
    await h.tick()
    snapshot = h.snapshots[-1]
    assert snapshot.config_valid is False
    assert snapshot.config_error is not None and "bogus" in snapshot.config_error
    assert h.orchestrator.workflow is h.workflow
    assert list(h.orchestrator.running) == ["1"]
    h.write_workflow(max_concurrent=4)
    await h.tick()
    assert h.snapshots[-1].config_valid is True
    assert h.snapshots[-1].max_concurrent_agents == 4


async def test_missing_workflow_file_is_reported_not_fatal(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.path.unlink()
    await h.tick()
    assert h.snapshots[-1].config_valid is False
    assert "unreadable" in (h.snapshots[-1].config_error or "")


async def test_preflight_failure_skips_dispatch_but_reconciles(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.add_issue(2, "todo")
    h.which_missing = {"claude"}
    h.github.human_set_state(1, StateLabel.TODO)
    h.github.calls.clear()
    await h.tick()
    assert len(h.sessions.runs) == 1
    assert h.calls("fetch_issues_by_states") == []
    assert h.entry(1).stop_cause == "moved"
    h.which_missing = set()
    await h.tick()
    assert len(h.sessions.runs) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'issuebot.orchestrator.orchestrator'`.

- [ ] **Step 3: Create the orchestrator module (this task's part)**

Create `src/issuebot/orchestrator/orchestrator.py` with this content (the class ends with `_finish`; Task 5 inserts the worker-exit and retry methods after it and Task 6 the loop, each adding the imports it needs):

```python
"""The orchestrator: one task owning the schedule, workers as child tasks, a queue between."""

import asyncio
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from issuebot.agent import (
    AgentError,
    ClaudeRunner,
    RunResult,
    TurnEvent,
    TurnRunner,
    WorkspaceManager,
    new_run_id,
    run_session,
)
from issuebot.config import ConfigError, GitHubSettings, Settings, Workflow, load_workflow
from issuebot.events import EventBus
from issuebot.github import (
    ACTIVE_STATES,
    GhCliAdapter,
    GitHubAdapter,
    GitHubError,
    Issue,
    StateLabel,
)
from issuebot.log import get_logger
from issuebot.orchestrator import actions
from issuebot.orchestrator.state import (
    REVIEW_GRACE_TICKS,
    TERMINAL_SWEEP_EVERY_TICKS,
    ClaudeTotals,
    Counters,
    RetryEntry,
    RetryRow,
    RunningEntry,
    RunningRow,
    RuntimeSnapshot,
    StopCause,
    observe_transition,
    sort_candidates,
)

RunSessionFn = Callable[..., Awaitable[RunResult]]
CANDIDATE_STATES: tuple[StateLabel, ...] = (
    StateLabel.IN_PROGRESS,
    StateLabel.REWORK,
    StateLabel.TODO,
)
SHUTDOWN_MARGIN_S = 10.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OrchestratorStartupError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def preflight(settings: Settings, *, which: Callable[[str], str | None]) -> list[str]:
    """Problems that block dispatch: the executables and the token the worker needs."""
    problems: list[str] = []
    if which(settings.claude.command) is None:
        problems.append(f"claude.command {settings.claude.command!r} not found on PATH")
    if which("gh") is None:
        problems.append("'gh' not found on PATH")
    if settings.github.token is None:
        problems.append("github.token not set; export GH_TOKEN or set github.token: $VAR")
    return problems


class RunObserver:
    """Feeds one running entry from the runner's turn events; satisfies TurnObserver."""

    def __init__(
        self,
        entry: RunningEntry,
        *,
        clock: Callable[[], float],
        now: Callable[[], datetime],
    ) -> None:
        self._entry = entry
        self._clock = clock
        self._now = now

    def on_turn_event(self, event: TurnEvent) -> None:
        entry = self._entry
        entry.last_activity_mono = self._clock()
        entry.last_activity_at = self._now()
        suffix = event.tool_name or event.message_type
        if event.kind == "turn_activity" and suffix:
            entry.last_event = f"{event.kind}:{suffix}"
        else:
            entry.last_event = event.kind
        if event.kind == "session_started" and event.session_id:
            entry.session_id = event.session_id
        if event.kind in ("turn_completed", "turn_failed", "turn_timeout"):
            entry.turns = event.turn_number


@dataclass(frozen=True, slots=True)
class _WorkerExited:
    issue_id: str


_REFRESH = object()
_STOP = object()


class Orchestrator:
    """Symphony §7 and §8 over GitHub labels: poll, claim, dispatch, retry, reconcile, recover."""

    def __init__(
        self,
        workflow: Workflow,
        *,
        bus: EventBus,
        adapter_factory: Callable[[GitHubSettings], GitHubAdapter] = GhCliAdapter,
        workspaces_factory: Callable[[Settings], WorkspaceManager] = WorkspaceManager,
        runner_factory: Callable[[Settings], TurnRunner] = ClaudeRunner,
        run_session: RunSessionFn = run_session,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
        environ: Mapping[str, str] | None = None,
        on_snapshot: Callable[[RuntimeSnapshot], None] | None = None,
    ) -> None:
        self._workflow = workflow
        self._bus = bus
        self._adapter_factory = adapter_factory
        self._workspaces_factory = workspaces_factory
        self._runner_factory = runner_factory
        self._run_session = run_session
        self._which = which
        self._clock = clock
        self._now = now
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._on_snapshot = on_snapshot
        self._adapter = adapter_factory(workflow.config.github)
        self._workspaces = workspaces_factory(workflow.config)
        self._running: dict[str, RunningEntry] = {}
        self._retries: dict[str, RetryEntry] = {}
        self._totals = ClaudeTotals()
        self._counters = Counters()
        self._tick_count = 0
        self._last_tick_at: datetime | None = None
        self._config_error: str | None = None
        self._reported_reload_error: str | None = None
        self._reported_preflight: str | None = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._refresh_pending = False
        self._stopping = False
        self._log = get_logger(__name__)

    # --- views ------------------------------------------------------------------------

    @property
    def workflow(self) -> Workflow:
        return self._workflow

    @property
    def running(self) -> Mapping[str, RunningEntry]:
        return MappingProxyType(self._running)

    @property
    def retries(self) -> Mapping[str, RetryEntry]:
        return MappingProxyType(self._retries)

    @property
    def stopping(self) -> bool:
        return self._stopping

    def snapshot(self) -> RuntimeSnapshot:
        now_mono = self._clock()
        active = sum(now_mono - entry.started_mono for entry in self._running.values())
        settings = self._workflow.config
        return RuntimeSnapshot(
            at=self._now(),
            workflow_path=str(self._workflow.path),
            workflow_mtime_ns=self._workflow.source_mtime_ns,
            config_valid=self._config_error is None,
            config_error=self._config_error,
            poll_interval_ms=settings.polling.interval_ms,
            max_concurrent_agents=settings.agent.max_concurrent_agents,
            tick_count=self._tick_count,
            last_tick_at=self._last_tick_at,
            running=tuple(RunningRow.from_entry(entry) for entry in self._running.values()),
            retrying=tuple(
                RetryRow.from_entry(entry)
                for entry in sorted(self._retries.values(), key=lambda entry: entry.due_mono)
            ),
            totals=replace(
                self._totals, seconds_running=round(self._totals.seconds_running + active, 3)
            ),
            counters=self._counters,
        )

    # --- startup ----------------------------------------------------------------------

    async def startup(self) -> None:
        """Symphony §6.3 startup validation plus the two probes; raises on any problem."""
        settings = self._workflow.config
        problems = preflight(settings, which=self._which)
        if not problems:
            try:
                await self._adapter.auth_status()
            except GitHubError as exc:
                problems.append(f"gh auth: {exc.message}; run gh auth login or set GH_TOKEN")
            try:
                missing = await self._adapter.missing_labels()
            except GitHubError as exc:
                problems.append(f"github.labels: {exc.message}")
            else:
                if missing:
                    names = ", ".join(missing)
                    problems.append(f"labels missing: {names}; run issuebot labels ensure")
        if problems:
            self._log.error("orchestrator_startup_failed", problems=problems)
            raise OrchestratorStartupError(problems)
        self._log.info(
            "orchestrator_started",
            repo=settings.github.repo,
            workflow=str(self._workflow.path),
            poll_interval_ms=settings.polling.interval_ms,
            max_concurrent_agents=settings.agent.max_concurrent_agents,
            max_turns=settings.agent.max_turns,
            max_attempts=settings.agent.max_attempts,
            stall_timeout_ms=settings.claude.stall_timeout_ms,
            workspace_root=str(settings.workspace.root),
        )

    # --- tick -------------------------------------------------------------------------

    async def tick(self) -> None:
        """Symphony §8.1: reconcile, reload, preflight, fetch, dispatch, snapshot."""
        await self.reconcile()
        self._reload_workflow()
        dispatched = 0
        problems = preflight(self._workflow.config, which=self._which)
        if problems:
            message = "; ".join(problems)
            if message != self._reported_preflight:
                self._log.error("dispatch_preflight_failed", problems=problems)
                self._reported_preflight = message
        else:
            self._reported_preflight = None
            dispatched = await self._dispatch_candidates()
        self._tick_count += 1
        self._last_tick_at = self._now()
        self._log.debug(
            "tick_finished",
            tick=self._tick_count,
            running=len(self._running),
            retrying=len(self._retries),
            dispatched=dispatched,
            slots=self._slots(),
        )
        self._publish_snapshot()

    def _slots(self) -> int:
        return max(self._workflow.config.agent.max_concurrent_agents - len(self._running), 0)

    def _publish_snapshot(self) -> None:
        if self._on_snapshot is None:
            return
        try:
            self._on_snapshot(self.snapshot())
        except Exception:
            self._log.exception("snapshot_consumer_failed")

    def _reload_workflow(self) -> None:
        path = self._workflow.path
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError as exc:
            self._report_reload_failure(f"workflow file unreadable: {exc}")
            return
        if mtime_ns == self._workflow.source_mtime_ns:
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

    def _report_reload_failure(self, message: str) -> None:
        self._config_error = message
        if message == self._reported_reload_error:
            return
        self._reported_reload_error = message
        self._log.error("workflow_reload_failed", path=str(self._workflow.path), error=message)

    async def _dispatch_candidates(self) -> int:
        try:
            issues = await self._adapter.fetch_issues_by_states(CANDIDATE_STATES)
        except GitHubError as exc:
            self._log.warning("candidates_fetch_failed", error=str(exc))
            return 0
        dispatched = 0
        for issue in sort_candidates(issues):
            if self._slots() <= 0:
                break
            if not issue.dispatchable or issue.state not in ACTIVE_STATES:
                continue
            if issue.id in self._running or issue.id in self._retries:
                continue
            attempt, resume_session_id = 1, None
            if issue.state is StateLabel.IN_PROGRESS:
                plan = self._resume_plan(issue)
                if plan is None:
                    continue
                attempt, resume_session_id = plan
            if await self._dispatch(issue, attempt=attempt, resume_session_id=resume_session_id):
                dispatched += 1
        return dispatched

    def _resume_plan(self, issue: Issue) -> tuple[int, str | None] | None:
        """How to dispatch an orphaned in_progress issue: resume, fresh, or not at all."""
        try:
            path = self._workspaces.path_for(issue.identifier)
        except AgentError as exc:
            self._log.warning(
                "dispatch_skipped",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                error=exc.message,
            )
            return None
        record = self._workspaces.read_session(path)
        if (
            record is not None
            and record.issue_number == issue.number
            and record.last_outcome in (None, "cancelled")
        ):
            return record.attempt, record.session_id
        return 1, None

    async def _dispatch(self, issue: Issue, *, attempt: int, resume_session_id: str | None) -> bool:
        rework = issue.state is StateLabel.REWORK
        if issue.state is not StateLabel.IN_PROGRESS:
            claimed = await actions.claim(self._adapter, self._bus, issue)
            if claimed is None:
                return False
            issue = claimed
        workflow = self._workflow
        entry = RunningEntry(
            issue=issue,
            attempt=attempt,
            rework=rework,
            resumed=resume_session_id is not None,
            run_id=new_run_id(self._now()),
            started_mono=self._clock(),
            started_at=self._now(),
            cancel=asyncio.Event(),
        )
        entry.task = asyncio.create_task(
            self._worker(entry, workflow, resume_session_id),
            name=f"issuebot-worker-{issue.number}",
        )
        entry.task.add_done_callback(
            lambda _task, issue_id=issue.id: self._queue.put_nowait(_WorkerExited(issue_id))
        )
        self._running[issue.id] = entry
        self._retries.pop(issue.id, None)
        self._counters = self._counters.bump(runs_started=1)
        self._log.info(
            "dispatched",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            attempt=attempt,
            rework=rework,
            resumed=entry.resumed,
            run_id=entry.run_id,
            slots_left=self._slots(),
        )
        return True

    async def _worker(
        self, entry: RunningEntry, workflow: Workflow, resume_session_id: str | None
    ) -> RunResult:
        return await self._run_session(
            entry.issue,
            workflow,
            self._adapter,
            self._bus,
            workspaces=self._workspaces,
            runner=self._runner_factory(workflow.config),
            attempt=entry.attempt,
            rework=entry.rework,
            resume_session_id=resume_session_id,
            cancel=entry.cancel,
            observer=RunObserver(entry, clock=self._clock, now=self._now),
            run_id=entry.run_id,
        )

    # --- reconcile ----------------------------------------------------------------------

    async def reconcile(self) -> None:
        """Symphony §8.5: stalls, then the label refresh; plus the terminal sweep on schedule."""
        self._check_stalls()
        if self._running:
            await self._refresh_running()
        if self._tick_count % TERMINAL_SWEEP_EVERY_TICKS == 0:
            await self.terminal_sweep()

    def _check_stalls(self) -> None:
        timeout_ms = self._workflow.config.claude.stall_timeout_ms
        if timeout_ms <= 0:
            return
        now = self._clock()
        for entry in self._running.values():
            if entry.stop_cause is not None:
                continue
            since = (
                entry.last_activity_mono
                if entry.last_activity_mono is not None
                else entry.started_mono
            )
            elapsed = now - since
            if elapsed * 1000 > timeout_ms:
                self._log.warning(
                    "reconcile_stalled",
                    issue_number=entry.issue.number,
                    issue_identifier=entry.identifier,
                    run_id=entry.run_id,
                    elapsed_s=round(elapsed),
                    stall_timeout_ms=timeout_ms,
                )
                entry.stop("stalled", f"no activity for {elapsed:.0f} s")

    async def _refresh_running(self) -> None:
        ids = list(self._running)
        try:
            refreshed = await self._adapter.fetch_issues_by_ids(ids)
        except GitHubError as exc:
            self._log.warning("reconcile_refresh_failed", error=str(exc))
            return
        by_id = {issue.id: issue for issue in refreshed}
        for issue_id in ids:
            entry = self._running.get(issue_id)
            if entry is None:
                continue
            current = by_id.get(issue_id)
            if current is None:
                self._stop_entry(entry, "missing", "issue no longer found")
                continue
            if current.github_state == "closed":
                entry.terminal_issue = current
                self._stop_entry(entry, "closed", "issue closed")
                continue
            for event in observe_transition(entry.issue, current):
                self._bus.publish(event)
            entry.issue = current
            if current.state is StateLabel.IN_PROGRESS and current.dispatchable:
                continue
            if current.state is StateLabel.REVIEW:
                if entry.review_seen_tick is None:
                    entry.review_seen_tick = self._tick_count
                    self._log.info(
                        "reconcile_review_grace",
                        issue_number=current.number,
                        issue_identifier=current.identifier,
                        run_id=entry.run_id,
                    )
                elif self._tick_count - entry.review_seen_tick >= REVIEW_GRACE_TICKS:
                    self._stop_entry(entry, "moved", "review")
                continue
            detail = current.state.value if current.state is not None else "unlabelled"
            self._stop_entry(entry, "moved", detail)

    def _stop_entry(self, entry: RunningEntry, cause: StopCause, detail: str) -> None:
        if entry.stop_cause is None:
            self._log.info(
                "reconcile_stop",
                issue_number=entry.issue.number,
                issue_identifier=entry.identifier,
                run_id=entry.run_id,
                cause=cause,
                detail=detail,
            )
        entry.stop(cause, detail)

    async def terminal_sweep(self) -> None:
        """Symphony §8.6, repeated: closed issues still carrying a state label."""
        try:
            issues = await self._adapter.fetch_terminal_issues()
        except GitHubError as exc:
            self._log.warning("terminal_sweep_failed", error=str(exc))
            return
        for issue in issues:
            if issue.id in self._running:
                continue
            self._retries.pop(issue.id, None)
            await self._finish(issue)

    async def _finish(self, issue: Issue) -> None:
        outcome = await actions.finish_terminal(self._adapter, self._bus, self._workspaces, issue)
        if outcome == "complete":
            self._counters = self._counters.bump(issues_completed=1)
        elif outcome == "cancelled":
            self._counters = self._counters.bump(issues_cancelled=1)


def _changed_sections(old: Workflow, new: Workflow) -> list[str]:
    changed = [
        name
        for name in type(old.config).model_fields
        if getattr(old.config, name) != getattr(new.config, name)
    ]
    if old.prompt_template != new.prompt_template:
        changed.append("prompt")
    return changed
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator.py -q`
Expected: 21 passed (preflight, observer, two startup tests; nine dispatch tests including the four parametrised orphan cases; two sweep tests; four reload and preflight tests). `uv run ruff check .` and `uv run ruff format --check .` clean.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/orchestrator/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: add the orchestrator core: startup, tick, dispatch, reconcile and snapshot"
```

---

### Task 5: Orchestrator, part two: worker exits, retries with backoff, the blocked escape

**Files:**
- Modify: `src/issuebot/orchestrator/orchestrator.py` (insert a block of methods into `class Orchestrator` after `_finish`)
- Test: `tests/test_orchestrator.py` (insert two sections)

**Interfaces:**
- Consumes: Task 4's class and internals; Task 2's `backoff_ms`, `CONTINUATION_DELAY_MS`, `RetryEntry`, `BlockedContext`; Task 3's `blocked_escape`.
- Produces: `handle_worker_exit(issue_id)`, `fire_due_retries()`, and the internals `_after_failure`, `_escape`, `_schedule`, `_requeue`, `_release`, `_earliest_due()` (used by Task 6's wait), `_fire`, `_add_elapsed`.

Spec: §6.7 (retries) and §6.8 (worker exit), plus the reconcile paths of §6.5 that only become observable once exits are handled (grace, closed, missing, stall).

- [ ] **Step 1: Write the failing tests**

In `tests/test_orchestrator.py`, extend two import lines: the `issuebot.events` import becomes

```python
from issuebot.events import (
    Blocked,
    Event,
    EventBus,
    IssueCancelled,
    IssueCompleted,
    PrOpened,
    StateChanged,
)
```

and the `issuebot.github` import becomes

```python
from issuebot.github import WORKPAD_MARKER, FakeGitHub, GhResult, Issue, StateLabel
```

Insert this section immediately before the `# --- terminal sweep` banner:

```python
# --- worker exits and retries -----------------------------------------------------------


async def test_a_freed_slot_dispatches_the_oldest_todo_next(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_concurrent=2)
    h.add_issue(1, "todo")
    h.clock.advance(1)
    h.add_issue(2, "todo")
    h.add_issue(3, "rework")
    h.add_issue(4, "in_progress")
    await h.tick()
    assert sorted(h.orchestrator.running) == ["3", "4"]
    h.github.human_set_state(4, StateLabel.REVIEW)
    await h.exit(h.run_for(4), final_issue=h.github.issue(4))
    assert "4" not in h.orchestrator.running
    await h.tick()
    assert [run.issue.number for run in h.sessions.runs] == [4, 3, 1]
    assert h.github.issue(2).state is StateLabel.TODO


async def test_normal_exit_to_review_schedules_a_continuation_that_releases(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.open_pr(1, pr_number=2)
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1))
    assert h.orchestrator.running == {}
    retry = h.retry(1)
    assert (retry.kind, retry.attempt, retry.error) == ("continuation", 1, None)
    assert retry.due_mono == START_MONO + 1
    assert retry.due_at == START + timedelta(seconds=1)
    assert h.recorder.kinds == ["state_changed", "state_changed", "pr_opened"]
    agent_move = h.recorder.of(StateChanged)[1]
    assert (agent_move.actor, agent_move.to_label) == ("agent", "issuebot/review")
    assert agent_move.pr_url == "https://github.com/example/repo/pull/2"
    snapshot = h.orchestrator.snapshot()
    assert snapshot.counters.runs_ended == 1
    assert (snapshot.totals.input_tokens, snapshot.totals.cost_usd) == (100, 0.5)
    assert snapshot.totals.seconds_running == 12.0
    await h.fire(0.5)
    assert "1" in h.orchestrator.retries
    await h.fire(0.5)
    assert h.orchestrator.retries == {}
    assert len(h.sessions.runs) == 1


async def test_normal_exit_to_todo_redispatches_a_fresh_attempt(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.human_set_state(1, StateLabel.TODO)
    await h.exit(h.run_for(1), final_issue=h.github.issue(1), final_state=StateLabel.TODO)
    assert h.recorder.of(StateChanged)[-1].actor == "human"
    await h.fire(1)
    assert len(h.sessions.runs) == 2
    assert h.run_for(1).kwargs["attempt"] == 1
    assert h.calls("set_state") == [(1, StateLabel.IN_PROGRESS), (1, StateLabel.IN_PROGRESS)]


async def test_failure_backoff_doubles_and_caps_then_escapes(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_attempts=3, max_retry_backoff_ms=30_000)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(
        h.run_for(1),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom",
        final_state=StateLabel.IN_PROGRESS,
    )
    retry = h.retry(1)
    assert (retry.kind, retry.attempt, retry.error) == ("failure", 2, "process_exit: boom")
    assert retry.due_mono == START_MONO + 20
    await h.fire(19)
    assert len(h.sessions.runs) == 1
    await h.fire(1)
    assert len(h.sessions.runs) == 2
    assert h.run_for(1).kwargs["attempt"] == 2
    assert h.run_for(1).kwargs["resume_session_id"] is None
    assert h.calls("set_state") == [(1, StateLabel.IN_PROGRESS)]
    await h.exit(
        h.run_for(1),
        outcome="timed_out",
        stop_reason="failure",
        error_category="turn_timeout",
        error="no output for 3600s",
        final_state=StateLabel.IN_PROGRESS,
    )
    retry = h.retry(1)
    assert (retry.attempt, retry.due_mono - h.clock()) == (3, 30.0)
    await h.fire(30)
    assert h.run_for(1).kwargs["attempt"] == 3
    await h.exit(
        h.run_for(1),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom again",
        final_state=StateLabel.IN_PROGRESS,
        turns=2,
    )
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is StateLabel.REVIEW
    body = h.github.comments_for(1)[0].body
    assert body.startswith(WORKPAD_MARKER)
    assert "3 consecutive worker sessions failed; last error: process_exit: boom again." in body
    assert "(attempt 3, 2 turns)" in body
    assert h.recorder.of(Blocked)[0].reason.startswith("3 consecutive worker sessions failed")
    assert h.orchestrator.snapshot().counters.blocked == 1


async def test_max_turns_while_in_progress_escapes_at_once(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.github.comment(1, f"{WORKPAD_MARKER}\n\n### Plan\n")
    await h.exit(
        h.run_for(1),
        stop_reason="max_turns",
        final_state=StateLabel.IN_PROGRESS,
        final_issue=h.github.issue(1),
        turns=3,
    )
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is StateLabel.REVIEW
    comments = h.github.comments_for(1)
    assert len(comments) == 1
    assert "Turn budget exhausted: 3 turns in attempt 1 without reaching `issuebot/review`." in (
        comments[0].body
    )
    assert h.recorder.kinds == ["state_changed", "state_changed", "blocked"]
    assert h.recorder.of(StateChanged)[1].actor == "issuebot"


async def test_escape_failure_is_retried_with_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    run = h.run_for(1)
    with monkeypatch.context() as patch:
        h.fail_on("set_state", patch)
        await h.exit(run, stop_reason="max_turns", final_state=StateLabel.IN_PROGRESS, turns=3)
    retry = h.retry(1)
    assert (retry.kind, retry.attempt, retry.error) == ("escape", 1, "blocked escape failed")
    assert retry.due_mono == START_MONO + 10
    assert retry.escape is not None
    assert retry.escape.run_id == run.kwargs["run_id"]
    assert h.github.issue(1).state is StateLabel.IN_PROGRESS
    with monkeypatch.context() as patch:
        h.fail_on("set_state", patch)
        await h.fire(10)
    retry = h.retry(1)
    assert (retry.kind, retry.attempt, retry.due_mono - h.clock()) == ("escape", 2, 20.0)
    await h.fire(20)
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.github.comments_for(1)[0].body.count("### Issuebot blocked") == 1
    assert h.orchestrator.snapshot().counters.blocked == 1


async def test_retry_requeues_when_no_slot_is_free(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_concurrent=2)
    h.add_issue(1, "todo")
    h.add_issue(2, "todo")
    await h.tick()
    await h.exit(
        h.run_for(2),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom",
        final_state=StateLabel.IN_PROGRESS,
    )
    h.add_issue(3, "todo")
    await h.tick()
    assert sorted(h.orchestrator.running) == ["1", "3"]
    await h.fire(20)
    retry = h.retry(2)
    assert (retry.kind, retry.attempt, retry.error) == (
        "slots",
        2,
        "no available orchestrator slots",
    )
    assert retry.due_mono == h.clock() + 30
    assert len(h.sessions.runs) == 3


async def test_retry_refresh_failure_requeues_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(
        h.run_for(1),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom",
        final_state=StateLabel.IN_PROGRESS,
    )
    with monkeypatch.context() as patch:
        h.fail_on("fetch_issues_by_ids", patch)
        await h.fire(20)
    retry = h.retry(1)
    assert (retry.kind, retry.attempt) == ("failure", 2)
    assert retry.error is not None and retry.error.startswith("retry refresh failed: ")
    assert retry.due_mono == h.clock() + 30
    assert len(h.sessions.runs) == 1


async def test_retry_finds_the_issue_closed_or_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path, max_concurrent=3)
    h.add_issue(1, "todo")
    h.add_issue(2, "todo")
    await h.tick()
    for number in (1, 2):
        await h.exit(
            h.run_for(number),
            outcome="failed",
            stop_reason="failure",
            error_category="process_exit",
            error="boom",
            final_state=StateLabel.IN_PROGRESS,
        )
    h.workspace_dir("repo-1")
    h.github.close_issue(1)

    original = h.github.fetch_issues_by_ids

    async def hide_two(ids: Any) -> list[Issue]:
        return [issue for issue in await original(ids) if issue.number != 2]

    monkeypatch.setattr(h.github, "fetch_issues_by_ids", hide_two)
    await h.fire(20)
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is None
    assert isinstance(h.recorder.of(IssueCancelled)[0], IssueCancelled)
    assert not (h.root / "repo-1").exists()
    assert len(h.sessions.runs) == 2


async def test_crashed_worker_is_retried(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.clock.advance(7)
    h.run_for(1).fail(RuntimeError("kaboom"))
    await h.drain()
    retry = h.retry(1)
    assert (retry.kind, retry.attempt, retry.error) == ("failure", 2, "worker crashed: kaboom")
    snapshot = h.orchestrator.snapshot()
    assert snapshot.counters.runs_ended == 1
    assert snapshot.totals.seconds_running == 7.0


async def test_terminal_sweep_drops_a_retry_for_a_closed_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(
        h.run_for(1),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom",
        final_state=StateLabel.IN_PROGRESS,
    )
    h.github.close_issue(1)
    for _ in range(10):
        await h.tick()
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is None


# --- reconcile ----------------------------------------------------------------------------


async def test_reconcile_with_nothing_running_makes_no_request(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.tick()
    h.github.calls.clear()
    await h.orchestrator.reconcile()
    assert h.github.calls == []


async def test_reconcile_refresh_failure_keeps_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.human_set_state(1, StateLabel.TODO)
    with monkeypatch.context() as patch:
        h.fail_on("fetch_issues_by_ids", patch)
        await h.tick()
    assert h.entry(1).stop_cause is None
    assert not h.entry(1).cancel.is_set()


async def test_reconcile_updates_the_snapshot_and_sees_the_pr(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.open_pr(1, pr_number=5)
    await h.tick()
    entry = h.entry(1)
    assert entry.issue.linked_pr is not None and entry.issue.linked_pr.number == 5
    assert entry.stop_cause is None
    assert h.recorder.of(PrOpened)[0].pr_number == 5
    await h.tick()
    assert len(h.recorder.of(PrOpened)) == 1


async def test_reconcile_gives_review_one_tick_of_grace(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.tick()
    entry = h.entry(1)
    assert entry.review_seen_tick == 1
    assert not entry.cancel.is_set()
    assert [event.actor for event in h.recorder.of(StateChanged)] == ["issuebot", "agent"]
    await h.tick()
    assert entry.cancel.is_set()
    assert (entry.stop_cause, entry.stop_detail) == ("moved", "review")
    await h.drain()
    assert h.orchestrator.running == {}
    assert h.orchestrator.retries == {}
    assert (h.root / "repo-1").is_dir()
    assert len(h.recorder.of(StateChanged)) == 2


@pytest.mark.parametrize("state", [StateLabel.TODO, StateLabel.REWORK])
async def test_reconcile_cancels_other_moves_at_once(tmp_path: Path, state: StateLabel) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.human_set_state(1, state)
    await h.tick()
    entry = h.entry(1)
    assert entry.cancel.is_set()
    assert (entry.stop_cause, entry.stop_detail) == ("moved", state.value)
    assert h.recorder.of(StateChanged)[-1].actor == "human"
    await h.drain()
    assert h.orchestrator.running == {}
    assert h.orchestrator.retries == {}


async def test_reconcile_cancels_an_unlabelled_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.human_remove_label(1, "issuebot/in-progress")
    await h.tick()
    assert (h.entry(1).stop_cause, h.entry(1).stop_detail) == ("moved", "unlabelled")


async def test_reconcile_completes_a_closed_issue_after_the_worker_exits(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")
    h.github.open_pr(1, pr_number=5)
    h.github.merge_pr(5)
    await h.tick()
    entry = h.entry(1)
    assert entry.stop_cause == "closed"
    assert entry.terminal_issue is not None
    assert (h.root / "repo-1").is_dir()
    await h.drain()
    assert h.orchestrator.running == {}
    assert h.github.issue(1).state is StateLabel.COMPLETE
    assert h.recorder.of(IssueCompleted)[0].pr_url == "https://github.com/example/repo/pull/5"
    assert not (h.root / "repo-1").exists()
    assert h.orchestrator.snapshot().counters.issues_completed == 1


async def test_reconcile_cancels_a_closed_unmerged_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")
    h.github.close_issue(1)
    await h.tick()
    await h.drain()
    assert h.github.issue(1).state is None
    assert len(h.recorder.of(IssueCancelled)) == 1
    assert not (h.root / "repo-1").exists()
    assert h.orchestrator.snapshot().counters.issues_cancelled == 1


async def test_reconcile_releases_a_missing_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")

    async def nothing(ids: Any) -> list[Issue]:
        return []

    monkeypatch.setattr(h.github, "fetch_issues_by_ids", nothing)
    await h.tick()
    assert h.entry(1).stop_cause == "missing"
    await h.drain()
    assert h.orchestrator.running == {}
    assert h.orchestrator.retries == {}
    assert (h.root / "repo-1").is_dir()


async def test_stall_detection_kills_and_retries(tmp_path: Path) -> None:
    h = Harness(tmp_path, stall_timeout_ms=300_000)
    h.add_issue(1, "todo")
    await h.tick()
    run = h.run_for(1)
    h.clock.advance(200)
    run.observer.on_turn_event(activity(tool_name="Bash", message_type="assistant"))
    h.clock.advance(150)
    await h.tick()
    assert h.entry(1).stop_cause is None
    h.clock.advance(151)
    await h.tick()
    entry = h.entry(1)
    assert (entry.stop_cause, entry.stop_detail) == ("stalled", "no activity for 301 s")
    assert entry.cancel.is_set()
    await h.drain()
    retry = h.retry(1)
    assert (retry.kind, retry.attempt, retry.error) == (
        "failure",
        2,
        "stalled: no activity for 301 s",
    )


async def test_stall_detection_is_disabled_at_zero(tmp_path: Path) -> None:
    h = Harness(tmp_path, stall_timeout_ms=0)
    h.add_issue(1, "todo")
    await h.tick()
    h.clock.advance(100_000)
    await h.tick()
    assert h.entry(1).stop_cause is None
```

and append this section at the end of the file (after the reload and preflight section):

```python
# --- snapshot -----------------------------------------------------------------------------


async def test_snapshot_rows_and_active_seconds(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo", title="First")
    h.add_issue(2, "todo")
    await h.tick()
    h.run_for(1).observer.on_turn_event(activity(kind="session_started", session_id="sess-1"))
    await h.exit(
        h.run_for(2),
        outcome="failed",
        stop_reason="failure",
        error_category="process_exit",
        error="boom",
        final_state=StateLabel.IN_PROGRESS,
    )
    h.clock.advance(5)
    snapshot = h.orchestrator.snapshot()
    assert snapshot.at == h.now()
    assert snapshot.workflow_path == str(h.path)
    assert (snapshot.tick_count, snapshot.last_tick_at) == (1, START)
    row = snapshot.running[0]
    assert (row.issue_number, row.identifier, row.title, row.state) == (
        1,
        "repo-1",
        "First",
        "in_progress",
    )
    assert (row.session_id, row.attempt, row.started_at) == ("sess-1", 1, START)
    assert row.last_activity_at == START
    assert row.last_event == "session_started"
    retry = snapshot.retrying[0]
    assert (retry.issue_number, retry.kind, retry.attempt) == (2, "failure", 2)
    assert retry.due_at == START + timedelta(seconds=20)
    assert snapshot.totals.seconds_running == 12.0 + 5.0
    assert snapshot.counters.runs_started == 2
    assert snapshot.to_dict()["running"][0]["identifier"] == "repo-1"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py -q`
Expected: the new tests fail with `AttributeError: 'Orchestrator' object has no attribute 'handle_worker_exit'` (via `Harness.drain`) or `... 'fire_due_retries'`; the Task 4 tests still pass.

- [ ] **Step 3: Add the worker-exit and retry methods**

In `src/issuebot/orchestrator/orchestrator.py`, add `import math` after `import asyncio`, change `from datetime import UTC, datetime` to `from datetime import UTC, datetime, timedelta`, and add `CONTINUATION_DELAY_MS`, `BlockedContext`, `RetryKind` and `backoff_ms` to the `issuebot.orchestrator.state` import (keeping it sorted: constants first, then CamelCase, then lowercase, as the final file in Task 6 shows). Then insert the following block into `class Orchestrator` immediately after the `_finish` method (before the module-level `_changed_sections`):

```python
# --- worker exits and retries -----------------------------------------------------


async def handle_worker_exit(self, issue_id: str) -> None:
    """Symphony §16.6 with the blocked escape: totals, then retry, escape or release."""
    entry = self._running.pop(issue_id, None)
    if entry is None or entry.task is None:
        return
    task = entry.task
    self._counters = self._counters.bump(runs_ended=1)
    result: RunResult | None = None
    error: str | None = None
    if task.cancelled():
        error = "worker task cancelled"
        self._add_elapsed(entry)
    elif task.exception() is not None:
        exc = task.exception()
        self._log.error(
            "worker_crashed",
            issue_number=entry.issue.number,
            issue_identifier=entry.identifier,
            run_id=entry.run_id,
            error=str(exc),
            exc_info=exc,
        )
        error = f"worker crashed: {exc}"
        self._add_elapsed(entry)
    else:
        result = task.result()
        self._totals = self._totals.add(result)
    self._log.info(
        "worker_exited",
        issue_number=entry.issue.number,
        issue_identifier=entry.identifier,
        run_id=entry.run_id,
        attempt=entry.attempt,
        outcome=result.outcome if result is not None else None,
        stop_reason=result.stop_reason if result is not None else None,
        cause=entry.stop_cause,
        turns=result.turns if result is not None else entry.turns,
        cost_usd=result.cost_usd if result is not None else 0.0,
        error=error or (result.error if result is not None else None),
    )
    if entry.terminal_issue is not None:
        await self._finish(entry.terminal_issue)
        return
    if task.cancelled() or entry.stop_cause in ("moved", "missing", "shutdown"):
        self._log.info(
            "issue_released",
            issue_number=entry.issue.number,
            issue_identifier=entry.identifier,
            reason=entry.stop_cause or "task cancelled",
        )
        return
    if entry.stop_cause == "stalled":
        await self._after_failure(entry, f"stalled: {entry.stop_detail}", result)
        return
    if result is None:
        await self._after_failure(entry, error or "worker crashed", None)
        return
    if result.outcome == "succeeded":
        if result.stop_reason == "max_turns" and result.final_state is StateLabel.IN_PROGRESS:
            review = self._workflow.config.github.labels.review
            reason = (
                f"Turn budget exhausted: {result.turns} turns in attempt {entry.attempt} "
                f"without reaching `{review}`."
            )
            await self._escape(entry, reason, result)
            return
        final = result.final_issue
        if final is not None and final.github_state == "open":
            for event in observe_transition(entry.issue, final):
                self._bus.publish(event)
        self._schedule(
            entry.issue,
            attempt=1,
            kind="continuation",
            delay_ms=CONTINUATION_DELAY_MS,
            error=None,
        )
        return
    await self._after_failure(entry, f"{result.error_category}: {result.error}", result)


def _add_elapsed(self, entry: RunningEntry) -> None:
    elapsed = self._clock() - entry.started_mono
    self._totals = replace(
        self._totals, seconds_running=round(self._totals.seconds_running + elapsed, 3)
    )


async def _after_failure(self, entry: RunningEntry, error: str, result: RunResult | None) -> None:
    agent = self._workflow.config.agent
    if entry.attempt >= agent.max_attempts:
        reason = f"{agent.max_attempts} consecutive worker sessions failed; last error: {error}."
        await self._escape(entry, reason, result)
        return
    self._schedule(
        entry.issue,
        attempt=entry.attempt + 1,
        kind="failure",
        delay_ms=backoff_ms(entry.attempt + 1, agent.max_retry_backoff_ms),
        error=error,
    )


async def _escape(self, entry: RunningEntry, reason: str, result: RunResult | None) -> None:
    context = BlockedContext(
        reason=reason,
        run_id=entry.run_id,
        attempt=entry.attempt,
        turns=result.turns if result is not None else entry.turns,
        log_dir=str(result.log_dir) if result is not None and result.log_dir else None,
    )
    outcome = await actions.blocked_escape(
        self._adapter, self._bus, entry.issue_id, context, now=self._now()
    )
    if outcome == "applied":
        self._counters = self._counters.bump(blocked=1)
    elif outcome == "failed":
        self._schedule(
            entry.issue,
            attempt=1,
            kind="escape",
            delay_ms=backoff_ms(1, self._workflow.config.agent.max_retry_backoff_ms),
            error="blocked escape failed",
            escape=context,
        )


def _schedule(
    self,
    issue: Issue,
    *,
    attempt: int,
    kind: RetryKind,
    delay_ms: int,
    error: str | None,
    escape: BlockedContext | None = None,
) -> None:
    entry = RetryEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        issue_number=issue.number,
        issue_url=issue.url,
        attempt=attempt,
        kind=kind,
        due_mono=self._clock() + delay_ms / 1000,
        due_at=self._now() + timedelta(milliseconds=delay_ms),
        error=error,
        escape=escape,
    )
    self._retries[issue.id] = entry
    self._log.info(
        "retry_scheduled",
        issue_number=issue.number,
        issue_identifier=issue.identifier,
        kind=kind,
        attempt=attempt,
        due_in_ms=delay_ms,
        error=error,
    )


def _requeue(self, entry: RetryEntry, *, delay_ms: int, error: str, kind: RetryKind) -> None:
    self._retries[entry.issue_id] = replace(
        entry,
        kind=kind,
        due_mono=self._clock() + delay_ms / 1000,
        due_at=self._now() + timedelta(milliseconds=delay_ms),
        error=error,
    )
    self._log.info(
        "retry_scheduled",
        issue_number=entry.issue_number,
        issue_identifier=entry.identifier,
        kind=kind,
        attempt=entry.attempt,
        due_in_ms=delay_ms,
        error=error,
    )


def _release(self, entry: RetryEntry, reason: str) -> None:
    self._log.info(
        "retry_released",
        issue_number=entry.issue_number,
        issue_identifier=entry.identifier,
        kind=entry.kind,
        reason=reason,
    )


def _earliest_due(self) -> float:
    return min((entry.due_mono for entry in self._retries.values()), default=math.inf)


async def fire_due_retries(self) -> None:
    """Symphony §16.6's retry timer, for every entry whose time has come."""
    now = self._clock()
    due = sorted(
        (entry for entry in self._retries.values() if entry.due_mono <= now),
        key=lambda entry: entry.due_mono,
    )
    for entry in due:
        if self._stopping:
            return
        if self._retries.get(entry.issue_id) is not entry:
            continue
        del self._retries[entry.issue_id]
        await self._fire(entry)


async def _fire(self, entry: RetryEntry) -> None:
    settings = self._workflow.config
    self._log.info(
        "retry_fired",
        issue_number=entry.issue_number,
        issue_identifier=entry.identifier,
        kind=entry.kind,
        attempt=entry.attempt,
    )
    if entry.kind == "escape" and entry.escape is not None:
        outcome = await actions.blocked_escape(
            self._adapter, self._bus, entry.issue_id, entry.escape, now=self._now()
        )
        if outcome == "applied":
            self._counters = self._counters.bump(blocked=1)
        elif outcome == "failed":
            self._requeue(
                replace(entry, attempt=entry.attempt + 1),
                kind="escape",
                delay_ms=backoff_ms(entry.attempt + 1, settings.agent.max_retry_backoff_ms),
                error="blocked escape failed",
            )
        return
    try:
        issues = await self._adapter.fetch_issues_by_ids([entry.issue_id])
    except GitHubError as exc:
        self._requeue(
            entry,
            kind=entry.kind,
            delay_ms=settings.polling.interval_ms,
            error=f"retry refresh failed: {exc.message}",
        )
        return
    if not issues:
        self._release(entry, "missing")
        return
    issue = issues[0]
    if issue.github_state == "closed":
        await self._finish(issue)
        return
    if not issue.dispatchable or issue.state not in ACTIVE_STATES:
        self._release(entry, "not_active")
        return
    if self._slots() <= 0:
        self._requeue(
            entry,
            kind="slots",
            delay_ms=settings.polling.interval_ms,
            error="no available orchestrator slots",
        )
        return
    attempt = entry.attempt if issue.state is StateLabel.IN_PROGRESS else 1
    await self._dispatch(issue, attempt=attempt, resume_session_id=None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator.py -q`
Expected: 45 passed. `uv run ruff check .` clean (every added import is used).

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/orchestrator/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: handle worker exits, retries with backoff and the blocked escape"
```

---

### Task 6: Orchestrator, part three: the loop, refresh, stop, shutdown, end to end

**Files:**
- Modify: `src/issuebot/orchestrator/orchestrator.py` (insert the loop block after `_fire`), `src/issuebot/orchestrator/__init__.py` (complete re-exports)
- Test: `tests/test_orchestrator.py` (append two sections)

**Interfaces:**
- Consumes: Tasks 4 and 5.
- Produces: `run()`, `request_refresh()`, `request_stop()`, `shutdown()`, `_wait_for_next_tick()`; the package `issuebot.orchestrator` re-exporting `Orchestrator`, `OrchestratorStartupError`, `RunObserver`, `RunSessionFn`, `CANDIDATE_STATES`, `preflight`, the actions and the state names.

Spec: §6.9 (the loop, refresh and stop) and the end-to-end row of §11.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orchestrator.py`:

```python
# --- the loop -----------------------------------------------------------------------------


async def wait_until(condition: Any, *, timeout: float = 10.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def test_run_ticks_refreshes_and_stops(tmp_path: Path) -> None:
    h = Harness(tmp_path, interval_ms=60_000)
    h.orchestrator._clock = __import__("time").monotonic
    task = asyncio.create_task(h.orchestrator.run())
    await wait_until(lambda: len(h.snapshots) == 1)
    h.orchestrator.request_refresh()
    h.orchestrator.request_refresh()
    await wait_until(lambda: len(h.snapshots) == 2, timeout=2.0)
    await asyncio.sleep(0.1)
    assert len(h.snapshots) == 2
    h.orchestrator.request_stop()
    await asyncio.wait_for(task, timeout=5)
    assert h.orchestrator.stopping is True


async def test_shutdown_cancels_workers_and_leaves_the_label(tmp_path: Path) -> None:
    h = Harness(tmp_path, interval_ms=60_000)
    h.orchestrator._clock = __import__("time").monotonic
    h.add_issue(1, "todo")
    task = asyncio.create_task(h.orchestrator.run())
    await wait_until(lambda: len(h.sessions.runs) == 1)
    h.orchestrator.request_stop()
    await asyncio.wait_for(task, timeout=5)
    assert h.run_for(1).cancel.is_set()
    assert h.orchestrator.running == {}
    assert h.orchestrator.retries == {}
    assert h.github.issue(1).state is StateLabel.IN_PROGRESS
    assert h.orchestrator.snapshot().counters.runs_ended == 1


async def test_shutdown_cancels_stragglers_after_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path, interval_ms=60_000)
    h.orchestrator._clock = __import__("time").monotonic
    monkeypatch.setattr(orchestrator_module, "TERMINATE_GRACE_S", 0.0)
    monkeypatch.setattr(orchestrator_module, "SHUTDOWN_MARGIN_S", 0.2)
    h.write_workflow(hooks={"timeout_ms": "1"})
    h.orchestrator._workflow = load_workflow(h.path, environ=h.environ)

    async def stubborn(*args: Any, **kwargs: Any) -> RunResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    h.orchestrator._run_session = stubborn
    h.add_issue(1, "todo")
    task = asyncio.create_task(h.orchestrator.run())
    await wait_until(lambda: len(h.orchestrator.running) == 1)
    h.orchestrator.request_stop()
    await asyncio.wait_for(task, timeout=5)
    assert h.orchestrator.running == {}
    assert h.orchestrator.snapshot().counters.runs_ended == 1


async def test_run_propagates_startup_errors(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.which_missing = {"claude"}
    with pytest.raises(OrchestratorStartupError):
        await h.orchestrator.run()


# --- end to end ---------------------------------------------------------------------------


@posix
async def test_end_to_end_with_the_fakes(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        max_turns=1,
        interval_ms=1000,
        claude=str(FAKE_CLAUDE),
        hooks={"after_run": "touch after-run-ran"},
        real_sessions=True,
    )
    h.orchestrator._clock = __import__("time").monotonic
    h.add_issue(1, "todo", title="Add a function")
    task = asyncio.create_task(h.orchestrator.run())
    await wait_until(lambda: h.github.issue(1).state is StateLabel.REVIEW, timeout=20.0)
    h.orchestrator.request_stop()
    await asyncio.wait_for(task, timeout=10)
    assert h.recorder.kinds == [
        "state_changed",
        "run_started",
        "run_ended",
        "state_changed",
        "blocked",
    ]
    workspace = h.root / "repo-1"
    assert (workspace / ".git").is_dir()
    assert (workspace / "after-run-ran").exists()
    assert (workspace / ".issuebot" / "session.json").exists()
    runs = list((workspace / ".issuebot" / "runs").iterdir())
    assert len(runs) == 1
    assert (runs[0] / "turn-1.jsonl").exists()
    body = h.github.comments_for(1)[0].body
    assert body.startswith(WORKPAD_MARKER)
    assert "Turn budget exhausted: 1 turns in attempt 1" in body
    assert str(runs[0]) in body
    totals = h.orchestrator.snapshot().totals
    assert totals.input_tokens > 0 and totals.cost_usd > 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py -q -k "run_ticks or shutdown or propagates or end_to_end"`
Expected: `AttributeError: 'Orchestrator' object has no attribute 'run'` (and `request_refresh`, `request_stop`, `stopping` is already present).

- [ ] **Step 3: Add the loop**

In `src/issuebot/orchestrator/orchestrator.py`, add `from issuebot.agent.runner import TERMINATE_GRACE_S` after the `from issuebot.agent import (...)` block, then insert the following block into `class Orchestrator` immediately after the `_fire` method (before the module-level `_changed_sections`):

```python
# --- the loop -----------------------------------------------------------------------


async def run(self) -> None:
    """Startup, then tick and wait until stopped; shutdown on the way out."""
    await self.startup()
    try:
        while not self._stopping:
            await self.tick()
            await self._wait_for_next_tick()
    finally:
        await self.shutdown()


async def _wait_for_next_tick(self) -> None:
    deadline = self._clock() + self._workflow.config.polling.interval_ms / 1000
    while not self._stopping:
        await self.fire_due_retries()
        now = self._clock()
        if now >= deadline:
            return
        timeout = min(deadline, self._earliest_due()) - now
        if timeout <= 0:
            continue
        try:
            message = await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            continue
        if message is _REFRESH:
            self._refresh_pending = False
            return
        if message is _STOP:
            return
        if isinstance(message, _WorkerExited):
            await self.handle_worker_exit(message.issue_id)


def request_refresh(self) -> None:
    """Ask for a tick now; coalesced while one is already pending."""
    if self._refresh_pending:
        return
    self._refresh_pending = True
    self._queue.put_nowait(_REFRESH)


def request_stop(self) -> None:
    if self._stopping:
        return
    self._stopping = True
    self._log.info("stop_requested")
    self._queue.put_nowait(_STOP)


async def shutdown(self) -> None:
    """Cancel every worker, wait for after_run, drain the exits, drop the retries."""
    self._stopping = True
    settings = self._workflow.config
    if self._running:
        self._log.info("shutdown_started", running=len(self._running))
        tasks: list[asyncio.Task[RunResult]] = []
        for entry in self._running.values():
            entry.stop("shutdown", "worker stopping")
            if entry.task is not None:
                tasks.append(entry.task)
        timeout = settings.hooks.timeout_ms / 1000 + TERMINATE_GRACE_S + SHUTDOWN_MARGIN_S
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            self._log.warning("shutdown_timeout", pending=len(pending), timeout_s=timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for issue_id in list(self._running):
            await self.handle_worker_exit(issue_id)
    self._retries.clear()
    while not self._queue.empty():
        self._queue.get_nowait()
    counters = self._counters
    self._log.info(
        "orchestrator_stopped",
        runs_started=counters.runs_started,
        runs_ended=counters.runs_ended,
        issues_completed=counters.issues_completed,
        issues_cancelled=counters.issues_cancelled,
        blocked=counters.blocked,
        cost_usd=self._totals.cost_usd,
    )
```

For reference, the complete module after this step is:

```python
"""The orchestrator: one task owning the schedule, workers as child tasks, a queue between."""

import asyncio
import math
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

from issuebot.agent import (
    AgentError,
    ClaudeRunner,
    RunResult,
    TurnEvent,
    TurnRunner,
    WorkspaceManager,
    new_run_id,
    run_session,
)
from issuebot.agent.runner import TERMINATE_GRACE_S
from issuebot.config import ConfigError, GitHubSettings, Settings, Workflow, load_workflow
from issuebot.events import EventBus
from issuebot.github import (
    ACTIVE_STATES,
    GhCliAdapter,
    GitHubAdapter,
    GitHubError,
    Issue,
    StateLabel,
)
from issuebot.log import get_logger
from issuebot.orchestrator import actions
from issuebot.orchestrator.state import (
    CONTINUATION_DELAY_MS,
    REVIEW_GRACE_TICKS,
    TERMINAL_SWEEP_EVERY_TICKS,
    BlockedContext,
    ClaudeTotals,
    Counters,
    RetryEntry,
    RetryKind,
    RetryRow,
    RunningEntry,
    RunningRow,
    RuntimeSnapshot,
    StopCause,
    backoff_ms,
    observe_transition,
    sort_candidates,
)

RunSessionFn = Callable[..., Awaitable[RunResult]]
CANDIDATE_STATES: tuple[StateLabel, ...] = (
    StateLabel.IN_PROGRESS,
    StateLabel.REWORK,
    StateLabel.TODO,
)
SHUTDOWN_MARGIN_S = 10.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OrchestratorStartupError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def preflight(settings: Settings, *, which: Callable[[str], str | None]) -> list[str]:
    """Problems that block dispatch: the executables and the token the worker needs."""
    problems: list[str] = []
    if which(settings.claude.command) is None:
        problems.append(f"claude.command {settings.claude.command!r} not found on PATH")
    if which("gh") is None:
        problems.append("'gh' not found on PATH")
    if settings.github.token is None:
        problems.append("github.token not set; export GH_TOKEN or set github.token: $VAR")
    return problems


class RunObserver:
    """Feeds one running entry from the runner's turn events; satisfies TurnObserver."""

    def __init__(
        self,
        entry: RunningEntry,
        *,
        clock: Callable[[], float],
        now: Callable[[], datetime],
    ) -> None:
        self._entry = entry
        self._clock = clock
        self._now = now

    def on_turn_event(self, event: TurnEvent) -> None:
        entry = self._entry
        entry.last_activity_mono = self._clock()
        entry.last_activity_at = self._now()
        suffix = event.tool_name or event.message_type
        if event.kind == "turn_activity" and suffix:
            entry.last_event = f"{event.kind}:{suffix}"
        else:
            entry.last_event = event.kind
        if event.kind == "session_started" and event.session_id:
            entry.session_id = event.session_id
        if event.kind in ("turn_completed", "turn_failed", "turn_timeout"):
            entry.turns = event.turn_number


@dataclass(frozen=True, slots=True)
class _WorkerExited:
    issue_id: str


_REFRESH = object()
_STOP = object()


class Orchestrator:
    """Symphony §7 and §8 over GitHub labels: poll, claim, dispatch, retry, reconcile, recover."""

    def __init__(
        self,
        workflow: Workflow,
        *,
        bus: EventBus,
        adapter_factory: Callable[[GitHubSettings], GitHubAdapter] = GhCliAdapter,
        workspaces_factory: Callable[[Settings], WorkspaceManager] = WorkspaceManager,
        runner_factory: Callable[[Settings], TurnRunner] = ClaudeRunner,
        run_session: RunSessionFn = run_session,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
        environ: Mapping[str, str] | None = None,
        on_snapshot: Callable[[RuntimeSnapshot], None] | None = None,
    ) -> None:
        self._workflow = workflow
        self._bus = bus
        self._adapter_factory = adapter_factory
        self._workspaces_factory = workspaces_factory
        self._runner_factory = runner_factory
        self._run_session = run_session
        self._which = which
        self._clock = clock
        self._now = now
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._on_snapshot = on_snapshot
        self._adapter = adapter_factory(workflow.config.github)
        self._workspaces = workspaces_factory(workflow.config)
        self._running: dict[str, RunningEntry] = {}
        self._retries: dict[str, RetryEntry] = {}
        self._totals = ClaudeTotals()
        self._counters = Counters()
        self._tick_count = 0
        self._last_tick_at: datetime | None = None
        self._config_error: str | None = None
        self._reported_reload_error: str | None = None
        self._reported_preflight: str | None = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._refresh_pending = False
        self._stopping = False
        self._log = get_logger(__name__)

    # --- views ------------------------------------------------------------------------

    @property
    def workflow(self) -> Workflow:
        return self._workflow

    @property
    def running(self) -> Mapping[str, RunningEntry]:
        return MappingProxyType(self._running)

    @property
    def retries(self) -> Mapping[str, RetryEntry]:
        return MappingProxyType(self._retries)

    @property
    def stopping(self) -> bool:
        return self._stopping

    def snapshot(self) -> RuntimeSnapshot:
        now_mono = self._clock()
        active = sum(now_mono - entry.started_mono for entry in self._running.values())
        settings = self._workflow.config
        return RuntimeSnapshot(
            at=self._now(),
            workflow_path=str(self._workflow.path),
            workflow_mtime_ns=self._workflow.source_mtime_ns,
            config_valid=self._config_error is None,
            config_error=self._config_error,
            poll_interval_ms=settings.polling.interval_ms,
            max_concurrent_agents=settings.agent.max_concurrent_agents,
            tick_count=self._tick_count,
            last_tick_at=self._last_tick_at,
            running=tuple(RunningRow.from_entry(entry) for entry in self._running.values()),
            retrying=tuple(
                RetryRow.from_entry(entry)
                for entry in sorted(self._retries.values(), key=lambda entry: entry.due_mono)
            ),
            totals=replace(
                self._totals, seconds_running=round(self._totals.seconds_running + active, 3)
            ),
            counters=self._counters,
        )

    # --- startup ----------------------------------------------------------------------

    async def startup(self) -> None:
        """Symphony §6.3 startup validation plus the two probes; raises on any problem."""
        settings = self._workflow.config
        problems = preflight(settings, which=self._which)
        if not problems:
            try:
                await self._adapter.auth_status()
            except GitHubError as exc:
                problems.append(f"gh auth: {exc.message}; run gh auth login or set GH_TOKEN")
            try:
                missing = await self._adapter.missing_labels()
            except GitHubError as exc:
                problems.append(f"github.labels: {exc.message}")
            else:
                if missing:
                    names = ", ".join(missing)
                    problems.append(f"labels missing: {names}; run issuebot labels ensure")
        if problems:
            self._log.error("orchestrator_startup_failed", problems=problems)
            raise OrchestratorStartupError(problems)
        self._log.info(
            "orchestrator_started",
            repo=settings.github.repo,
            workflow=str(self._workflow.path),
            poll_interval_ms=settings.polling.interval_ms,
            max_concurrent_agents=settings.agent.max_concurrent_agents,
            max_turns=settings.agent.max_turns,
            max_attempts=settings.agent.max_attempts,
            stall_timeout_ms=settings.claude.stall_timeout_ms,
            workspace_root=str(settings.workspace.root),
        )

    # --- tick -------------------------------------------------------------------------

    async def tick(self) -> None:
        """Symphony §8.1: reconcile, reload, preflight, fetch, dispatch, snapshot."""
        await self.reconcile()
        self._reload_workflow()
        dispatched = 0
        problems = preflight(self._workflow.config, which=self._which)
        if problems:
            message = "; ".join(problems)
            if message != self._reported_preflight:
                self._log.error("dispatch_preflight_failed", problems=problems)
                self._reported_preflight = message
        else:
            self._reported_preflight = None
            dispatched = await self._dispatch_candidates()
        self._tick_count += 1
        self._last_tick_at = self._now()
        self._log.debug(
            "tick_finished",
            tick=self._tick_count,
            running=len(self._running),
            retrying=len(self._retries),
            dispatched=dispatched,
            slots=self._slots(),
        )
        self._publish_snapshot()

    def _slots(self) -> int:
        return max(self._workflow.config.agent.max_concurrent_agents - len(self._running), 0)

    def _publish_snapshot(self) -> None:
        if self._on_snapshot is None:
            return
        try:
            self._on_snapshot(self.snapshot())
        except Exception:
            self._log.exception("snapshot_consumer_failed")

    def _reload_workflow(self) -> None:
        path = self._workflow.path
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError as exc:
            self._report_reload_failure(f"workflow file unreadable: {exc}")
            return
        if mtime_ns == self._workflow.source_mtime_ns:
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

    def _report_reload_failure(self, message: str) -> None:
        self._config_error = message
        if message == self._reported_reload_error:
            return
        self._reported_reload_error = message
        self._log.error("workflow_reload_failed", path=str(self._workflow.path), error=message)

    async def _dispatch_candidates(self) -> int:
        try:
            issues = await self._adapter.fetch_issues_by_states(CANDIDATE_STATES)
        except GitHubError as exc:
            self._log.warning("candidates_fetch_failed", error=str(exc))
            return 0
        dispatched = 0
        for issue in sort_candidates(issues):
            if self._slots() <= 0:
                break
            if not issue.dispatchable or issue.state not in ACTIVE_STATES:
                continue
            if issue.id in self._running or issue.id in self._retries:
                continue
            attempt, resume_session_id = 1, None
            if issue.state is StateLabel.IN_PROGRESS:
                plan = self._resume_plan(issue)
                if plan is None:
                    continue
                attempt, resume_session_id = plan
            if await self._dispatch(issue, attempt=attempt, resume_session_id=resume_session_id):
                dispatched += 1
        return dispatched

    def _resume_plan(self, issue: Issue) -> tuple[int, str | None] | None:
        """How to dispatch an orphaned in_progress issue: resume, fresh, or not at all."""
        try:
            path = self._workspaces.path_for(issue.identifier)
        except AgentError as exc:
            self._log.warning(
                "dispatch_skipped",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                error=exc.message,
            )
            return None
        record = self._workspaces.read_session(path)
        if (
            record is not None
            and record.issue_number == issue.number
            and record.last_outcome in (None, "cancelled")
        ):
            return record.attempt, record.session_id
        return 1, None

    async def _dispatch(self, issue: Issue, *, attempt: int, resume_session_id: str | None) -> bool:
        rework = issue.state is StateLabel.REWORK
        if issue.state is not StateLabel.IN_PROGRESS:
            claimed = await actions.claim(self._adapter, self._bus, issue)
            if claimed is None:
                return False
            issue = claimed
        workflow = self._workflow
        entry = RunningEntry(
            issue=issue,
            attempt=attempt,
            rework=rework,
            resumed=resume_session_id is not None,
            run_id=new_run_id(self._now()),
            started_mono=self._clock(),
            started_at=self._now(),
            cancel=asyncio.Event(),
        )
        entry.task = asyncio.create_task(
            self._worker(entry, workflow, resume_session_id),
            name=f"issuebot-worker-{issue.number}",
        )
        entry.task.add_done_callback(
            lambda _task, issue_id=issue.id: self._queue.put_nowait(_WorkerExited(issue_id))
        )
        self._running[issue.id] = entry
        self._retries.pop(issue.id, None)
        self._counters = self._counters.bump(runs_started=1)
        self._log.info(
            "dispatched",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            attempt=attempt,
            rework=rework,
            resumed=entry.resumed,
            run_id=entry.run_id,
            slots_left=self._slots(),
        )
        return True

    async def _worker(
        self, entry: RunningEntry, workflow: Workflow, resume_session_id: str | None
    ) -> RunResult:
        return await self._run_session(
            entry.issue,
            workflow,
            self._adapter,
            self._bus,
            workspaces=self._workspaces,
            runner=self._runner_factory(workflow.config),
            attempt=entry.attempt,
            rework=entry.rework,
            resume_session_id=resume_session_id,
            cancel=entry.cancel,
            observer=RunObserver(entry, clock=self._clock, now=self._now),
            run_id=entry.run_id,
        )

    # --- reconcile ----------------------------------------------------------------------

    async def reconcile(self) -> None:
        """Symphony §8.5: stalls, then the label refresh; plus the terminal sweep on schedule."""
        self._check_stalls()
        if self._running:
            await self._refresh_running()
        if self._tick_count % TERMINAL_SWEEP_EVERY_TICKS == 0:
            await self.terminal_sweep()

    def _check_stalls(self) -> None:
        timeout_ms = self._workflow.config.claude.stall_timeout_ms
        if timeout_ms <= 0:
            return
        now = self._clock()
        for entry in self._running.values():
            if entry.stop_cause is not None:
                continue
            since = (
                entry.last_activity_mono
                if entry.last_activity_mono is not None
                else entry.started_mono
            )
            elapsed = now - since
            if elapsed * 1000 > timeout_ms:
                self._log.warning(
                    "reconcile_stalled",
                    issue_number=entry.issue.number,
                    issue_identifier=entry.identifier,
                    run_id=entry.run_id,
                    elapsed_s=round(elapsed),
                    stall_timeout_ms=timeout_ms,
                )
                entry.stop("stalled", f"no activity for {elapsed:.0f} s")

    async def _refresh_running(self) -> None:
        ids = list(self._running)
        try:
            refreshed = await self._adapter.fetch_issues_by_ids(ids)
        except GitHubError as exc:
            self._log.warning("reconcile_refresh_failed", error=str(exc))
            return
        by_id = {issue.id: issue for issue in refreshed}
        for issue_id in ids:
            entry = self._running.get(issue_id)
            if entry is None:
                continue
            current = by_id.get(issue_id)
            if current is None:
                self._stop_entry(entry, "missing", "issue no longer found")
                continue
            if current.github_state == "closed":
                entry.terminal_issue = current
                self._stop_entry(entry, "closed", "issue closed")
                continue
            for event in observe_transition(entry.issue, current):
                self._bus.publish(event)
            entry.issue = current
            if current.state is StateLabel.IN_PROGRESS and current.dispatchable:
                continue
            if current.state is StateLabel.REVIEW:
                if entry.review_seen_tick is None:
                    entry.review_seen_tick = self._tick_count
                    self._log.info(
                        "reconcile_review_grace",
                        issue_number=current.number,
                        issue_identifier=current.identifier,
                        run_id=entry.run_id,
                    )
                elif self._tick_count - entry.review_seen_tick >= REVIEW_GRACE_TICKS:
                    self._stop_entry(entry, "moved", "review")
                continue
            detail = current.state.value if current.state is not None else "unlabelled"
            self._stop_entry(entry, "moved", detail)

    def _stop_entry(self, entry: RunningEntry, cause: StopCause, detail: str) -> None:
        if entry.stop_cause is None:
            self._log.info(
                "reconcile_stop",
                issue_number=entry.issue.number,
                issue_identifier=entry.identifier,
                run_id=entry.run_id,
                cause=cause,
                detail=detail,
            )
        entry.stop(cause, detail)

    async def terminal_sweep(self) -> None:
        """Symphony §8.6, repeated: closed issues still carrying a state label."""
        try:
            issues = await self._adapter.fetch_terminal_issues()
        except GitHubError as exc:
            self._log.warning("terminal_sweep_failed", error=str(exc))
            return
        for issue in issues:
            if issue.id in self._running:
                continue
            self._retries.pop(issue.id, None)
            await self._finish(issue)

    async def _finish(self, issue: Issue) -> None:
        outcome = await actions.finish_terminal(self._adapter, self._bus, self._workspaces, issue)
        if outcome == "complete":
            self._counters = self._counters.bump(issues_completed=1)
        elif outcome == "cancelled":
            self._counters = self._counters.bump(issues_cancelled=1)

    # --- worker exits and retries -----------------------------------------------------

    async def handle_worker_exit(self, issue_id: str) -> None:
        """Symphony §16.6 with the blocked escape: totals, then retry, escape or release."""
        entry = self._running.pop(issue_id, None)
        if entry is None or entry.task is None:
            return
        task = entry.task
        self._counters = self._counters.bump(runs_ended=1)
        result: RunResult | None = None
        error: str | None = None
        if task.cancelled():
            error = "worker task cancelled"
            self._add_elapsed(entry)
        elif task.exception() is not None:
            exc = task.exception()
            self._log.error(
                "worker_crashed",
                issue_number=entry.issue.number,
                issue_identifier=entry.identifier,
                run_id=entry.run_id,
                error=str(exc),
                exc_info=exc,
            )
            error = f"worker crashed: {exc}"
            self._add_elapsed(entry)
        else:
            result = task.result()
            self._totals = self._totals.add(result)
        self._log.info(
            "worker_exited",
            issue_number=entry.issue.number,
            issue_identifier=entry.identifier,
            run_id=entry.run_id,
            attempt=entry.attempt,
            outcome=result.outcome if result is not None else None,
            stop_reason=result.stop_reason if result is not None else None,
            cause=entry.stop_cause,
            turns=result.turns if result is not None else entry.turns,
            cost_usd=result.cost_usd if result is not None else 0.0,
            error=error or (result.error if result is not None else None),
        )
        if entry.terminal_issue is not None:
            await self._finish(entry.terminal_issue)
            return
        if task.cancelled() or entry.stop_cause in ("moved", "missing", "shutdown"):
            self._log.info(
                "issue_released",
                issue_number=entry.issue.number,
                issue_identifier=entry.identifier,
                reason=entry.stop_cause or "task cancelled",
            )
            return
        if entry.stop_cause == "stalled":
            await self._after_failure(entry, f"stalled: {entry.stop_detail}", result)
            return
        if result is None:
            await self._after_failure(entry, error or "worker crashed", None)
            return
        if result.outcome == "succeeded":
            if result.stop_reason == "max_turns" and result.final_state is StateLabel.IN_PROGRESS:
                review = self._workflow.config.github.labels.review
                reason = (
                    f"Turn budget exhausted: {result.turns} turns in attempt {entry.attempt} "
                    f"without reaching `{review}`."
                )
                await self._escape(entry, reason, result)
                return
            final = result.final_issue
            if final is not None and final.github_state == "open":
                for event in observe_transition(entry.issue, final):
                    self._bus.publish(event)
            self._schedule(
                entry.issue,
                attempt=1,
                kind="continuation",
                delay_ms=CONTINUATION_DELAY_MS,
                error=None,
            )
            return
        await self._after_failure(entry, f"{result.error_category}: {result.error}", result)

    def _add_elapsed(self, entry: RunningEntry) -> None:
        elapsed = self._clock() - entry.started_mono
        self._totals = replace(
            self._totals, seconds_running=round(self._totals.seconds_running + elapsed, 3)
        )

    async def _after_failure(
        self, entry: RunningEntry, error: str, result: RunResult | None
    ) -> None:
        agent = self._workflow.config.agent
        if entry.attempt >= agent.max_attempts:
            reason = (
                f"{agent.max_attempts} consecutive worker sessions failed; last error: {error}."
            )
            await self._escape(entry, reason, result)
            return
        self._schedule(
            entry.issue,
            attempt=entry.attempt + 1,
            kind="failure",
            delay_ms=backoff_ms(entry.attempt + 1, agent.max_retry_backoff_ms),
            error=error,
        )

    async def _escape(self, entry: RunningEntry, reason: str, result: RunResult | None) -> None:
        context = BlockedContext(
            reason=reason,
            run_id=entry.run_id,
            attempt=entry.attempt,
            turns=result.turns if result is not None else entry.turns,
            log_dir=str(result.log_dir) if result is not None and result.log_dir else None,
        )
        outcome = await actions.blocked_escape(
            self._adapter, self._bus, entry.issue_id, context, now=self._now()
        )
        if outcome == "applied":
            self._counters = self._counters.bump(blocked=1)
        elif outcome == "failed":
            self._schedule(
                entry.issue,
                attempt=1,
                kind="escape",
                delay_ms=backoff_ms(1, self._workflow.config.agent.max_retry_backoff_ms),
                error="blocked escape failed",
                escape=context,
            )

    def _schedule(
        self,
        issue: Issue,
        *,
        attempt: int,
        kind: RetryKind,
        delay_ms: int,
        error: str | None,
        escape: BlockedContext | None = None,
    ) -> None:
        entry = RetryEntry(
            issue_id=issue.id,
            identifier=issue.identifier,
            issue_number=issue.number,
            issue_url=issue.url,
            attempt=attempt,
            kind=kind,
            due_mono=self._clock() + delay_ms / 1000,
            due_at=self._now() + timedelta(milliseconds=delay_ms),
            error=error,
            escape=escape,
        )
        self._retries[issue.id] = entry
        self._log.info(
            "retry_scheduled",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            kind=kind,
            attempt=attempt,
            due_in_ms=delay_ms,
            error=error,
        )

    def _requeue(self, entry: RetryEntry, *, delay_ms: int, error: str, kind: RetryKind) -> None:
        self._retries[entry.issue_id] = replace(
            entry,
            kind=kind,
            due_mono=self._clock() + delay_ms / 1000,
            due_at=self._now() + timedelta(milliseconds=delay_ms),
            error=error,
        )
        self._log.info(
            "retry_scheduled",
            issue_number=entry.issue_number,
            issue_identifier=entry.identifier,
            kind=kind,
            attempt=entry.attempt,
            due_in_ms=delay_ms,
            error=error,
        )

    def _release(self, entry: RetryEntry, reason: str) -> None:
        self._log.info(
            "retry_released",
            issue_number=entry.issue_number,
            issue_identifier=entry.identifier,
            kind=entry.kind,
            reason=reason,
        )

    def _earliest_due(self) -> float:
        return min((entry.due_mono for entry in self._retries.values()), default=math.inf)

    async def fire_due_retries(self) -> None:
        """Symphony §16.6's retry timer, for every entry whose time has come."""
        now = self._clock()
        due = sorted(
            (entry for entry in self._retries.values() if entry.due_mono <= now),
            key=lambda entry: entry.due_mono,
        )
        for entry in due:
            if self._stopping:
                return
            if self._retries.get(entry.issue_id) is not entry:
                continue
            del self._retries[entry.issue_id]
            await self._fire(entry)

    async def _fire(self, entry: RetryEntry) -> None:
        settings = self._workflow.config
        self._log.info(
            "retry_fired",
            issue_number=entry.issue_number,
            issue_identifier=entry.identifier,
            kind=entry.kind,
            attempt=entry.attempt,
        )
        if entry.kind == "escape" and entry.escape is not None:
            outcome = await actions.blocked_escape(
                self._adapter, self._bus, entry.issue_id, entry.escape, now=self._now()
            )
            if outcome == "applied":
                self._counters = self._counters.bump(blocked=1)
            elif outcome == "failed":
                self._requeue(
                    replace(entry, attempt=entry.attempt + 1),
                    kind="escape",
                    delay_ms=backoff_ms(entry.attempt + 1, settings.agent.max_retry_backoff_ms),
                    error="blocked escape failed",
                )
            return
        try:
            issues = await self._adapter.fetch_issues_by_ids([entry.issue_id])
        except GitHubError as exc:
            self._requeue(
                entry,
                kind=entry.kind,
                delay_ms=settings.polling.interval_ms,
                error=f"retry refresh failed: {exc.message}",
            )
            return
        if not issues:
            self._release(entry, "missing")
            return
        issue = issues[0]
        if issue.github_state == "closed":
            await self._finish(issue)
            return
        if not issue.dispatchable or issue.state not in ACTIVE_STATES:
            self._release(entry, "not_active")
            return
        if self._slots() <= 0:
            self._requeue(
                entry,
                kind="slots",
                delay_ms=settings.polling.interval_ms,
                error="no available orchestrator slots",
            )
            return
        attempt = entry.attempt if issue.state is StateLabel.IN_PROGRESS else 1
        await self._dispatch(issue, attempt=attempt, resume_session_id=None)

    # --- the loop -----------------------------------------------------------------------

    async def run(self) -> None:
        """Startup, then tick and wait until stopped; shutdown on the way out."""
        await self.startup()
        try:
            while not self._stopping:
                await self.tick()
                await self._wait_for_next_tick()
        finally:
            await self.shutdown()

    async def _wait_for_next_tick(self) -> None:
        deadline = self._clock() + self._workflow.config.polling.interval_ms / 1000
        while not self._stopping:
            await self.fire_due_retries()
            now = self._clock()
            if now >= deadline:
                return
            timeout = min(deadline, self._earliest_due()) - now
            if timeout <= 0:
                continue
            try:
                message = await asyncio.wait_for(self._queue.get(), timeout)
            except TimeoutError:
                continue
            if message is _REFRESH:
                self._refresh_pending = False
                return
            if message is _STOP:
                return
            if isinstance(message, _WorkerExited):
                await self.handle_worker_exit(message.issue_id)

    def request_refresh(self) -> None:
        """Ask for a tick now; coalesced while one is already pending."""
        if self._refresh_pending:
            return
        self._refresh_pending = True
        self._queue.put_nowait(_REFRESH)

    def request_stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._log.info("stop_requested")
        self._queue.put_nowait(_STOP)

    async def shutdown(self) -> None:
        """Cancel every worker, wait for after_run, drain the exits, drop the retries."""
        self._stopping = True
        settings = self._workflow.config
        if self._running:
            self._log.info("shutdown_started", running=len(self._running))
            tasks: list[asyncio.Task[RunResult]] = []
            for entry in self._running.values():
                entry.stop("shutdown", "worker stopping")
                if entry.task is not None:
                    tasks.append(entry.task)
            timeout = settings.hooks.timeout_ms / 1000 + TERMINATE_GRACE_S + SHUTDOWN_MARGIN_S
            _done, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                self._log.warning("shutdown_timeout", pending=len(pending), timeout_s=timeout)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            for issue_id in list(self._running):
                await self.handle_worker_exit(issue_id)
        self._retries.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
        counters = self._counters
        self._log.info(
            "orchestrator_stopped",
            runs_started=counters.runs_started,
            runs_ended=counters.runs_ended,
            issues_completed=counters.issues_completed,
            issues_cancelled=counters.issues_cancelled,
            blocked=counters.blocked,
            cost_usd=self._totals.cost_usd,
        )


def _changed_sections(old: Workflow, new: Workflow) -> list[str]:
    changed = [
        name
        for name in type(old.config).model_fields
        if getattr(old.config, name) != getattr(new.config, name)
    ]
    if old.prompt_template != new.prompt_template:
        changed.append("prompt")
    return changed
```

- [ ] **Step 4: Complete the package re-exports**

Replace `src/issuebot/orchestrator/__init__.py` with:

```python
"""Coordination: the poll loop, claims, dispatch, retries, reconciliation and recovery."""

from issuebot.orchestrator.actions import (
    CANCEL_REASON,
    EscapeOutcome,
    FinishOutcome,
    blocked_block,
    blocked_escape,
    claim,
    finish_terminal,
    remove_workspace,
)
from issuebot.orchestrator.orchestrator import (
    CANDIDATE_STATES,
    Orchestrator,
    OrchestratorStartupError,
    RunObserver,
    RunSessionFn,
    preflight,
)
from issuebot.orchestrator.state import (
    BACKOFF_BASE_MS,
    CONTINUATION_DELAY_MS,
    REVIEW_GRACE_TICKS,
    TERMINAL_SWEEP_EVERY_TICKS,
    BlockedContext,
    ClaudeTotals,
    Counters,
    RetryEntry,
    RetryKind,
    RetryRow,
    RunningEntry,
    RunningRow,
    RuntimeSnapshot,
    StopCause,
    backoff_ms,
    claimed_snapshot,
    observe_transition,
    pr_url,
    sort_candidates,
    state_label_name,
)

__all__ = [
    "BACKOFF_BASE_MS",
    "CANCEL_REASON",
    "CANDIDATE_STATES",
    "CONTINUATION_DELAY_MS",
    "REVIEW_GRACE_TICKS",
    "TERMINAL_SWEEP_EVERY_TICKS",
    "BlockedContext",
    "ClaudeTotals",
    "Counters",
    "EscapeOutcome",
    "FinishOutcome",
    "Orchestrator",
    "OrchestratorStartupError",
    "RetryEntry",
    "RetryKind",
    "RetryRow",
    "RunObserver",
    "RunSessionFn",
    "RunningEntry",
    "RunningRow",
    "RuntimeSnapshot",
    "StopCause",
    "backoff_ms",
    "blocked_block",
    "blocked_escape",
    "claim",
    "claimed_snapshot",
    "finish_terminal",
    "observe_transition",
    "pr_url",
    "preflight",
    "remove_workspace",
    "sort_candidates",
    "state_label_name",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator.py -q`
Expected: 50 passed, in about a second (the loop tests use a 1 s poll interval and stop early; the end-to-end run replays the fake `claude` in well under a second).

- [ ] **Step 6: Commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/orchestrator tests/test_orchestrator.py
git commit -m "feat: add the orchestrator loop, refresh, stop and shutdown"
```

---

### Task 7: `issuebot worker` and the compose service

**Files:**
- Modify: `src/issuebot/cli.py`, `compose.yaml`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `issuebot.orchestrator.{Orchestrator, OrchestratorStartupError}`; the existing CLI seams `_which`, `_adapter_factory`, `_run_session`.
- Produces: the `worker [--workflow PATH]` subcommand, `cmd_worker`, `_run_worker(workflow) -> int`, the seam `_orchestrator_factory = Orchestrator`; exit codes 2 (unloadable), 1 (startup failure, one `[FAIL] startup: <problem>` line each), 0 (clean stop).

Spec: §7 and §8.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py`, change the imports at the top to add `asyncio`, `signal` and `OrchestratorStartupError`:

```python
import asyncio
import os
import signal
import subprocess
import sys
```

and after the `issuebot.github` import line:

```python
from issuebot.orchestrator import OrchestratorStartupError
```

Then append:

```python
# --- worker --------------------------------------------------------------------------------


class StubOrchestrator:
    """Stands in for Orchestrator: records its construction and plays one scripted run()."""

    instances: ClassVar[list[StubOrchestrator]] = []
    next_problems: ClassVar[list[str] | None] = None
    next_sigterm: ClassVar[bool] = False

    def __init__(self, workflow: object, **kwargs: object) -> None:
        self.workflow = workflow
        self.kwargs = kwargs
        self.stops = 0
        StubOrchestrator.instances.append(self)

    def request_stop(self) -> None:
        self.stops += 1

    async def run(self) -> None:
        if StubOrchestrator.next_problems is not None:
            raise OrchestratorStartupError(StubOrchestrator.next_problems)
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
    stub_orchestrator.next_problems = ["'gh' not found on PATH", "labels missing: a"]
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out == ["[FAIL] startup: 'gh' not found on PATH", "[FAIL] startup: labels missing: a"]


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
    assert [sink.name for sink in kwargs["bus"].sinks] == ["log"]  # type: ignore[attr-defined]


def test_worker_requires_no_arguments(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["worker", "extra"])
    assert exc.value.code == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q -k worker`
Expected: `AttributeError: <module 'issuebot.cli'> has no attribute '_orchestrator_factory'` from the fixture, and `test_worker_requires_no_arguments` fails because argparse rejects `worker` as an unknown command (exit 2 for a different reason; it passes for the wrong reason, which is fine).

- [ ] **Step 3: Add the subcommand and the compose change**

Apply this diff to `src/issuebot/cli.py` and `compose.yaml`:

```diff
diff --git a/compose.yaml b/compose.yaml
index 721807f..7bea359 100644
--- a/compose.yaml
+++ b/compose.yaml
@@ -19,8 +19,13 @@ services:

   worker:
     build: .
-    # Phase 4 replaces this with ["worker"]. Until then `docker compose up` validates config.
-    command: ["validate"]
+    command: ["worker"]
+    # tini as PID 1 reaps grandchildren that outlive a killed claude.
+    init: true
+    restart: unless-stopped
+    # Longer than the orchestrator's shutdown wait (hooks.timeout_ms + 20 s), so Docker never
+    # SIGKILLs a worker that is still running after_run.
+    stop_grace_period: 120s
     env_file:
       - path: .env
         required: false
diff --git a/src/issuebot/cli.py b/src/issuebot/cli.py
index 9ffac85..45e0b0a 100644
--- a/src/issuebot/cli.py
+++ b/src/issuebot/cli.py
@@ -2,8 +2,10 @@

 import argparse
 import asyncio
+import contextlib
 import os
 import shutil
+import signal
 import subprocess
 from collections.abc import Callable, Mapping, Sequence
 from dataclasses import dataclass
@@ -38,6 +40,7 @@ from issuebot.events import EventBus, LogSink, StateChanged
 from issuebot.github import GhCliAdapter, GitHubAdapter, GitHubError, Issue, StateLabel
 from issuebot.github.normalise import repo_short_name
 from issuebot.log import LOG_LEVELS, configure_logging
+from issuebot.orchestrator import Orchestrator, OrchestratorStartupError

 DEFAULT_WORKFLOW = "WORKFLOW.md"

@@ -65,6 +68,7 @@ def _claude_version_output(command: str) -> str | None:

 _claude_version = _claude_version_output
 _run_session = run_session
+_orchestrator_factory = Orchestrator

 CheckStatus = Literal["ok", "warn", "fail"]
 _TAGS: dict[CheckStatus, str] = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
@@ -152,6 +156,12 @@ def build_parser() -> argparse.ArgumentParser:
         help="print the rendered first-turn prompt and exit without running anything",
     )
     run_once.set_defaults(func=cmd_run_once)
+
+    worker = subparsers.add_parser(
+        "worker", help="run the orchestrator until SIGTERM or SIGINT (the long-running service)"
+    )
+    _add_workflow_option(worker)
+    worker.set_defaults(func=cmd_worker)
     return parser


@@ -550,3 +560,40 @@ def render_run_summary(result: RunResult) -> str:
 def _duration(seconds: float) -> str:
     total = int(seconds)
     return f"{total // 60}m{total % 60:02d}s"
+
+
+# --- worker ----------------------------------------------------------------------------
+
+
+def cmd_worker(args: argparse.Namespace) -> int:
+    workflow = _load_or_report(args)
+    if workflow is None:
+        return 2
+    return asyncio.run(_run_worker(workflow))
+
+
+async def _run_worker(workflow: Workflow) -> int:
+    """Run the orchestrator until a stop signal; 1 when startup validation fails."""
+    orchestrator = _orchestrator_factory(
+        workflow,
+        bus=EventBus([LogSink()]),
+        adapter_factory=_adapter_factory,
+        run_session=_run_session,
+        which=_which,
+    )
+    loop = asyncio.get_running_loop()
+    signals = (signal.SIGTERM, signal.SIGINT)
+    for signum in signals:
+        with contextlib.suppress(NotImplementedError, RuntimeError):
+            loop.add_signal_handler(signum, orchestrator.request_stop)
+    try:
+        await orchestrator.run()
+    except OrchestratorStartupError as exc:
+        for problem in exc.problems:
+            print(f"[FAIL] startup: {problem}")
+        return 1
+    finally:
+        for signum in signals:
+            with contextlib.suppress(NotImplementedError, RuntimeError):
+                loop.remove_signal_handler(signum)
+    return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q`
Expected: 63 passed (the four new ones included). `docker compose config --services` still lists `db` and `worker`.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/cli.py compose.yaml tests/test_cli.py
git commit -m "feat: add issuebot worker and make the compose worker service real"
```

---

### Task 8: Dogfood `WORKFLOW.md`: unshallow hook and attempt wording

**Files:**
- Modify: `WORKFLOW.md`
- Test: `tests/test_workflow_default.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `hooks.after_create` in the dogfood workflow (a guarded `git fetch --unshallow`); the follow-up context block's first line reads `This is attempt {{ attempt }} for this issue: ...`.

Spec: §9. The unshallow is guarded with `git rev-parse --is-shallow-repository` because a `--depth 1` clone of a repository whose default branch has a single commit is not shallow at all, and `git fetch --unshallow` then fails with "--unshallow on a complete repository does not make sense", which would fail every workspace creation (the scratch repository's `main` has exactly one commit).

- [ ] **Step 1: Write the failing tests**

Apply this diff to `tests/test_workflow_default.py`:

```diff
diff --git a/tests/test_workflow_default.py b/tests/test_workflow_default.py
index cb97c73..3703586 100644
--- a/tests/test_workflow_default.py
+++ b/tests/test_workflow_default.py
@@ -97,7 +97,8 @@ def test_follow_up_and_rework_context(make_issue: Callable[..., Issue]) -> None:
         context(workflow, issue, attempt=2, rework=True)
     )
     assert "## Follow-up context" in text
-    assert "worker session #2" in text
+    assert "This is attempt 2 for this issue" in text
+    assert "worker session #" not in text
     assert "## Rework context" in text
     assert "The pull request is #51 (open)" in text
     assert "Linked pull request: #51 (open)" in text
@@ -118,3 +119,10 @@ def test_continuation_renders(make_issue: Callable[..., Issue]) -> None:
         context(workflow, dispatched(make_issue), turn_number=2)
     )
     assert "continuation turn 2 of 5" in text
+
+
+def test_after_create_unshallows_a_shallow_clone() -> None:
+    hook = load().config.hooks.after_create
+    assert hook is not None
+    assert "git rev-parse --is-shallow-repository" in hook
+    assert "git fetch --unshallow" in hook
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_workflow_default.py -q`
Expected: `test_follow_up_and_rework_context` fails on `"This is attempt 2 for this issue" in text`; `test_after_create_unshallows_a_shallow_clone` fails on `assert hook is not None`.

- [ ] **Step 3: Edit `WORKFLOW.md`**

Apply this diff (the Edit tool with the two exact old strings is the safest way):

```diff
diff --git a/WORKFLOW.md b/WORKFLOW.md
index dee9a7f..30c31e9 100644
--- a/WORKFLOW.md
+++ b/WORKFLOW.md
@@ -6,6 +6,11 @@ polling:
   interval_ms: 30000
 workspace:
   root: /workspaces
+hooks:
+  # The built-in clone is shallow; the self-review's `git diff origin/HEAD...HEAD` and a
+  # rework's merge of the default branch need the merge base.
+  after_create: |
+    if [ "$(git rev-parse --is-shallow-repository)" = true ]; then git fetch --unshallow; fi
 agent:
   max_concurrent_agents: 2
   max_turns: 5
@@ -26,7 +31,7 @@ You are working on GitHub issue `{{ issue.identifier }}` (#{{ issue.number }}) i
 {% if attempt > 1 %}
 ## Follow-up context

-- This is worker session #{{ attempt }} for this issue: a continuation, or a retry after a failure.
+- This is attempt {{ attempt }} for this issue: the previous worker session failed or was cut short, and issuebot dispatched a fresh session.
 - Resume from the current workspace, branch and workpad state instead of starting over.
 - Do not repeat investigation or validation the workpad already records unless new changes need it.
 - Do not end the turn while the issue is still labelled `{{ labels.in_progress }}` unless you are blocked by missing access.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_workflow_default.py tests/test_cli.py -q && uv run issuebot validate --show-config | head -30`
Expected: tests pass; `validate` still prints `12 checks` and the configuration shows `after_create` under `hooks` (the network checks may fail without a token; the count is what matters).

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add WORKFLOW.md tests/test_workflow_default.py
git commit -m "feat: unshallow dogfood workspaces and reword the retry context in WORKFLOW.md"
```

---

### Task 9: Documentation

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`, `docs/superpowers/specs/2026-09-03-phase-3-agent-runner-design.md`

Use the Edit tool with the exact old strings below.

- [ ] **Step 1: `CLAUDE.md`**

In the Commands block, replace

```
uv run issuebot run-once <number>    # one worker session in the foreground (--show-prompt renders only)
docker compose build                 # image: git, gh, claude, app venv
docker compose up                    # db (postgres:18) + worker (runs validate until Phase 4)
```

with

```
uv run issuebot run-once <number>    # one worker session in the foreground (--show-prompt renders only)
uv run issuebot worker               # the long-running orchestrator; SIGTERM or Ctrl-C stops it
docker compose build                 # image: git, gh, claude, app venv
docker compose up                    # db (postgres:18) + worker (issuebot worker)
```

In the Package layout list, after the `issuebot.agent` bullet (the one ending `Tests use tests/fakes/claude (replays tests/fixtures/claude/*.jsonl).`), insert:

```
- `issuebot.orchestrator`: one asyncio task owns the schedule. `state.py` (pure): `RunningEntry`,
  `RetryEntry`, `RuntimeSnapshot`, `backoff_ms` (`min(10000 * 2^(attempt-1), max_retry_backoff_ms)`,
  attempt being the one about to run), `sort_candidates` (orphaned `in_progress`, then `rework`,
  then `todo`, oldest first), `observe_transition` (agent for `in_progress`→`review`, human
  otherwise, plus `PrOpened`). `actions.py`: `claim`, `blocked_escape` (workpad block then
  `review`, idempotent per run id), `finish_terminal` (complete or cancelled, workspace removed).
  `orchestrator.py`: `Orchestrator.run()` = `startup()` (preflight, `auth_status`,
  `missing_labels`), then `tick()` (reconcile: stalls, running refresh with a one-tick grace for
  `review`, terminal sweep on the first and every tenth tick; mtime reload; preflight; fetch
  `in_progress`/`rework`/`todo`; dispatch while slots remain; snapshot) and a queue wait that
  fires retries (continuation 1 s; failure backoff; `escape`; `slots`) and handles worker exits
  (`max_turns` while `in_progress` or `max_attempts` failures → the blocked escape).
  `request_refresh()`, `request_stop()`, `snapshot()`; SIGTERM shutdown waits for `after_run`.
  Orphans resume from `session.json` when its `last_outcome` is `null` or `cancelled`; retries
  never resume. Tests drive `tick()`, `handle_worker_exit()` and `fire_due_retries()` directly
  with a fake clock and a scripted `run_session`.
```

In the `issuebot.cli` bullet, replace `run-once <number> [--show-prompt]` ... `never sets` `review`); with the same text plus the worker command: change

```
  `labels ensure`, `issues list`, `run-once <number> [--show-prompt]` (claims
  `in-progress`, runs one session, never sets `review`); exit codes 0/1/2 (ok / failed /
  workflow unloadable). Tests substitute `_which`, `_claude_version`, `_adapter_factory`
  and `_run_session`.
```

to

```
  `labels ensure`, `issues list`, `run-once <number> [--show-prompt]` (claims
  `in-progress`, runs one session, never sets `review`), `worker [--workflow PATH]` (the
  orchestrator until SIGTERM/SIGINT; `[FAIL] startup:` lines and exit 1 when the startup probes
  fail); exit codes 0/1/2 (ok / failed / workflow unloadable). Tests substitute `_which`,
  `_claude_version`, `_adapter_factory`, `_run_session` and `_orchestrator_factory`.
```

- [ ] **Step 2: `README.md`**

Replace

```
uv run issuebot run-once 42       # one agent session for issue #42, in the foreground
cp .env.example .env              # then fill in GH_TOKEN and Claude auth
docker compose up --build         # postgres:18 + worker
```

with (write this with the Edit tool; the middle line mentions the dot-env file and must not pass through a shell command)

```
uv run issuebot run-once 42       # one agent session for issue #42, in the foreground
uv run issuebot worker            # the long-running orchestrator; Ctrl-C stops it
cp .env.example .env              # then fill in GH_TOKEN and Claude auth
docker compose up --build         # postgres:18 + worker (issuebot worker)
```

- [ ] **Step 3: Roadmap**

In `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`, Phase 4 scope, replace

```
- Repository chore for the dogfooding milestone: install the Claude Code GitHub
  Action with an automated review prompt on pull-request events (needs an
  `ANTHROPIC_API_KEY` repository secret, which is the user's call). issuebot's code
  needs nothing for it; the prompt's feedback sweep and the continuation turns are
  what make the action's comments reach the agent (§2.6, layer 2).
```

with

```
- Repository chore for the dogfooding milestone: install the Claude Code GitHub
  Action with an automated review prompt on pull-request events (needs an
  `ANTHROPIC_API_KEY` repository secret, which is the user's call). issuebot's code
  needs nothing for it; the prompt's feedback sweep and the continuation turns are
  what make the action's comments reach the agent (§2.6, layer 2). Deferred on
  2026-09-03 (Phase 4 spec, decision 12): unguarded, a missing secret fails every
  pull-request check, which the agent's completion bar treats as blocking. It becomes
  a chore issue issuebot can take once it is dogfooding.
```

and in the "Later (not scheduled)" paragraph, after `would share one `.git` between concurrent agents.` append (same paragraph):

```
 A `since` filter on `fetch_terminal_issues`, so the terminal sweep stops re-reading every completed issue the repository has (Phase 4 bounds it to every tenth tick instead).
```

- [ ] **Step 4: Phase 3 spec amendments**

In `docs/superpowers/specs/2026-09-03-phase-3-agent-runner-design.md`:

After the paragraph that begins `**`create_or_reuse`.** `path_for(issue.identifier)`; if `path / ".git"` exists` (§5.3), insert a new paragraph:

```
*Amended by Phase 4 (spec §10):* reuse requires both `path/.git` and `path/.issuebot`;
`.issuebot` is created as the last creation step, after `after_create`, so a hook that
writes under it must `mkdir -p .issuebot` first; the root `mkdir`, the marker `mkdir`
and `remove()` raise `AgentError("workspace_error")` instead of a raw `OSError`.
```

After the §7.5 paragraph that ends `so a cancelled task never leaves a `claude` behind.`, insert:

```
*Amended by Phase 4 (spec §10):* the process group is SIGKILLed even when the leader has
already exited (a grandchild holding stdout no longer outlives the turn), and a `cancel`
event that is already set when `run_turn` starts returns `cancelled` without spawning,
creating the log directory or emitting any event.
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pre-commit run --all-files && uv run pytest -q`
Expected: hooks pass (re-`git add` any document the formatter rewrote); 500 passed.

```bash
git add CLAUDE.md README.md docs/superpowers/specs/2026-09-02-issuebot-phased-design.md docs/superpowers/specs/2026-09-03-phase-3-agent-runner-design.md
git commit -m "docs: describe issuebot.orchestrator, the worker command and the Phase 4 amendments"
```

---

### Task 10: Live check against `jleavers/issuebot-scratch`

**Files:** none in this repository. Everything here happens against GitHub and in directories outside every checkout. This task spends real Claude budget under the operator's subscription login (no `ANTHROPIC_API_KEY` exported) and creates real issues, branches and pull requests; that is intended. Never print `GH_TOKEN`. The executor never merges a pull request; Step 6 asks the operator to.

- [ ] **Step 1: Environment and the scratch workflow file**

Recreate `~/issuebot-scratch/WORKFLOW.md` from the repository's `WORKFLOW.md` (it must carry the Task 8 edits): copy it with `cp WORKFLOW.md ~/issuebot-scratch/WORKFLOW.md`, then with the Edit tool change `repo: jleavers/issuebot` to `repo: jleavers/issuebot-scratch` and `root: /workspaces` to `root: /home/jleavers/issuebot-workspaces`. Then:

```bash
export GH_TOKEN=$(gh auth token) && unset ANTHROPIC_API_KEY && claude --version && uv run issuebot validate --workflow ~/issuebot-scratch/WORKFLOW.md && uv run issuebot issues list --workflow ~/issuebot-scratch/WORKFLOW.md
```

Expected: `12 checks: 0 failed, 0 warnings`; the issue table shows `#1` in `review` with `#2 open`.

- [ ] **Step 2: Start the worker**

Run in the background (the harness's Bash tool with `run_in_background`, or `nohup ... &` in a plain shell), logging to a file:

```bash
export GH_TOKEN=$(gh auth token) && unset ANTHROPIC_API_KEY && cd /home/jleavers/_dev/issuebot && uv run issuebot --log-format console worker --workflow ~/issuebot-scratch/WORKFLOW.md >> ~/issuebot-scratch/worker.log 2>&1 & echo $! > ~/issuebot-scratch/worker.pid
```

Then `sleep 5 && tail -20 ~/issuebot-scratch/worker.log`.
Expected: `orchestrator_started` with `repo=jleavers/issuebot-scratch`, then nothing dispatched (issue #1 is in `review`; no `dispatched` line). `issue_finished` must not appear either (PR #2 is still open).

- [ ] **Step 3: File a second trivial issue**

Write `~/issuebot-scratch/issue-multiply.md` with the Write tool:

```markdown
Add a `multiply(a: int, b: int) -> int` function to `src/scratch/__init__.py` next to `add` and `subtract`, returning `a * b`.

## Acceptance criteria

- `multiply(3, 4) == 12` and `multiply(-2, 5) == -10`.
- A test in `tests/test_scratch.py` covers both cases.
- `uv run pytest -q` passes.
```

```bash
gh issue create -R jleavers/issuebot-scratch --title "Add a multiply function" --body-file ~/issuebot-scratch/issue-multiply.md --label issuebot/todo
```

Note the number (`N` below).

- [ ] **Step 4: Watch the run**

Poll the log every 30 s (`tail -5 ~/issuebot-scratch/worker.log`) for up to ten minutes. Expected sequence for issue N: `state_changed` (`actor=issuebot`, to `issuebot/in-progress`), `dispatched` (`attempt=1 rework=False resumed=False`), `run_started`, `claude_turn_started`, ..., `state_changed` with `actor=agent` and `to_label=issuebot/review`, `run_ended` with `outcome=succeeded`, `worker_exited` with `stop_reason=issue_moved`, `retry_scheduled kind=continuation`, `retry_fired`, `retry_released reason=not_active`. Then:

```bash
gh issue view N -R jleavers/issuebot-scratch --json labels,state --jq '{labels: [.labels[].name], state}' && gh pr list -R jleavers/issuebot-scratch --json number,headRefName,body --jq '.[] | {number, headRefName, closes: (.body | test("Closes #N"))}' && gh api repos/jleavers/issuebot-scratch/issues/N/comments --jq '.[] | select(.body | startswith("## Issuebot Workpad")) | .id'
```

Expected: labels `["issuebot/review"]`, an open PR from `issuebot/N-...` whose body closes N, one workpad comment. If the run ends `max_turns` instead, the worker applies the blocked escape: the issue still reaches `review`, the workpad gains an `### Issuebot blocked` block and the log shows `blocked_escape_applied`; record that and read `turn-*.jsonl` under `~/issuebot-workspaces/issuebot-scratch-N/.issuebot/runs/` to see why the agent did not set the label itself.

- [ ] **Step 5: Merged pull request → `complete` (operator action)**

Ask the operator to merge PR #2 on GitHub (the executor never merges). Then either wait for the next terminal sweep (every tenth tick, five minutes at the 30 s interval) or force one with a restart: `kill -TERM $(cat ~/issuebot-scratch/worker.pid)`, wait for `orchestrator_stopped`, and start the worker again as in Step 2 (its first tick sweeps). Expected in the log: `issue_finished outcome=complete` for issue 1, `issue_completed`, `workspace_removed` for `issuebot-scratch-1`. Verify:

```bash
gh issue view 1 -R jleavers/issuebot-scratch --json labels,state --jq '{labels: [.labels[].name], state}' && ls ~/issuebot-workspaces
```

Expected: `{"labels": ["issuebot/complete"], "state": "CLOSED"}` and no `issuebot-scratch-1` directory.

- [ ] **Step 6: SIGTERM during a run, then resume**

File a third issue the same way (`Add a divide function` returning `a // b`, with two test cases; number `M`), wait for `dispatched` for M in the log, wait one more minute so the agent is inside its turn, then:

```bash
kill -TERM $(cat ~/issuebot-scratch/worker.pid) && sleep 20 && tail -15 ~/issuebot-scratch/worker.log && cat ~/issuebot-workspaces/issuebot-scratch-M/.issuebot/session.json && gh issue view M -R jleavers/issuebot-scratch --json labels --jq '[.labels[].name]'
```

Expected: `stop_requested`, `shutdown_started running=1`, `claude_turn_finished` with `error_category=cancelled`, `hook_finished hook=after_run` is absent (the scratch workflow configures no `after_run`; `run_ended outcome=cancelled` is the marker that `run_session` completed its exit path), `worker_exited cause=shutdown`, `issue_released reason=shutdown`, `orchestrator_stopped`; `session.json` shows `"last_outcome": "cancelled"`; the label is still `issuebot/in-progress`. Start the worker again (Step 2) and watch: `dispatched ... attempt=1 resumed=True` for M, `claude_turn_started` with `--resume` in the argv, and the run continues to `review` as in Step 4 (the continuation prompt tells the agent to pick up from the workpad). If the resume fails with `process_exit` (transcript not found), the log shows a `retry_scheduled kind=failure attempt=2` and, 20 s later, a fresh attempt; record which path happened.

- [ ] **Step 7: Stop and report**

```bash
kill -TERM $(cat ~/issuebot-scratch/worker.pid) && sleep 3 && tail -3 ~/issuebot-scratch/worker.log && uv run issuebot issues list --workflow ~/issuebot-scratch/WORKFLOW.md
```

Expected: `orchestrator_stopped` within a second; issues N and M in `review`, issue 1 absent (closed). Paste the relevant log lines from Steps 2, 4, 5 and 6 and the `gh` outputs into the report for the PR body. Do not merge N's or M's pull requests.

---

### Task 11: Push the branch and open the pull request

**Files:** none.

- [ ] **Step 1: Final full check**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files && uv run pytest -q`
Expected: everything passes; `git status --short` is empty.

- [ ] **Step 2: Push the branch**

Run: `git push -u origin phase-4-orchestrator`

- [ ] **Step 3: Write the PR body to a file under the session scratchpad directory (a separate call from Step 4; use the Write tool)**

`<scratchpad>/issuebot-phase-4-pr.md`:

```markdown
## Phase 4: Orchestrator

Implements `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md`.

- `issuebot.orchestrator`: one asyncio task owns the schedule; workers are child tasks around the frozen `run_session`; a queue of worker exits and a deadline-driven wait; every timing rule through an injectable clock
- Tick: reconcile (stall detection over the whole run, label refresh with a one-tick grace for `review`, terminal sweep on the first and every tenth tick), mtime reload with last-good fallback, local preflight, fetch `in_progress`/`rework`/`todo`, sort (orphans, rework, todo; oldest first), dispatch while slots remain, snapshot
- Dispatch claims `in_progress` first (a failed claim aborts); orphans resume from `session.json` when its last outcome is `null` or `cancelled`
- Retries: continuation after 1 s; failure backoff `min(10000 * 2^(attempt-1), max_retry_backoff_ms)` on the attempt about to run; slot requeues keep the attempt; `max_attempts` or `max_turns` while still `in_progress` → the blocked escape (dated block appended to the workpad, then `review`, `Blocked` event; idempotent per run id; retried with backoff on failure)
- Human moves out of `in_progress` stop the worker without cleanup; a closed issue completes (merged PR) or is cancelled (labels stripped) once the worker has exited, and its workspace is removed
- SIGTERM/SIGINT shutdown cancels workers, waits for `after_run`, leaves issues `in_progress` for the restart to resume
- `RuntimeSnapshot` (`/api/v1/state` shape) via `snapshot()` and an `on_snapshot` hook; `request_refresh()` for Phase 6's `LISTEN`
- CLI `issuebot worker [--workflow PATH]`; compose `worker` runs it with `init`, `restart: unless-stopped`, `stop_grace_period: 120s`
- Phase 3 hardening: reuse requires `.git` and `.issuebot` (created last); `remove()` and the mkdirs raise `workspace_error`; `_terminate` kills the group after the leader exited; a pre-set cancel spawns nothing
- Dogfood `WORKFLOW.md`: guarded `git fetch --unshallow` in `after_create`; the follow-up block now describes a retry attempt
- The Claude Code GitHub Action for PR review is deferred (spec decision 12)
- Live check against `jleavers/issuebot-scratch`: a `todo` issue reached `review` with a PR through the worker, merging PR #2 turned issue #1 `complete` and removed its workspace, SIGTERM during a run left the issue `in_progress` and the restart resumed it (output below)

Database, dashboard and Slack are Phases 5 to 7.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Append the live-check output from Task 10 under a `## Live check` heading before the generated-with line, and the session link the executing harness requires after it.

- [ ] **Step 4: Open the PR via the REST API (the CLI's `pr create` is blocked in this repo)**

```bash
gh api repos/jleavers/issuebot/pulls -X POST \
  -f title='Phase 4: Orchestrator' \
  -f head='phase-4-orchestrator' -f base='main' \
  -F body=@<scratchpad>/issuebot-phase-4-pr.md
```

Then confirm with `gh pr view --json title,body --jq '.title'` and watch CI with `gh pr checks --watch`. CI must be green before handing over for human review. Do not merge.

---

## Acceptance criteria

Spec §13, restated for the executor:

- `uv run pytest -q` passes with no network (500 tests after Task 9); ruff and pre-commit clean; CI green; `docker compose build` succeeds.
- `uv run issuebot validate` still reports twelve checks for the committed `WORKFLOW.md`.
- The live check (Task 10) shows: a `todo` issue reaching `review` with a pull request through the worker, the agent-observed `state_changed actor=agent` in the log, a merged pull request turning its issue `complete` with the workspace removed, and a SIGTERM during a run followed by a resume (`resumed=True`) on restart.
- `CLAUDE.md`, `README.md`, the roadmap and the Phase 3 spec carry the Task 9 edits.
