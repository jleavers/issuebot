# Blocked Escape Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a turn's final message begins `BLOCKED: <one line>`, the session stops with `stop_reason == "blocked"` carrying that line, and the orchestrator escapes the issue at the end of that turn with the agent's own reason instead of after `max_turns`.

**Architecture:** A pure `blocker_from(result_text)` in `issuebot.agent.session` reads the marker off the first non-empty line; `_run_turns` checks it after the issue-moved check and before the `max_turns` check; `RunResult` carries the line in a new `blocker` field. `handle_worker_exit` gains one branch beside the `max_turns` one that calls the existing `_escape` with the line. The prompt names the marker in ground rule 2 and the completion bar; `run-once` prints it.

**Tech Stack:** Python 3.14, `uv`, pytest (hermetic), `FakeGitHub`, the scripted runners the session and orchestrator tests already use, ruff + pre-commit.

**Spec:** `docs/superpowers/specs/2026-09-13-blocked-escape-design.md`

## Global Constraints

- Run everything with `uv run ...`; tests `uv run pytest`, lint `uv run ruff check . && uv run ruff format --check .`, and `uv run pre-commit run --all-files` must pass before each commit (the git hook runs it anyway).
- Work on the branch `issuebot/blocked-escape` (exists, holds the spec). Never push to `main`; open the PR through `gh api repos/jleavers/issuebot/pulls -X POST ... -F body=@file.md` (see `CLAUDE.md`, "Creating PRs"), never `gh pr create`.
- Every commit message ends with the two attribution lines given in the session's system reminder (`Co-Authored-By:` and `Claude-Session:`).
- The marker is exactly `BLOCKED:` (case-sensitive), matched on the first non-empty line of the turn's `result_text` only; the reason is the text after it, stripped; an empty reason reads as no marker.
- The new stop reason is exactly `"blocked"`; the outcome stays `"succeeded"`; `RunResult.blocker` defaults to `None`.
- The check order after a turn is: cancelled, issue missing, issue moved, blocked, max_turns. A failed turn never reaches the blocked check.
- No retry after a blocked escape. No new event kind, no `RunEnded` field, no `runs` column, no setting, no migration.
- `tests/fixtures/runs/` is byte-for-byte and never touched.

---

## File map

| File | Change |
|---|---|
| `src/issuebot/agent/session.py` | `BLOCKED_MARKER`, `blocker_from`; `StopReason` + `"blocked"`; `RunResult.blocker`; `_State.blocker`; the check in `_run_turns`; `blocker` on the `run_finished` log line |
| `src/issuebot/agent/__init__.py` | add `BLOCKED_MARKER` and `blocker_from` to the `issuebot.agent.session` import and to `__all__` (it already re-exports `RunResult`, `StopReason`, `new_run_id`, `run_session`) |
| `src/issuebot/orchestrator/orchestrator.py` | the `blocked` branch in `handle_worker_exit` |
| `src/issuebot/cli.py` | the `run-once` summary line for `blocked` |
| `configs/WORKFLOW.md` | ground rule 2; the completion bar's closing sentence |
| `CLAUDE.md`, `README.md` | one sentence each |
| Tests | `test_agent_session.py`, `test_orchestrator.py`, `test_cli.py`, `test_workflow_default.py` |

---

### Task 1: The session stops on the marker

**Files:**
- Modify: `src/issuebot/agent/session.py:20` (`StopReason`), `:24-45` (`RunResult`), `:53-70` (`_State`), `:335-347` (the post-turn checks in `_run_turns`), `:199-206` (`run_finished`)
- Test: `tests/test_agent_session.py`

**Interfaces:**
- Produces: `BLOCKED_MARKER: str = "BLOCKED:"` and `blocker_from(result_text: str | None) -> str | None` in `issuebot.agent.session`; `StopReason` includes `"blocked"`; `RunResult.blocker: str | None = None`.

- [ ] **Step 1: Write the failing pure tests**

