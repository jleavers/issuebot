# Triage phase 1: the branches #291 left untested, as a checklist

Date: 2026-10-27
Status: step 1 of 2 of #311 (this issue, #312); the tests themselves are step 2

## Why

#291 landed triage with its main paths tested, but its reviews logged branches that no test
reaches: each could be deleted or broken today with the suite still green. #311 lists them,
checked against `main` at 09071f6. This spec turns that list into something step 2 can work
through one box at a time. For each bullet it records:

- **Branch**: the production line it covers, re-checked against `main` at `cd5dba5`. 48
  commits landed between the two, among them #319 (verdict retries under a claude hold), #323
  (the triage tidy-up #311 names as #310) and #324 (phase 2's step order), so most line
  numbers have moved and a few branches have changed shape.
- **Test**: the file the test belongs in, and the existing test it sits beside.
- **Fails if**: the assertion that goes red if that branch is removed or broken. Step 2
  checks this by reverting the line once while writing the test, where practical, and says so
  in its pull request.

Items that no longer apply on `cd5dba5` are marked **No longer applies**, with the reason and
what landed instead. #311's own exclusions are still out of scope: a carriage return in a
title (#307) and the triage argv's `setting_sources` / `disallowed_tools` (closed before #291
merged).

## Scope

**No production code changes apart from the operator-step wording and the fake's helper.**
The one behaviour change is the operator-step note's wording (section 8). The other permitted
change, `FakeGitHub.link_sub_issue`, is already on `main` (section 11), so in practice the
operator-step note is the only change under `src/`. Everything else is tests, and test-only
helpers under `tests/`.

## 1. The store (`tests/test_db_queries.py`)

- [ ] **1.1 The work chain starts at the last successful *work* run.**
  Branch: `src/issuebot/db/queries.py:225`, `max(r.started_at) FILTER (WHERE r.outcome =
  'succeeded' AND r.kind = 'work')` in `ISSUE_LEDGERS` (line 210).
  Test: beside `test_triage_failures_do_not_count_toward_the_work_chain` (line 836), which has
  no successful triage run, so dropping the filter still passes.
  Fails if: runs, oldest first, of failed work, succeeded triage, failed work give `(row.failures,
  row.runs) == (2, 3)`. Without `AND r.kind = 'work'` the succeeded triage run starts the chain
  and `failures` is 1. Needs `DATABASE_URL`; without it the module skips.

## 2. The verdict parser (`tests/test_agent_verdict.py`, against `src/issuebot/agent/verdict.py`)

- [ ] **2.1 A verdict that is not an object.**
  Branch: `_fields`, line 127-128 (`{what} must be an object`).
  Test: a new case beside `test_parse_refuses_malformed_verdicts` (line 59), which only
  changes fields of a dict.
  Fails if: `parse_verdict([], ...)` raises `VerdictError` matching `the verdict must be an
  object`. Without the check `name not in names` iterates the list and the error, if any, is
  something else.
- [ ] **2.2 A sub-issue that is not an object.**
  Branch: the same line, reached from line 167 with `what="a sub-issue"`.
  Fails if: `sub_issues: ["a string"]` raises matching `a sub-issue must be an object`.
- [ ] **2.3 A missing field.**
  Branch: lines 139-141 (`missing field {name} in {what}`).
  Fails if: the verdict with `summary` deleted raises matching `missing field summary in the
  verdict`, and a sub-issue with `kind` deleted raises matching `missing field kind in a
  sub-issue`. Without the loop the first is a `KeyError`, not a `VerdictError`.
- [ ] **2.4 `sub_issues` that is not a list.**
  Branch: lines 155-156; the message is `sub_issues must be an array`, not "list".
  Fails if: `sub_issues: {}` raises matching `sub_issues must be an array`.
- [ ] **2.5 An empty or whitespace-only title or body.**
  Branch: `_text`, lines 121-122 (`{name} must not be empty`), with `required=True` from lines
  168 and 173. The value is stripped at line 118 first, so whitespace counts as empty.
  Fails if: a title of `""`, a title of `"   "`, a body of `""` and a body of `"\n\t "` each
  raise matching `title must not be empty` or `body must not be empty`.
- [ ] **2.6 A body over `BODY_LIMIT`.**
  Branch: lines 119-120 with `limit=BODY_LIMIT` (20,000, line 35) from line 173.
  Fails if: a body of `"x" * 20_001` raises matching `body is longer than 20000 characters`,
  and `"x" * 20_000` parses. Only the title's limit is tested today.
- [ ] **2.7 A reason over `REASON_LIMIT`.**
  Branch: the same lines with `limit=REASON_LIMIT` (2,000, line 36) from line 153.
  Fails if: `summary` of `"x" * 2_001` raises matching `summary is longer than 2000
  characters`, and `"x" * 2_000` parses.
- [ ] **2.8 Distinct titles are compared stripped and casefolded.**
  Branch: line 118 strips each title, line 176 casefolds it (`key = title.casefold()`). #311
  named line 164; the two halves now sit in `_text` and the loop.
  Test: beside the identical-titles case in `test_parse_refuses_malformed_verdicts` (line 48).
  Fails if: `" Write the plan "` and `"write the plan"` raise matching `sub-issue titles must
  be distinct`. Dropping either `strip()` or `casefold()` lets the pair through.
- [ ] **2.9 The schema's lengths and item schema.**
  Branch: `verdict_schema`, lines 66-91.
  Test: extend `test_the_schema_matches_the_parser` (line 105), which checks `maxItems`, the
  top-level `required` and `additionalProperties` only.
  Fails if any of these is not asserted equal to the parser's constant: every reason's
  `maxLength == REASON_LIMIT`; the item's `title.maxLength == TITLE_LIMIT` and
  `body.maxLength == BODY_LIMIT`; `title.minLength == body.minLength == 1`; the item's
  `required` as the set `{"title", "body", "kind"}`; its `additionalProperties is False`; and
  `kind.enum == ["issuebot", "operator"]`. Compare against the module's constants, not
  literals, so a limit that moves in one place only fails.
- [ ] **2.10 The unknown-key scrub itself.**
  Branch: line 135, `scrubber.scrub(name) if name.isidentifier() and len(name) <= 40`.
  Test: beside `test_an_unknown_key_that_is_a_credential_is_not_echoed` (line 120), whose two
  keys never reach the scrub: `s3cret-token` is not an identifier, and its `ghp_` key is 42
  characters.
  Fails if: an identifier-shaped key of at most 40 characters that the scrubber masks -- `ghp_`
  plus 36 alphanumerics, exactly 40, which the GitHub-token shape at
  `src/issuebot/agent/scrub.py:52` masks -- raises a message that does not contain the key and
  does contain `unexpected field *** in the verdict`. With the `scrub` call removed the key
  itself is echoed; with the branch removed the message has no `***`.

## 3. `TRIAGE.md` loading (`tests/test_workflow.py`, `tests/test_orchestrator.py`)

The load is no longer TRIAGE.md's own code: since #306, `_read_triage`
(`src/issuebot/config/workflow.py:284`) calls `_read_beside` (line 304), which the overlay
shares. No test reaches either failure through either caller (`test_workflow.py:151` covers
an undecodable *workflow* file, which is another read), so both still apply and are tested
through `TRIAGE.md`, the caller #311 names.

- [ ] **3.1 A read `OSError` or invalid UTF-8.**
  Branch: `src/issuebot/config/workflow.py:322-325`, `except (OSError, UnicodeDecodeError)` ->
  `MissingWorkflowFile(f"{what} unreadable: ...", path=base)`. #311 named lines 312-315.
  Test: beside `test_a_triage_path_that_is_a_directory_is_refused` (`tests/test_workflow.py:288`).
  Fails if: a `TRIAGE.md` of `b"\xff\xfe"` raises `MissingWorkflowFile` matching
  `TRIAGE\.md unreadable` with `info.value.path` the workflow file's resolved path; and the
  same for a `Path.read_text` monkeypatched to raise `PermissionError` on that path alone.
  Without the `except` the first escapes as a bare `UnicodeDecodeError`.
- [ ] **3.2 A `stat` error other than not-found.**
  Branch: lines 315-319: `FileNotFoundError` is "none", any other `OSError` is
  `MissingWorkflowFile(f"{what} unreadable: ...")`. #311 named lines 302-307.
  Fails if: `Path.stat` monkeypatched to raise `PermissionError` for `TRIAGE.md` alone (as
  `tests/test_orchestrator.py:2680` does for the overlay) raises `MissingWorkflowFile`
  matching `TRIAGE\.md unreadable`. Narrowing the clause to `FileNotFoundError` turns it into
  a bare `PermissionError`; widening "none" to every `OSError` loads the default prompt
  silently.
- [ ] **3.3 Reload's own `stat` failure.**
  Branch: `src/issuebot/orchestrator/orchestrator.py:1051-1058`, in `_reload_workflow` (line
  1032): `self._report_reload_failure(f"{triage_path.name} unreadable: {exc}")`. #311 named
  lines 1044-1051.
  Test: beside `test_an_overlay_that_cannot_be_stated_is_a_reload_failure` (line 2680), the
  same shape for `TRIAGE.md`.
  Fails if: after a first tick, `Path.stat` raising `PermissionError` for `TRIAGE.md` leaves
  `snapshot.config_valid is False`, `"TRIAGE.md unreadable" in snapshot.config_error`, and
  `h.orchestrator.workflow is h.workflow` (nothing reloaded).
- [ ] **3.4 Deleting `TRIAGE.md` restores the shipped prompt.**
  Branch: `src/issuebot/config/workflow.py:260`, `triage_template=triage_body or
  default_triage_prompt()`, reached on reload once the file is gone.
  Test: `test_editing_triage_md_reloads_the_workflow` (`tests/test_orchestrator.py:2546`),
  which asserts only `triage_path is None` after the unlink (line 2569).
  Fails if: one more line there, `assert h.orchestrator._workflow.triage_template ==
  default_triage_prompt()`, does not hold. A reload that kept the last template would leave it
  `"Second"`.

## 4. The triage prompt's limits (`tests/test_agent_prompt.py`)

- [ ] **4.1 `max_issue_cost_usd` comes from `agent.max_issue_cost_usd`.**
  Branch: `src/issuebot/agent/prompt.py:840`, in `TriageLimits.from_settings` (line 834).
  Test: `test_triage_limits_come_from_the_work_runs_settings` (line 941), whose settings leave
  the field at its default `0.0`, so a wrong source that also yields `0.0` passes (line 955).
  Fails if: with `"agent": {..., "max_issue_cost_usd": 12.5}` the result has
  `max_issue_cost_usd=12.5`. Pick a value no other limit in that test carries, so reading
  `claude.max_budget_usd` or `triage.max_budget_usd` by mistake also fails.

## 5. The triage clone sweep (`tests/test_agent_workspace.py`)

- [ ] **5.1 A symlink named `*.triage` pointing at a directory is not a key.**
  Branch: `src/issuebot/agent/workspace.py:389`, `self._boundary.is_own_dir(self.root,
  (name,))` in `triage_keys` (line 371). The startup sweep removes whatever it returns.
  Test: `test_triage_keys_lists_only_triage_directories` (line 1249, `@posix`), which has a
  regular file named `stray.triage` but no link.
  Fails if: with `manager.root / "linked.triage"` a symlink to a real directory outside the
  root (holding a sentinel file), `manager.triage_keys()` still equals the two real triage
  keys, and the sentinel survives. Replacing `is_own_dir` with `Path.is_dir()`, which follows
  the link, adds `linked.triage`.

## 6. The triage session (`tests/test_agent_triage.py`, against `src/issuebot/agent/triage.py`)

Since #323 the deadline check, a failed turn and cancellation between turns are `RunState`
methods (`src/issuebot/agent/session.py:150`, `161`, `195`) shared with the work loop, whose
tests (`tests/test_agent_session.py`) reach them. Triage's *calls* to them are still what no
test reaches: each can be deleted from `_execute` with the suite green. So every item below
still applies, on the call site.

- [ ] **6.1 `own_login` fails.**
  Branch: lines 156-160, `state.fail("github_error", "could not read the account's login:
  ...")`. #311 named lines 158-166 for both.
  Test: beside `test_a_github_failure_fails_the_run_with_github_error` (line 95), which fails
  `fetch_comments` only.
  Fails if: `h.github.fail_next("server_error", on="own_login")` gives `result.error_category
  == "github_error"`, `"account's login" in result.error`, `runner.calls == []` and
  `h.kinds() == ["run_started", "run_ended"]`.
- [ ] **6.2 `repo_info` fails.**
  Branch: lines 161-165 (`could not read the account's repository role`).
  Fails if: the same four assertions with `on="repo_info"` and `"repository role" in
  result.error`.
- [ ] **6.3 The home is swept before every turn.**
  Branch: line 190, `await workspaces.sweep_agent_home()` inside the turn loop. #311 named
  line 202.
  Test: beside `test_a_failing_sweep_still_ends_the_run_and_propagates` (line 150), which
  shows only that a failing sweep ends the run.
  Fails if: with `triage_max_turns=3`, no verdict, and `sweep_agent_home` wrapped to append a
  marker to the same list the scripted runner appends to on each turn, the sequence is
  `[sweep, turn, sweep, turn, sweep, turn]`. Moving the sweep above the loop gives one sweep;
  deleting it gives none.
- [ ] **6.4 A `usage_limited` turn carries its window into the result.**
  Branch: `state.fails_on` at line 203, which sets `usage_reset_at` and `usage_window`
  (`src/issuebot/agent/session.py:176-177`) before `RunState.result` copies them (lines
  233-234). #311 named lines 216-219. The orchestrator's usage hold reads them from the result.
  Fails if: a `ScriptedRunner("usage_limited")` turn (as `tests/test_agent_session.py:398`
  scripts it) gives `result.error_category == "usage_limited"` and `(result.usage_reset_at,
  result.usage_window) == (USAGE_RESET, "five_hour")`.
- [ ] **6.5 The run deadline is reached before a turn.**
  Branch: lines 169-170, `if state.past_deadline(settings.agent, turn_number): return`. #311
  named lines 172-178.
  Fails if: with `triage_max_turns=2`, `agent.run_timeout_ms` small and the clock moved past
  it after turn 1 (as `tests/test_agent_session.py:467` does), the result is
  `error_category == "run_timeout"`, `result.error` starts `run deadline reached before turn
  2`, and `len(runner.calls) == 1`.
- [ ] **6.6 A cancel between turns.**
  Branch: lines 211-212, `if state.cancelled(cancel): return`. #311 named lines 234-236.
  Fails if: with `triage_max_turns=2`, no verdict, and the `cancel` event set during turn 1,
  the result is `error_category == "cancelled"` and `stop_reason == "cancelled"`, with
  `len(runner.calls) == 1`. Without the check the run takes turn 2 and ends `max_turns`.
  `TriageHarness.triage` needs a `cancel` argument passed through to `run_triage`.
- [ ] **6.7 The raw verdict stays out of `RunEnded` and `run_finished`.**
  Branch: lines 85-113: the `RunEnded` fields and the `log.info("run_finished", ...)` call,
  neither of which names `structured_output` (the comment at line 103).
  Test: beside `test_a_verdict_ends_the_run` (line 46).
  Fails if: with a verdict holding a distinctive string, that string is in neither
  `repr(ended)` nor any field of the captured `run_finished` entry (`structlog.testing.
  capture_logs`), and neither the event nor the entry has a `structured_output` or `verdict`
  key. Adding `structured_output=result.structured_output` to either call fails it.

## 7. Filing a verdict (`tests/test_orchestrator_triage.py`, against `apply_verdict` in `src/issuebot/orchestrator/triage.py`)

- [ ] **7.1 A retry after the operator-label step fails.**
  Branch: line 162, `await adapter.edit_labels(child.number, add=[labels.operator_step])`.
  #311 named line 136.
  Test: beside `test_a_retry_files_only_the_missing_sub_issues` (line 112).
  Fails if: `github.fail_next("server_error", on="edit_labels")` with `SPLIT` (whose second
  step is the operator's, so the first `edit_labels` call is that one) raises `GitHubError`
  with both children filed, no comment and the parent still `triage_todo`; the retry then
  leaves exactly two children, `operator_step` on the second, the parent `(triage_complete,)`
  and one comment.
- [ ] **7.2 A retry after the label swap fails.**
  Branch: lines 163-165, the swap. #311 named line 137.
  Fails if: with `fail_next("server_error", on="edit_labels", after=1)` (the swap, after the
  operator label), the first call raises with no comment and no `StateChanged`; the retry
  files no third child, swaps, publishes one `StateChanged` and posts one comment.
- [ ] **7.3 An existing child that differs only in case or surrounding whitespace.**
  Branch: `_key`, line 115-116, `title.strip().casefold()`, used at lines 147 and 150. #311
  named line 93.
  Fails if: with the parent already carrying a child titled `"  first STEP "` (made with
  `create_sub_issue`, or `add_issue` plus `link_sub_issue`), applying `SPLIT` creates only
  `"Second step"` -- two children in all -- and returns the existing child as step 1. Without
  `strip()` or `casefold()` it files a duplicate.
- [ ] **7.4 A child a human added is left alone.**
  Branch: lines 147-160: `existing` is read for matching only; nothing iterates it to close,
  relabel or edit.
  Fails if: with a child titled `"Human's own step"` linked first, applying `SPLIT` leaves it
  linked, open, with its title, body and labels unchanged, out of the returned list and out of
  the comment's numbered steps, and the parent has three children.
- [ ] **7.5 ~~A retry of a `fits` verdict.~~** **No longer applies.**
  `test_a_retry_after_the_swap_posts_the_comment_and_no_second_state_change` (line 192),
  added by #319 for #308, files `make_verdict()`, a `fits`, through two failed comments and a
  third, successful call, and asserts one comment, `(triage_complete,)` and one
  `StateChanged`. A `fits` retry that swapped twice, commented twice or lost the label fails
  it. 7.2 covers a `fits` whose swap fails, if step 2 wants the other half too: the same
  assertions with `make_verdict()` and `on="edit_labels"`.

## 8. The operator-step hand-back (`tests/test_orchestrator_actions.py`)

This is the one production change.

- [ ] **8.1 The note names the state label actually removed.**
  Branch: `src/issuebot/orchestrator/actions.py:556-559`, the `way_back` of
  `operator_step_escape` (line 537): `f"issuebot has removed `{labels.todo}`."` whichever
  label `clear_state` took off. #311 named lines 539-541. `_dispatch`
  (`src/issuebot/orchestrator/orchestrator.py:1591-1602`) reaches it for any dispatched
  issue carrying `operator_step`, which is `todo`, `rework`, and an `in-progress` one resumed
  after a restart. #311 named line 1509.
  Change: name the label `state_label_name(issue)` gives (`src/issuebot/orchestrator/state.py:58`),
  the same value already passed as `from_label` at line 560, so the note and the
  `StateChanged` cannot disagree. The issue reached here always carries one, since `_dispatch`
  is only reached for a state-label candidate; if it is `None`, the sentence names none
  rather than inventing `todo`.
  Test: beside `test_operator_step_escape_clears_todo_and_says_why` (line 1430), parametrised
  over `todo`, `rework` and `in-progress`.
  Fails if: for an issue labelled `issuebot/rework` plus `issuebot/operator-step`, the comment
  ends `issuebot has removed `issuebot/rework`.`, `issue.state is None`, and `StateChanged`
  is `("issuebot/rework", None)`. Today the body ends `issuebot/todo`, so the `rework` case
  fails before the change and passes after it -- the only test in step 2 written to fail
  first.

## 9. The worker's triage paths (`tests/test_orchestrator.py`, against `src/issuebot/orchestrator/orchestrator.py`)

Already covered: `turn_failed` and a refused verdict (`test_repeated_bad_verdicts_escape`,
line 5396), a verdict no process can carry (line 5447), and `usage_limited` (line 5753). All
new tests use the triage harness those do (`h.add_triage_issue`, `h.exit`) and sit beside
them. `_triage_exit` is at line 2008.

- [ ] **9.1 `auth_failed`: the auth hold, no failure counted.**
  Branch: lines 2028-2037, the `else: self._hold_for_auth_failure(entry, result)` arm. #311
  named line 1938.
  Fails if: with `max_attempts=1`, a triage run ending `error_category="auth_failed"` sets
  `snapshot().dispatch_hold.kind == "auth"`, leaves `triage_todo` on the issue, publishes no
  `Blocked`, and starts no second triage run on the next tick. Counting it as a failure would
  escape at once with one attempt allowed.
- [ ] **9.2 A stall counts as a failure, named `stalled: ...`.**
  Branch: lines 2060-2061, `failure = f"stalled: {entry.stop_detail}"`. #311 named line 1970.
  Fails if: with `max_attempts=1` and `claude.stall_timeout_ms` set, a triage run that stalls
  (as `test_stall_detection_kills_and_retries`, line 2049, drives one) escapes with a note on
  the issue, and the `Blocked` reason contains `stalled:`.
- [ ] **9.3 A crash: the result is `None`, the failure is "worker crashed".**
  Branch: lines 2062-2063, `failure = error or "worker crashed"`, reached with `result is
  None` from the task's exception path (line 2517, `f"worker crashed: {exc}"`).
  Fails if: with `max_attempts=1`, a triage session that raises (as
  `test_crashed_worker_is_retried`, line 1667, scripts one) escapes, the `Blocked` reason
  contains `worker crashed`, and the issue keeps no `triage_complete`.
- [ ] **9.4 A stop on `shutdown`: no failure, the verdict discarded.**
  Branch: lines 2018-2027, `"shutdown"` in `stopped`, which schedules the discard and
  returns before the verdict branch at line 2038.
  Fails if: a triage run still going when `h.orchestrator.shutdown()` runs, finishing with
  `**VERDICT` once stopped, leaves `triage_todo` on the issue, no `triage_complete`, no
  comment, no `Blocked`, and no failure on the chain (`_triage_failures` has no entry for the
  issue). Taking `"shutdown"` out of `stopped` files the verdict.
- [ ] **9.5 The spend refusal hands the issue back as `triage budget limit`.**
  Branch: lines 1762-1776, `verdict.kind == "spend"` -> `_triage_hand_back(..., title="triage
  budget limit", ...)`. #311 named lines 1678-1689. `tests/test_admission.py:386` covers
  only the refusal.
  Fails if: with `agent.max_issue_cost_usd` set below what the issue's ledger already holds,
  a tick starts no triage run, removes `triage_todo`, and posts one comment starting
  `### Issuebot triage budget limit (` whose body names `agent.max_issue_cost_usd`; a second
  tick posts nothing more. Without the branch the refusal `continue`s silently on every tick
  and the label stays.
- [ ] **9.6 A discard due while a new triage run for the same issue is live is held.**
  Branch: line 2159-2160 in `_discard_triage_workspaces` (line 2149), `identifier in running`.
  #311 named line 2063.
  Test: beside `test_a_triage_workspace_is_discarded_after_the_delay` (line 5991).
  Fails if: run 1 ends (a failure, so the issue is re-dispatched), run 2 for the same issue
  starts before `TRIAGE_DISCARD_DELAY_S` is up, the clock passes the delay, and the clone
  still exists after the tick; once run 2 ends and its own delay passes, it is gone. Without
  the check the clone run 2 is reading is removed under it.

## 10. `run-once --triage` (`tests/test_cli.py`, against `_run_once_triage` in `src/issuebot/cli.py`)

`_run_once_triage` is at `src/issuebot/cli.py:2164`; `StubTriage` at `tests/test_cli.py:5470`.

- [ ] **10.1 No verdict creates no sub-issues.**
  Branch: lines 2262-2296: only `stop_reason == "verdict"` reaches `apply_verdict`.
  Test: `test_run_once_triage_without_a_verdict_writes_nothing` (line 5685; #311 named 5589).
  Fails if: with `stub_triage.structured_output` set to a valid *split* and `stop_reason =
  "max_turns"`, `fake_github.calls` has no `create_sub_issue` and `sub_issues(42)` is empty. A
  `fits` output would prove nothing, since filing it creates none either.
- [ ] **10.2 A refused verdict creates no sub-issues and moves no label.**
  Branch: lines 2263-2270, the `except VerdictError` arm.
  Test: `test_run_once_triage_with_a_refused_verdict_writes_nothing` (line 5704; #311 named
  5608), which asserts only that there is no comment.
  Fails if: with a split whose second step is refused (e.g. an empty title), no
  `create_sub_issue` call was made and `fake_github.issue(42).labels ==
  ("issuebot/triage-todo",)`. Parsing step by step and filing as it went would create the
  first child.
- [ ] **10.3 `run_triage` raising.**
  Branch: the `try`/`finally: await sinks.close()` around lines 2250-2298; nothing catches,
  so the exception leaves `main`.
  Fails if: with `StubTriage` raising `RuntimeError("boom")`, `main([...])` raises
  `RuntimeError`, the issue's labels are unchanged, no comment or sub-issue was written, and
  `_Sinks.close` (`src/issuebot/cli.py:1934`, monkeypatched to record the call) ran. A
  `finally` that is lost leaves the sinks open; an `except Exception` that files anyway
  writes.
- [ ] **10.4 A pooled workflow runs triage as the issue's bound account.**
  Branch: line 2206, `workflow = _with_bound_account(workflow, issue)`. #311 named line 2165.
  `_with_bound_account` (line 2442) is tested directly
  (`test_run_once_takes_the_workspaces_own_account_from_the_pool`, line 4410), not through
  `run-once --triage`.
  Fails if: with `agent.run_as: agent-1,agent-2`, the workflow `StubTriage` receives has
  `config.agent.run_as == ("agent-1",)` and `not run_as_pooled`, and the registry's
  `bound("example-42")` is `"agent-1"`. `StubTriage.__call__` records `workflow` beside
  `issue` and `attempt` for this (test-only). Deleting line 2206 hands it the pool.

## 11. Test hygiene

- [ ] **11.1 `FakeGitHub.link_sub_issue(parent, child)`.**
  **The helper no longer needs adding:** #324 landed it
  (`src/issuebot/github/fake.py:456`, commit 4552ef1), refusing a child that already has a
  parent, and `test_link_sub_issue_links_both_ways` (`tests/test_github_fake.py:526`) tests
  it. What still applies is the other half: two tests still touch `_issues`.
  - [ ] `tests/test_github_fake.py:421`, in `test_fake_create_sub_issue_links_both_ways`, reads
    `fake._issues[sub.number].parent`. Replace with `fake.issue(sub.number).parent ==
    parent.number`; `Issue.parent` (`src/issuebot/github/models.py:71`) is the public read.
  - [ ] `tests/test_cli.py:5754-5755`, in
    `test_run_once_triage_refuses_an_issue_that_already_has_sub_issues` (line 5739; #311
    named 5658-5659), writes `_issues[42].children` and `_issues[43].parent`. Replace both
    with `fake_github.link_sub_issue(42, 43)`.
  Fails if: `grep -rn "\._issues" tests` finds anything. Both tests keep their existing
  assertions, which a link made the wrong way round (`children` without `parent`) fails.
- [ ] **11.2 `tests/test_events.py` `common: dict[str, Any]`.**
  Branch: none; `test_run_events_default_to_work_and_round_trip_the_kind` (line 220) declares
  `common: dict[str, object]` (line 221), which is why lines 233-234 each carry `# type:
  ignore[arg-type]`.
  Change: type it `dict[str, Any]` and drop both comments. No type checker runs in CI, so the
  check is the test still passing and `ruff` clean (`Any` imported, no unused ignore left).
- [ ] **11.3 The redundant `CLAUDE_FAKE_RECORD_ENV=GH_TOKEN`.**
  Branch: none; `tests/test_agent_runner.py:2872` names `GH_TOKEN`, which the fake already
  records (`RECORDED_ENV`, `tests/fakes/claude:23-24`; extra names join it at line 54).
  Change: delete the line. The test's own assertion,
  `json.loads(record.read_text())["env"]["GH_TOKEN"] is None` (line 2877), is the proof: it
  still passes only because the fake records `GH_TOKEN` without being asked.

## Validation for step 2

#311's own:

```shell
uv run pytest        # with DATABASE_URL set, so tests/test_db_queries.py runs rather than skips
uv run ruff check . && uv run ruff format --check .
```

plus `uv run pre-commit run --all-files`, and, for each box above where it is practical, the
branch reverted once to see the test go red, noted in the pull request.
