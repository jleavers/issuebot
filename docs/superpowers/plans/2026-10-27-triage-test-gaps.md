# Triage Test Gaps (Staged) Implementation Plan

> **For agentic workers:** each stage below is one work run and one pull request. Take the
> stages in the order given unless a stage says it is independent; tick the spec's boxes in the
> pull request body, not in the spec.

**Goal:** Every branch #311 lists as untested has a test that goes red when the branch is
removed or broken, and the operator-step note names the state label it actually removed.

**Spec:** `docs/superpowers/specs/2026-10-27-triage-test-gaps-design.md` (#312). It gives,
for each item, the production branch, the test it sits beside and the assertion that fails.
This plan does not repeat those assertions; it assigns each item to a stage and says how the
stage is checked. Read the spec's section for a stage before starting it.

**Checked against:** `main` at `df2f988`. The spec was checked at `cd5dba5`, and the only
commits since are the spec's own, so its line numbers still hold. A stage that starts later
re-reads them: other pull requests may have moved them.

## Rulings on what the spec and the issue leave open

1. **Nine stages, not eight.** The issue's likely shape puts `TRIAGE.md` loading with the
   store and the worker's triage paths in one stage. Two of the four `TRIAGE.md` items (3.3,
   3.4) live in `tests/test_orchestrator.py`, so they move to a worker stage to keep that file
   out of stage 3. With them, the worker has eight tests in that file, and the shutdown and
   discard tests (9.4, 9.6) each need a run held open across ticks. That is two runs' work, so
   the worker is split into stages 7 and 8, both touching `tests/test_orchestrator.py` alone.
2. **11.1 stays whole in stage 1, two lines of `tests/test_cli.py` included.** The spec makes
   the `_issues` cleanup one item with one check (`grep -rn "\._issues" tests` finds nothing),
   and the issue names it as the fake-helper stage's. Its `tests/test_cli.py` part is a
   two-line substitution inside one existing test, with no new test, so stage 1 stays small.
   Stage 9, the one stage that adds to `tests/test_cli.py`, comes after it.