In `tests/test_agent_session.py`, extend the `from issuebot.agent.session import ...` line with `BLOCKED_MARKER, blocker_from` (alphabetical: `BLOCKED_MARKER, RunResult, blocker_from, new_run_id, run_session`). Add after `test_new_run_id_is_sortable_and_unique`:

```python
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "BLOCKED: gh cannot reach api.github.com; a human must fix DNS",
            "gh cannot reach api.github.com; a human must fix DNS",
        ),
        ("BLOCKED:no space after the colon", "no space after the colon"),
        (
            "\n\n  BLOCKED: after blank lines and indentation  \nmore",
            "after blank lines and indentation",
        ),
        ("Done. BLOCKED: mentioned later on the first line", None),
        ("Finished the PR.\nBLOCKED: only on the second line", None),
        ("BLOCKED:", None),
        ("BLOCKED:   ", None),
        ("blocked: lower case is not the marker", None),
        ("", None),
        (None, None),
    ],
)
def test_blocker_from_reads_the_marker_off_the_first_line(
    text: str | None, expected: str | None
) -> None:
    assert BLOCKED_MARKER == "BLOCKED:"
    assert blocker_from(text) == expected
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_agent_session.py -k blocker_from -v`
Expected: FAIL at import with `ImportError: cannot import name 'BLOCKED_MARKER'`.

- [ ] **Step 3: Write the pure piece and the field**

In `src/issuebot/agent/session.py`, change `StopReason` to:

```python
StopReason = Literal["issue_moved", "max_turns", "issue_missing", "failure", "cancelled", "blocked"]
```

and directly below it:

```python
# The first line of a blocked turn's final message (prompt ground rule 2). Read by the session,
# so the escape happens at the end of that turn rather than after max_turns.
BLOCKED_MARKER = "BLOCKED:"


def blocker_from(result_text: str | None) -> str | None:
    """The blocker line's reason when the turn's final message begins with the marker.

    Only the first non-empty line counts, and only when it starts with the marker: a message
    that mentions the word later is a report, not a stop. An empty reason reads as no marker,
    so a bare ``BLOCKED:`` cannot escape an issue with an empty block.
    """
    if not result_text:
        return None
    for line in result_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith(BLOCKED_MARKER):
            return None
        reason = stripped[len(BLOCKED_MARKER) :].strip()
        return reason or None
    return None
```

In `RunResult`, after `log_dir: Path | None`:

```
    # The reason after ``BLOCKED:`` on a blocked turn's final message; None for every other stop.
    blocker: str | None = None
```

In `_State`, after `error: str | None = None`:

```
    blocker: str | None = None
```

and in `_State.result()`, after `log_dir=self.log_dir,`:

```
            blocker=self.blocker,
```

- [ ] **Step 4: Run the pure tests**

Run: `uv run pytest tests/test_agent_session.py -k blocker_from -v`
Expected: PASS, all ten cases.

- [ ] **Step 5: Write the failing session tests**

`ScriptedRunner` returns `result_text="done"` for every turn. Give it a per-turn override: change its `__init__` signature to

```
    def __init__(
        self,
        *outcomes: str,
        on_turn: Callable[[int], None] | None = None,
        texts: dict[int, str] | None = None,
    ) -> None:
        self.script = list(outcomes)
        self.on_turn = on_turn
        self.texts = texts or {}
        self.calls: list[dict[str, object]] = []
```

and in `run_turn` change `result_text="done",` to `result_text=self.texts.get(turn_number, "done"),`.

Then add after `test_runs_until_max_turns_with_continuation_prompts`:

```python
async def test_a_blocked_final_message_stops_the_run_at_that_turn(tmp_path: Path) -> None:
    """Ground rule 2's marker: the session stops after the turn that carries it, so the
    orchestrator can escape at once instead of burning the turn budget re-checking."""
    h = Harness(tmp_path, max_turns=3)
    line = "BLOCKED: `gh` cannot reach api.github.com from this network; a human must fix DNS"
    runner = ScriptedRunner(texts={1: line + "\n\nThe workpad's Blockers section has the brief."})
    result = await h.run(runner, run_id="run-b")
    assert result.stop_reason == "blocked"
    assert result.outcome == "succeeded"
    assert result.error_category is None
    assert (
        result.blocker == "`gh` cannot reach api.github.com from this network; a human must fix DNS"
    )
    assert result.turns == 1
    assert result.final_state is StateLabel.IN_PROGRESS
    assert [call["turn_number"] for call in runner.calls] == [1]
    ended = h.recorder.events[-1]
    assert isinstance(ended, RunEnded)
    assert (ended.outcome, ended.error, ended.turns) == ("succeeded", None, 1)


async def test_a_marker_later_in_the_message_does_not_stop_the_run(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_turns=2)
    runner = ScriptedRunner(texts={1: "Pushed the fix.\nBLOCKED: is a word I used in a note."})
    result = await h.run(runner)
    assert result.stop_reason == "max_turns"
    assert result.blocker is None
    assert result.turns == 2


async def test_a_moved_issue_wins_over_the_marker(tmp_path: Path) -> None:
    """The label is the truth: an agent that handed off and also wrote the marker is done."""
    h = Harness(tmp_path, max_turns=3)
    runner = ScriptedRunner(
        on_turn=lambda _: h.github.human_set_state(42, StateLabel.REVIEW),
        texts={1: "BLOCKED: written by mistake after the hand-off"},
    )
    result = await h.run(runner)
    assert result.stop_reason == "issue_moved"
    assert result.blocker is None
    assert result.final_state is StateLabel.REVIEW


async def test_a_failed_turn_with_the_marker_still_fails(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_turns=3)
    runner = ScriptedRunner("process_exit", texts={1: "BLOCKED: the process died anyway"})
    result = await h.run(runner)
    assert result.outcome == "failed"
    assert result.stop_reason == "failure"
    assert result.error_category == "process_exit"
    assert result.blocker is None
```

- [ ] **Step 6: Run to verify they fail**

Run: `uv run pytest tests/test_agent_session.py -k "blocked_final_message or marker_later or wins_over_the_marker or with_the_marker" -v`
Expected: the first test FAILS (`stop_reason == "max_turns"`, three turns); the other three PASS already (they assert today's behaviour plus `blocker is None`, which the field's default satisfies). That is fine: they pin the order once the check exists.

- [ ] **Step 7: Add the check and the log field**

In `_run_turns`, between the `issue_moved` check and the `max_turns` check, insert:

```
        blocker = blocker_from(turn.result_text)
        if blocker is not None:
            state.blocker = blocker
            state.stop("blocked")
            return
```

so the tail of the loop reads:

```
        if state.issue.state is not StateLabel.IN_PROGRESS or not state.issue.dispatchable:
            state.stop("issue_moved")
            return
        blocker = blocker_from(turn.result_text)
        if blocker is not None:
            state.blocker = blocker
            state.stop("blocked")
            return
        if turn_number == max_turns:
            state.stop("max_turns")
            return
```

In `run_session`'s `run_finished` log call, add `blocker=result.blocker,` after `error=_error_text(result),`.