3. **The fake's helper is not added again.** `FakeGitHub.link_sub_issue` is on `main`
   (`src/issuebot/github/fake.py:456`, #324) with its own test. Stage 1 only uses it. Stage 1
   may edit `src/issuebot/github/fake.py` only if the helper turns out to be missing when the
   stage starts. In practice no stage but 6 touches `src/`.
4. **7.5 is closed in stage 5 by a parametrised 7.2, not by a new test.** The spec marks 7.5
   **No longer applies**, because #319's `test_a_retry_after_the_swap_posts_the_comment_and_no_second_state_change`
   covers a `fits` retry. It offers the other half, a `fits` whose *swap* fails. Stage 5 takes
   that as a second case of 7.2's test. A `fits` verdict files no operator label, so its first
   `edit_labels` call is the swap: that case uses `after=0` where `SPLIT` uses `after=1`. 7.5
   then needs nothing of its own, and its box is ticked as "covered by 7.2's `fits` case".
5. **Reverting a branch to watch a test go red is a local edit, never committed.** Edit the
   production line, run the one test, see it fail, then restore the file with
   `git checkout -- <file>` (not `git reset --hard` or `git stash`) and confirm
   `git status --short` lists only the stage's own files. Each stage records the outcome in
   its pull request, one line per item: the edit made and the failing assertion, or why the
   check was not practical.
6. **Where the reverted branch is an argument or a filter, "reverted" means the smallest edit
   the spec's "Fails if" names:** drop `AND r.kind = 'work'`, drop `.strip()` or
   `.casefold()`, swap `is_own_dir` for `Path.is_dir()`, delete the call. Do not delete
   whole functions.

## Validation, every stage

```shell
uv run pytest                                   # the whole suite, not only the stage's file
uv run ruff check . && uv run ruff format --check .
uv run pre-commit run --all-files
```

Stage 3 also runs `tests/test_db_queries.py` against a database (see its section). CI's `test`
job always does, since it sets `DATABASE_URL` to its postgres service, so a stage whose run
has no database still gets the store tests checked on its pull request.

Every stage leaves `main` green on its own. None depends on a later one, and each test passes
on the `main` it lands on. The only test written to fail first is 8.1's `rework` case, and it
lands in the same commit as the change that makes it pass.

## Stage order and dependencies

| Stage | Area | Spec items | Files | After |
|---|---|---|---|---|
| 1 | Fake helper and hygiene | 11.1, 11.2, 11.3 | `tests/test_github_fake.py`, `tests/test_cli.py` (2 lines), `tests/test_events.py`, `tests/test_agent_runner.py` | -- |
| 2 | Verdict parser and schema | 2.1 -- 2.10 | `tests/test_agent_verdict.py` | -- |
| 3 | Store, prompt limits, clone sweep, `TRIAGE.md` reads | 1.1, 3.1, 3.2, 4.1, 5.1 | `tests/test_db_queries.py`, `tests/test_workflow.py`, `tests/test_agent_prompt.py`, `tests/test_agent_workspace.py` | -- |
| 4 | Triage session | 6.1 -- 6.7 | `tests/test_agent_triage.py` | -- |
| 5 | `apply_verdict` retries and matching | 7.1 -- 7.5 | `tests/test_orchestrator_triage.py` | -- |
| 6 | Operator-step hand-back | 8.1 | `src/issuebot/orchestrator/actions.py`, `tests/test_orchestrator_actions.py` | -- |
| 7 | Worker: `TRIAGE.md` reload, exits | 3.3, 3.4, 9.1, 9.2, 9.3 | `tests/test_orchestrator.py` | -- |
| 8 | Worker: shutdown, spend, discard | 9.4, 9.5, 9.6 | `tests/test_orchestrator.py` | 7 |
| 9 | `run-once --triage` | 10.1 -- 10.4 | `tests/test_cli.py` | 1 |

Stages 1 to 7 are independent of one another and may run in parallel. Stage 8 waits for 7,
and 9 for 1, because each pair shares a busy file. Running them in sequence costs less than
resolving the conflicts.

---

## Stage 1: the fake's helper and test hygiene

**Spec items:** 11.1, 11.2, 11.3. **Production edits:** none (ruling 3).

**Files:** `tests/test_github_fake.py`, `tests/test_cli.py`, `tests/test_events.py`,
`tests/test_agent_runner.py`.

- [ ] **11.1** In `test_fake_create_sub_issue_links_both_ways` (`tests/test_github_fake.py`),
  read the parent through `fake.issue(sub.number).parent`. In
  `test_run_once_triage_refuses_an_issue_that_already_has_sub_issues` (`tests/test_cli.py`),
  replace the two `_issues` writes with `fake_github.link_sub_issue(42, 43)`. Keep both tests'
  existing assertions.
- [ ] **11.2** In `test_run_events_default_to_work_and_round_trip_the_kind`, type `common` as
  `dict[str, Any]`, import `Any`, and drop both `# type: ignore[arg-type]` comments.
- [ ] **11.3** Delete the `"CLAUDE_FAKE_RECORD_ENV": "GH_TOKEN"` line in
  `tests/test_agent_runner.py`. The test's `GH_TOKEN is None` assertion stays as it is.

**Confirm it fails when reverted:**

- 11.1: `grep -rn "\._issues" tests` prints nothing. Then link the two the wrong way round
  locally (`link_sub_issue(43, 42)`) and watch the refusal test fail.
- 11.2 and 11.3 have no branch to revert, because they change no behaviour. The proof is the
  suite still green and `ruff` clean: no unused import, no stale ignore. For 11.3, also run
  the test with `tests/fakes/claude`'s `RECORDED_ENV` locally without `GH_TOKEN` and watch it
  fail. That shows the deleted line was redundant rather than load-bearing.

**Pause:** none.

## Stage 2: the verdict parser and its schema

**Spec items:** 2.1 -- 2.10. **Production edits:** none.

**Files:** `tests/test_agent_verdict.py` (against `src/issuebot/agent/verdict.py`).

- [ ] **2.1 -- 2.8** Add each as a case of `test_parse_refuses_malformed_verdicts`'s
  parametrisation, where the shape is a field change. Use a separate test where the input
  is not a dict (2.1: `parse_verdict([], ...)`; 2.2: a string sub-issue) or where the limit
  needs a passing twin (2.6, 2.7: the limit parses, one over is refused).
- [ ] **2.9** Extend `test_the_schema_matches_the_parser` with every equality the spec lists,
  compared against `REASON_LIMIT`, `TITLE_LIMIT` and `BODY_LIMIT`, not literals.
- [ ] **2.10** A new test beside `test_an_unknown_key_that_is_a_credential_is_not_echoed`, with
  a 40-character `ghp_` key.

**Confirm it fails when reverted:** one local edit per item in `verdict.py`. Remove the
object check (2.1, 2.2), the missing-field loop (2.3), the array check (2.4), the `required`
emptiness check (2.5), and the `limit=` arguments (2.6, 2.7). Drop `strip()`, then
`casefold()` (2.8). Change one `maxLength` in `verdict_schema` (2.9). Remove the `scrub`
call, then the whole conditional (2.10).

**Pause:** none.

## Stage 3: the store, the prompt limits, the clone sweep and `TRIAGE.md` reads

**Spec items:** 1.1, 3.1, 3.2, 4.1, 5.1. **Production edits:** none.

**Files:** `tests/test_db_queries.py`, `tests/test_workflow.py`, `tests/test_agent_prompt.py`,
`tests/test_agent_workspace.py`.

- [ ] **1.1** Beside `test_triage_failures_do_not_count_toward_the_work_chain`: failed work,
  succeeded triage, failed work gives `(failures, runs) == (2, 3)`.
- [ ] **3.1** Beside `test_a_triage_path_that_is_a_directory_is_refused`: an undecodable
  `TRIAGE.md`, and a `read_text` that raises `PermissionError` for that path alone.
- [ ] **3.2** The same place: a `stat` that raises `PermissionError` for `TRIAGE.md` alone.
- [ ] **4.1** Set `agent.max_issue_cost_usd` to `12.5` in
  `test_triage_limits_come_from_the_work_runs_settings`, a value no other limit there carries.
- [ ] **5.1** Add a `linked.triage` symlink to `test_triage_keys_lists_only_triage_directories`
  (or a sibling test, `@posix`), and sweep its keys as the sweep test does.

**Validation, in addition to the common commands:** the store test runs only with a
database:

```shell
docker compose --profile test up -d --wait test-db
DATABASE_URL=postgresql://issuebot@$(docker compose port test-db 5432)/issuebot uv run pytest tests/test_db_queries.py
docker compose rm -sf test-db
```

Where the run has no Docker and no `DATABASE_URL`, the module skips locally. CI's `test` job
is then the proof that 1.1 passes, and the pull request says so.

**Confirm it fails when reverted:**

- 1.1: drop `AND r.kind = 'work'` from `ISSUE_LEDGERS` (`src/issuebot/db/queries.py`), and
  `failures` reads 1.
- 3.1: remove the `except (OSError, UnicodeDecodeError)` in `_read_beside`.
- 3.2: narrow the `stat` clause to `FileNotFoundError`.
- 4.1: read `claude.max_budget_usd` in `TriageLimits.from_settings`.
- 5.1: swap `is_own_dir` for `(self.root / name).is_dir()` in `triage_keys`.

**Pause:** conditional. If the work run had no database, it cannot do 1.1's revert check
itself, and the pull request says so. The maintainer then runs the `test-db` commands above
once, with `AND r.kind = 'work'` removed locally, and sees the new test fail. Every other
item is checked in the run.

## Stage 4: the triage session

**Spec items:** 6.1 -- 6.7. **Production edits:** none.

**Files:** `tests/test_agent_triage.py` (against `src/issuebot/agent/triage.py`). The
`cancel` argument that 6.6 needs goes on `TriageHarness.triage` in the same file.

- [ ] **6.1, 6.2** Beside `test_a_github_failure_fails_the_run_with_github_error`, one
  parametrised test over `own_login` and `repo_info`.
- [ ] **6.3** A sweep/turn pairing over three turns (assert the pairing, not the whole list).
- [ ] **6.4** A `usage_limited` turn's window reaches the result.
- [ ] **6.5** The deadline before turn 2.
- [ ] **6.6** A cancel set during turn 1. Add `cancel: asyncio.Event | None = None` to
  `TriageHarness.triage` and pass it through to `run_triage`.
- [ ] **6.7** Beside `test_a_verdict_ends_the_run`, with `structlog.testing.capture_logs`.

**Confirm it fails when reverted:** in `src/issuebot/agent/triage.py`'s `_execute`, delete
each call once. That is the `own_login` and `repo_info` `try` arms (6.1, 6.2),
`sweep_agent_home` in the loop (6.3; also move it above the loop), `state.fails_on` (6.4),
`state.past_deadline` (6.5) and `state.cancelled` (6.6). Add
`structured_output=result.structured_output` to `RunEnded` and to `run_finished` in turn
(6.7).

**Pause:** none.

## Stage 5: `apply_verdict` retries and matching

**Spec items:** 7.1 -- 7.5. **Production edits:** none.

**Files:** `tests/test_orchestrator_triage.py` (against `src/issuebot/orchestrator/triage.py`).

- [ ] **7.1** Beside `test_a_retry_files_only_the_missing_sub_issues`: the operator label
  fails, then the retry.
- [ ] **7.2** The swap fails, then the retry. Parametrise it over `SPLIT` with
  `after=1` and `make_verdict()` with `after=0` (ruling 4).
- [ ] **7.3** An existing child titled `"  first STEP "`. Link it with `link_sub_issue`, from
  stage 1's helper, which is already on `main`.
- [ ] **7.4** A child a human added is left untouched and stays out of the comment.
- [ ] **7.5** No test of its own. Its box is ticked as covered by 7.2's `fits` case.

**Confirm it fails when reverted:** in `apply_verdict`, delete the operator-label call
(7.1). Publish `StateChanged` whether or not `swapped` is set, and drop the existing-child
lookup so a retry files again (7.2). Drop `strip()` and then `casefold()` from `_key` (7.3).
For 7.4, add a loop over `existing` locally that closes or relabels children not in the
verdict, and watch the test fail.

**Pause:** none.

## Stage 6: the operator-step hand-back

**Spec items:** 8.1. **Production edits:** `src/issuebot/orchestrator/actions.py`, the one
behaviour change in the whole of #311.

**Files:** `src/issuebot/orchestrator/actions.py`, `tests/test_orchestrator_actions.py`.

- [ ] **8.1, test first** Parametrise `test_operator_step_escape_clears_todo_and_says_why`
  over `todo`, `rework` and `in-progress`. Each case asserts that the comment ends
  ``issuebot has removed `<that label>`.``, that `issue.state is None`, and that
  `StateChanged` is `(<that label>, None)`. Run it on `main`: the `rework` and `in-progress`
  cases fail, because the note says `issuebot/todo`. Record that output.
- [ ] **8.1, change** In `operator_step_escape`, compute `removed = state_label_name(issue)`
  once. Use it both as `from_label` and in `way_back`. When it is `None`, the sentence names no
  label (e.g. "issuebot has removed its state label.") rather than `todo`. Add that case to the
  test, so the `None` branch is also covered.
- [ ] Search the tree for the old wording outside `docs/superpowers/`
  (`grep -rn "has removed \`" src tests docs README.md`). Today only this function and this
  test carry it. Update any other copy found.

**Confirm it fails when reverted:** the fail-first run above is the check. Then put
`labels.todo` back in `way_back` locally and watch the `rework` case fail again.

**Pause:** none. The wording is visible to operators in an issue comment, so the pull
request quotes the new note for each label. That lets the reviewer read it without running
anything.

## Stage 7: the worker -- `TRIAGE.md` reload and the run exits

**Spec items:** 3.3, 3.4, 9.1, 9.2, 9.3. **Production edits:** none.

**Files:** `tests/test_orchestrator.py` alone (against `src/issuebot/orchestrator/orchestrator.py`).

- [ ] **3.3** Beside `test_an_overlay_that_cannot_be_stated_is_a_reload_failure`, the same
  shape for `TRIAGE.md`.
- [ ] **3.4** One more assertion in `test_editing_triage_md_reloads_the_workflow`:
  `triage_template == default_triage_prompt()` after the unlink.
- [ ] **9.1** `auth_failed` with `max_attempts=1`: auth hold, no `Blocked`, label kept.
- [ ] **9.2** A stall with `max_attempts=1`: escapes, the `Blocked` reason contains
  `stalled:`.
- [ ] **9.3** A crash with `max_attempts=1`: escapes, the `Blocked` reason contains `worker
  crashed`.

Each one sits beside the triage tests the spec names (`test_repeated_bad_verdicts_escape` and
its neighbours) and uses their harness (`h.add_triage_issue`, `h.exit`).

**Confirm it fails when reverted:** in `_reload_workflow`, catch nothing around the
`TRIAGE.md` stat (3.3). Keep the old `triage_template` on reload, so the template still reads
`"Second"` (3.4). In `_triage_exit`, route `auth_failed` into the failure arm (9.1). Drop the
`stalled:` prefix (9.2). Replace `"worker crashed"` with `""` (9.3).

**Pause:** none.

## Stage 8: the worker -- shutdown, the spend hand-back and the discard hold

**Spec items:** 9.4, 9.5, 9.6. **Production edits:** none. **After:** stage 7 (same file).

**Files:** `tests/test_orchestrator.py` alone.

- [ ] **9.4** A triage run still going when `shutdown()` runs, ending with a verdict once
  stopped: nothing filed, no failure on the chain.
- [ ] **9.5** `agent.max_issue_cost_usd` below the ledger: no run, `triage_todo` removed, one
  `### Issuebot triage budget limit (` comment, and nothing more on a second tick.
- [ ] **9.6** Beside `test_a_triage_workspace_is_discarded_after_the_delay`: run 2 live past
  run 1's delay keeps the clone; it goes once run 2 ends and its own delay passes.

**Confirm it fails when reverted:** take `"shutdown"` out of the `stopped` check in
`_triage_exit` (9.4). Delete the `verdict.kind == "spend"` hand-back, so the refusal
`continue`s (9.5). Delete `identifier in running` in `_discard_triage_workspaces` (9.6).

**Pause:** none.

## Stage 9: `run-once --triage`

**Spec items:** 10.1 -- 10.4. **Production edits:** none. **After:** stage 1 (same file;
10.4's test sits near the one stage 1 edits).

**Files:** `tests/test_cli.py` alone (against `_run_once_triage` in `src/issuebot/cli.py`).

- [ ] **10.1** In `test_run_once_triage_without_a_verdict_writes_nothing`, a valid *split*
  with `stop_reason = "max_turns"`: no `create_sub_issue`, no children.
- [ ] **10.2** In `test_run_once_triage_with_a_refused_verdict_writes_nothing`, a split whose
  second step has an empty title: no `create_sub_issue`, labels unchanged.
- [ ] **10.3** `StubTriage` raising `RuntimeError("boom")`: it leaves `main`, nothing is
  written, and `_Sinks.close` ran.
- [ ] **10.4** A pooled `agent.run_as`: `StubTriage` records `workflow` (a test-only
  addition to `StubTriage.__call__`) and receives `run_as == ("agent-1",)`, bound in the
  registry.

**Confirm it fails when reverted:** in `_run_once_triage`, let any `stop_reason` reach
`apply_verdict` (10.1). File step by step before parsing the whole verdict (10.2). Turn the
`finally` into an `except Exception` that files anyway (10.3). Delete
`workflow = _with_bound_account(workflow, issue)` (10.4).

**Pause:** none.

---

## Coverage: every spec item, one stage

| Spec item | Stage | | Spec item | Stage |
|---|---|---|---|---|
| 1.1 | 3 | | 6.5 | 4 |
| 2.1 | 2 | | 6.6 | 4 |
| 2.2 | 2 | | 6.7 | 4 |
| 2.3 | 2 | | 7.1 | 5 |
| 2.4 | 2 | | 7.2 | 5 |
| 2.5 | 2 | | 7.3 | 5 |
| 2.6 | 2 | | 7.4 | 5 |
| 2.7 | 2 | | 7.5 | 5 (by 7.2's `fits` case) |
| 2.8 | 2 | | 8.1 | 6 |
| 2.9 | 2 | | 9.1 | 7 |
| 2.10 | 2 | | 9.2 | 7 |
| 3.1 | 3 | | 9.3 | 7 |
| 3.2 | 3 | | 9.4 | 8 |
| 3.3 | 7 | | 9.5 | 8 |
| 3.4 | 7 | | 9.6 | 8 |
| 4.1 | 3 | | 10.1 | 9 |
| 5.1 | 3 | | 10.2 | 9 |
| 6.1 | 4 | | 10.3 | 9 |
| 6.2 | 4 | | 10.4 | 9 |
| 6.3 | 4 | | 11.1 | 1 |
| 6.4 | 4 | | 11.2 | 1 |
| | | | 11.3 | 1 |

43 items, each in exactly one stage. Production code changes in stage 6 alone, and in stage 1
only if ruling 3's condition is met. Everything else is under `tests/`.

## Out of scope

As the spec: a carriage return in a title (#307), and the triage argv's `setting_sources`
and `disallowed_tools`. Anything a stage finds that the spec does not list becomes a
follow-up issue, not an extra test in the stage.