In `src/issuebot/agent/__init__.py`, the line `from issuebot.agent.session import RunResult, StopReason, new_run_id, run_session` gains `BLOCKED_MARKER` and `blocker_from` (keep it sorted the way ruff's isort wants: constants first, then classes, then functions), and `__all__` gains `"BLOCKED_MARKER"` and `"blocker_from"` in their alphabetical places.

- [ ] **Step 8: Run the session tests, then the suite and lint**

Run: `uv run pytest tests/test_agent_session.py -v && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS, lint clean. If ruff reformats `StopReason` onto several lines, accept it.

- [ ] **Step 9: Commit**

```bash
git add src/issuebot/agent tests/test_agent_session.py
git commit -F - <<'EOF'
feat: a turn whose final message begins BLOCKED: stops the session

blocker_from reads the marker off the first non-empty line of the turn's
result text; the session stops with stop_reason "blocked", outcome
"succeeded", and the line in RunResult.blocker. The check runs after
issue_moved (the label is the truth) and before max_turns.

<attribution lines>
EOF
```

---

### Task 2: The orchestrator escapes at once

**Files:**
- Modify: `src/issuebot/orchestrator/orchestrator.py:1057-1065` (the succeeded branch of `handle_worker_exit`)
- Test: `tests/test_orchestrator.py` (after `test_max_turns_while_in_progress_escapes_at_once`)

**Interfaces:**
- Consumes: `RunResult.stop_reason == "blocked"`, `RunResult.blocker` (Task 1).

- [ ] **Step 1: Write the failing tests**

In `tests/test_orchestrator.py`, after `test_max_turns_while_in_progress_escapes_at_once`:

```python
async def test_a_blocked_stop_while_in_progress_escapes_with_the_agents_reason(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.github.comment(1, f"{WORKPAD_MARKER}\n\n### Plan\n")
    reason = "GitHub Actions is not allocating runners; a human must clear the billing hold."
    await h.exit(
        h.run_for(1),
        stop_reason="blocked",
        blocker=reason,
        final_state=StateLabel.IN_PROGRESS,
        final_issue=h.github.issue(1),
        turns=1,
    )
    assert h.orchestrator.retries == {}
    assert h.orchestrator.running == {}
    assert h.github.issue(1).state is StateLabel.REVIEW
    body = h.github.comments_for(1)[0].body
    assert "### Issuebot blocked (" in body
    assert f"\n\n{reason}\n" in body
    assert "(attempt 1, 1 turn)" in body
    assert "Turn budget" not in body
    assert h.recorder.kinds == ["state_changed", "state_changed", "blocked"]
    assert h.recorder.of(Blocked)[0].reason == reason
    assert h.recorder.of(StateChanged)[1].actor == "issuebot"
    assert h.orchestrator.snapshot().counters.blocked == 1


async def test_a_blocked_stop_without_a_line_still_escapes(tmp_path: Path) -> None:
    """Defensive: the session never produces this pair, but the escape must not write None."""
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(
        h.run_for(1),
        stop_reason="blocked",
        blocker=None,
        final_state=StateLabel.IN_PROGRESS,
        final_issue=h.github.issue(1),
    )
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.recorder.of(Blocked)[0].reason == "the session reported a blocker"


async def test_a_blocked_stop_after_the_label_moved_is_released(tmp_path: Path) -> None:
    """The label is the truth: if the agent handed off, there is nothing to escape."""
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.exit(
        h.run_for(1),
        stop_reason="blocked",
        blocker="written after the hand-off",
        final_state=StateLabel.REVIEW,
        final_issue=h.github.issue(1),
    )
    assert h.github.comments_for(1) == []
    assert h.recorder.of(Blocked) == []
    assert h.github.issue(1).state is StateLabel.REVIEW
    await h.fire(1.0)  # the continuation retry a succeeded exit queues; review releases it
    assert h.orchestrator.retries == {}
    assert h.orchestrator.running == {}
```

`Blocked` and `StateChanged` are already imported from `issuebot.events` in this file (check the import block; add `Blocked` if it is missing).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py -k "blocked_stop" -v`
Expected: the first two FAIL (the issue stays `in_progress`, a continuation retry is queued); the third PASSES already.

- [ ] **Step 3: Add the branch**

In `handle_worker_exit`, directly after the `max_turns` block (the `await self._escape(entry, reason, result); return` lines) and before `self._schedule(...)`:

```
            if result.stop_reason == "blocked" and result.final_state is StateLabel.IN_PROGRESS:
                # The agent said so on its final line (spec 2026-09-13-blocked-escape-design.md);
                # an external blocker does not clear by retrying, so escape now.
                reason = result.blocker or "the session reported a blocker"
                await self._escape(entry, reason, result)
                return
```

- [ ] **Step 4: Run the orchestrator tests, then the suite and lint**

Run: `uv run pytest tests/test_orchestrator.py -k "blocked_stop or max_turns_while" -v && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/orchestrator/orchestrator.py tests/test_orchestrator.py
git commit -F - <<'EOF'
feat: a blocked stop while in progress takes the escape at once

<attribution lines>
EOF
```

---

### Task 3: Prompt, `run-once` and documentation

**Files:**
- Modify: `configs/WORKFLOW.md:80` (ground rule 2), `:235` (the completion bar's closing sentence)
- Modify: `src/issuebot/cli.py:976-980` (the `run-once` summary)
- Modify: `CLAUDE.md:180-182` (the `run_session` clause in `issuebot.agent`), `:249-250` (the exit sentence in `issuebot.orchestrator`)
- Modify: `README.md:240` (a new paragraph before "A credential that stops working *after* startup")
- Test: `tests/test_workflow_default.py`, `tests/test_cli.py`

- [ ] **Step 1: Write the failing prompt test**

In `tests/test_workflow_default.py`, after `test_a_run_that_never_executed_does_not_hold_the_issue`:

```python
def test_a_blocked_turn_marks_its_final_message(make_issue: Callable[..., Issue]) -> None:
    """The session reads the marker off the final message, so the prompt must name it in both
    places the agent decides to stop: ground rule 2 and the completion bar."""
    workflow = load()
    text = PromptRenderer(workflow.prompt_template).render(
        context(workflow, dispatched(make_issue, linked_pr=PR))
    )
    assert text.count("`BLOCKED: <one line") == 2
    assert "the first line of your final message; issuebot escalates the issue at once" in text
    assert "do not spend further turns re-checking the same blocker" in text
    assert "issuebot will escalate" not in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_workflow_default.py -k marks_its_final_message -v`
Expected: FAIL on the `count(...) == 2` assertion.

- [ ] **Step 3: Edit the prompt**

In `configs/WORKFLOW.md`, ground rule 2 currently ends `... Record what is missing and the exact human action needed in the workpad, then end the turn. An issue whose reported defect no longer happens ...`. Change it to:

```
2. Stop early only for a true external blocker: a required tool, credential or permission that is missing and cannot be obtained in-session. Record what is missing and the exact human action needed in the workpad, make `BLOCKED: <one line: what is missing and the exact human action>` the first line of your final message; issuebot escalates the issue at once, so do not spend further turns re-checking the same blocker. An issue whose reported defect no longer happens is not a blocker and not a failure: it is the No fault found outcome below.
```

The completion bar's closing line currently reads `Only then run the label command from the Labels section. If the bar cannot be met because of a true external blocker, write the blocker brief in the workpad instead and end the turn; issuebot will escalate.`. Change it to:

```
Only then run the label command from the Labels section. If the bar cannot be met because of a true external blocker, write the blocker brief in the workpad, make `BLOCKED: <one line>` the first line of your final message and end the turn; issuebot escalates at once.
```

- [ ] **Step 4: Run the prompt tests**

Run: `uv run pytest tests/test_workflow_default.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing `run-once` test**

In `tests/test_cli.py`, `StubSession.__init__` gains `self.blocker: str | None = None`, and its `RunResult(...)` call gains `blocker=self.blocker,` after `log_dir=...`. Then add after the `max_turns` run-once test (the one asserting `"turn budget exhausted; issue #42 remains in_progress (the blocked escape is Phase 4)"`), copying its fixture list:

```python
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
```

- [ ] **Step 6: Run to verify it fails**

Run: `uv run pytest tests/test_cli.py -k reports_a_blocked_stop -v`
Expected: FAIL on the second assertion (the summary has no blocked line).

- [ ] **Step 7: Add the summary line**

In `src/issuebot/cli.py`, the run-once summary's `if result.stop_reason == "max_turns": ... elif result.error_category is not None:` chain gains a branch between them:

```
    elif result.stop_reason == "blocked":
        lines.append(
            f"blocked: {result.blocker or 'no reason given'}; issue #{result.issue_number} "
            f"remains {state} (the worker would escalate it)"
        )
```

- [ ] **Step 8: Run the cli tests**

Run: `uv run pytest tests/test_cli.py -k "run_once" -v`
Expected: PASS.

- [ ] **Step 9: Update the docs**

`CLAUDE.md`, `issuebot.agent` paragraph: the clause `` `run_session` (turns, refresh between turns, `RunResult`, publishes `RunStarted`/`RunEnded`); `` becomes `` `run_session` (turns, refresh between turns, `RunResult`, publishes `RunStarted`/`RunEnded`; a turn whose final message begins `BLOCKED:` stops the run with `stop_reason` `blocked` and the line in `RunResult.blocker`, read by `blocker_from` off the first non-empty line, checked after `issue_moved` and before `max_turns`); ``. Re-wrap the paragraph at the file's width.

`CLAUDE.md`, `issuebot.orchestrator` paragraph: `` (the session's final transition is published before any release; `max_turns` while `in_progress` or `max_attempts` failures → the blocked escape) `` becomes `` (the session's final transition is published before any release; `max_turns` or `blocked` while `in_progress`, or `max_attempts` failures → the blocked escape, a `blocked` stop's block carrying the agent's own `BLOCKED:` line; no retry, since an external blocker does not clear by retrying) ``. Re-wrap.

`README.md`: before the paragraph beginning `A credential that stops working *after* startup`, insert a paragraph:

```
A session that hits a true external blocker (a credential it does not have, a tool it cannot
install, a service it cannot reach) writes the brief to the workpad and puts `BLOCKED: <one
line>` at the top of its final message. The worker moves the issue to `issuebot/review` at the
end of that turn with that line in the workpad block, rather than after `agent.max_turns`
re-checks of the same blocker; the turn budget stays as the fallback for a session that stops
without saying why.
```

- [ ] **Step 10: Run the whole suite, lint and pre-commit**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files`
Expected: all green.

- [ ] **Step 11: Commit**

```bash
git add configs/WORKFLOW.md src/issuebot/cli.py CLAUDE.md README.md tests/test_workflow_default.py tests/test_cli.py
git commit -F - <<'EOF'
feat: the prompt names the BLOCKED: line; run-once and the docs report it

<attribution lines>
EOF
```

---

### Task 4: Push and open the pull request

- [ ] **Step 1: Push the branch**

```bash
git push -u origin issuebot/blocked-escape
```

- [ ] **Step 2: Write the PR body (separate call from the API call, per the hook)**

Write `/tmp/claude-1001/-home-jleavers--dev-issuebot/<session>/scratchpad/pr-blocked-escape.md` with the Write tool: the problem (#76's two runs burning turns 2 to 5 on a written-up blocker, the "Turn budget exhausted" block blaming the wrong thing), the marker and where the session reads it, the escape's new reason, what did not change (no event, no column, no setting), validation (the suite count, ruff, pre-commit; CI on the PR shows the zero-step shape #93 is about), and the two trailer lines from the session reminder.

- [ ] **Step 3: Open it**

```bash
gh api repos/jleavers/issuebot/pulls -X POST \
  -f title='feat: the session says BLOCKED: and the worker escalates at once' \
  -f head='issuebot/blocked-escape' -f base='main' \
  -F body=@<the body file> --jq '.number, .html_url'
```

If the call returns "unexpected end of JSON input", check `gh pr list --head issuebot/blocked-escape` before retrying: a 502 can still have created the PR.

- [ ] **Step 4: Confirm the body took**

```bash
gh pr view <number> -R jleavers/issuebot --json body --jq '.body' | head -5
```
