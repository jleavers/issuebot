# The session says it is blocked, and the worker escalates at once

Date: 2026-09-13
Status: implemented

## Problem

The prompt's ground rule 2 tells the agent to stop for a true external blocker: record what
is missing and the exact human action in the workpad, then end the turn, and "issuebot will
escalate". Nothing in the orchestrator reads that brief. The only escalation is the turn
budget: `run_session` resumes the same session on the next turn, the agent re-checks the same
blocker, ends the turn again, and only when `max_turns` runs out while the issue is still
`in_progress` does `handle_worker_exit` apply the blocked escape, with the reason "Turn budget
exhausted".

Issue #76 showed the cost twice on 2026-09-13. The original run finished its pull request in
turn 1 and then spent turns 2 to 5 re-sweeping a CI outage it had already written up
(`$1.37` and twenty minutes); the rework run after the merge-conflict bounce did the same
(`$1.20`, fifteen minutes). Both escape blocks blamed a turn budget, which is not what
happened, and the operator had to read the workpad's Notes to find out. PR #93 removed the
Actions-billing case from the completion bar, but the gap is general: any genuine blocker
still burns every remaining turn before a human hears of it.

So the requirement is: **when a turn ends because the agent is blocked, the worker escapes
the issue at the end of that turn, with the agent's own reason.**

## Decision

The agent marks a blocked turn in the one place the session already reads: the turn's final
message. Ground rule 2 gains a fixed first line, `BLOCKED: <one line>`. `run_session` reads
the turn's `result_text`; when it begins with that marker the session stops with a new
`StopReason`, `blocked`, carrying the rest of the line as `RunResult.blocker`. The
orchestrator treats `blocked` while `in_progress` exactly as it treats `max_turns` while
`in_progress` today, except that the escape's reason is the agent's line rather than a
turn-budget sentence. No retry: an external blocker does not clear by retrying, which is the
choice the authentication escape already makes.

### Why not the alternatives

**Reading the workpad's `### Blockers` section after every turn** was rejected. The workpad
is the long-form brief and stays so, but detecting a change there costs one more `gh` call
per turn, needs Markdown section parsing that has to tell the template's placeholder from a
real entry, and yields a paragraph where the escape block and the Slack line want a sentence.

**A file in the workspace** (`.issuebot/blocked`, like `.issuebot/env`) was rejected as a
second protocol for the agent to remember with the same content as the marker line. The
final message is already governed by ground rule 3 ("completed actions and blockers only"),
so the marker is a constraint on text the agent writes anyway.

**A `blocked` outcome or an error category** was rejected: nothing failed. The run
succeeded at what it could do and stopped for a reason, which is what `StopReason` is for.

## Design

### 1. The session contract

`issuebot.agent.session`:

- `StopReason` gains `"blocked"`.
- `RunResult` gains `blocker: str | None = None`: the marker line's text after `BLOCKED:`,
  stripped, when `stop_reason == "blocked"`; `None` otherwise.
- `BLOCKED_MARKER = "BLOCKED:"` and a pure `blocker_from(result_text: str | None) -> str | None`:
  the text after the marker on the *first non-empty line* when that line starts with the
  marker, stripped; `None` for any other text, including one that mentions `BLOCKED:` later
  on. An empty reason after the marker reads as `None` too, so a bare `BLOCKED:` does not
  escape with an empty block. The reason is capped at 500 characters (`BLOCKER_LIMIT`): the
  workpad's Blockers section is the long-form brief, and an unbounded line would fail the
  escape's comment update and loop its retry. Case-sensitive: the prompt gives the exact
  spelling.
- In `_run_turns`, after the turn is recorded and the issue refreshed, the checks run in
  this order: cancelled; issue missing; issue moved (`issue_moved`, which wins because the
  label is the truth and the agent may have handed off in the same turn); then, new, `blocker
  = blocker_from(turn.result_text)`, and if it is set, `state.stop("blocked")` with
  `state.blocker = blocker` and return; then `max_turns` as today. A turn that failed never
  reaches the check: `turn.ok` is handled first, as now.
- The `session_finished` / `run_finished` log line carries `blocker` when set.

### 2. The orchestrator

`Orchestrator.handle_worker_exit`, succeeded branch: beside the `max_turns` while
`in_progress` case,

```
if result.stop_reason == "blocked" and result.final_state is StateLabel.IN_PROGRESS:
    await self._escape(entry, result.blocker or "the session reported a blocker", result)
    return
```

`_escape` is unchanged: `BlockedContext(reason=<the line>, run_id, attempt, turns, log_dir)`,
`blocked_escape` writes the block and moves the issue to `review`, `Blocked` is published and
the counter bumps. A `blocked` stop whose issue is no longer `in_progress` falls through to
the continuation schedule as any succeeded run does, and the retry's refresh releases it
(`not_active`), the same as a session that moved the label itself.

The escape block therefore reads:

```md
### Issuebot blocked (2026-09-13T12:55:15Z)

GitHub Actions is not allocating runners for this repository; a human must clear the billing hold and re-run the checks on PR #84.
Run `20260913T124052Z-a77c09` (attempt 1, 1 turn); logs: `...`.
Moved to `issuebot/review` for a human to look at.
```

and the Slack line `:no_entry: <issue> blocked: <the same sentence>`.

### 3. The prompt

`configs/WORKFLOW.md`, ground rule 2, after "then end the turn": "Make `BLOCKED: <one line:
what is missing and the exact human action>` the first line of your final message; issuebot
escalates the issue at once, so do not spend further turns re-checking the same blocker."

The completion bar's closing sentence, "write the blocker brief in the workpad instead and
end the turn; issuebot will escalate", becomes "write the blocker brief in the workpad, make
`BLOCKED: <one line>` the first line of your final message and end the turn; issuebot
escalates at once."

Nothing else changes. Ground rule 3 already reserves the final message for completed actions
and blockers.

### 4. `run-once`

`cli.py`'s run summary adds, for `stop_reason == "blocked"`: `blocked: <blocker>; issue
#N remains <state> (the worker would escalate it)`, beside the existing `max_turns` hint.
`run-once` never moves labels, so it reports rather than escapes, as it does for `max_turns`.

### 5. What does not change

`RunEnded` and the `runs` table carry no stop reason today and gain none; the `Blocked`
event and the workpad block are the record, as they are for the turn-budget escape. No
setting: the marker is fixed. No migration. A `blocked` stop during a rework session behaves
the same. The turn-budget escape stays as the fallback for an agent that forgets the prefix;
the change only shortens the path when the agent does what the prompt says.

### 6. Trust

The marker is read from the agent's own final message, which hostile issue text can
influence. The worst it can do is end the run one turn early and move the issue to `review`
with a note a human reads, which is what the agent can already do by choosing to stop. It
cannot move the issue anywhere else or skip the workpad block.

### 7. Testing

Hermetic, like the rest of the suite.

- `tests/test_agent_session.py`: `blocker_from` on a marker line, a marker after a leading
  blank line, a marker later in the text (`None`), a bare marker (`None`), `None` input; a
  scripted run whose turn-1 result text starts with the marker stops after one turn with
  `stop_reason == "blocked"`, `outcome == "succeeded"` and the line in `blocker`, and
  publishes `RunEnded` with `turns == 1`; a turn that moved the issue to `review` and also
  carried the marker stops `issue_moved`; a failed turn carrying the marker still fails.
- `tests/test_orchestrator.py`: a worker exit with `stop_reason="blocked"` while
  `in_progress` writes the block with the agent's line, moves the issue to `review`,
  publishes `Blocked` with that reason, bumps the `blocked` counter and queues no retry; the
  same exit after the label moved to `review` is released with no escape.
- `tests/test_workflow_default.py`: the two prompt sentences render.
- `tests/test_cli.py`: `run-once` prints the blocked line.
- `tests/test_notifications_messages.py` already covers the `Blocked` line's shape.

## Out of scope

- Detecting a blocker from the workpad or from a workspace file.
- Distinguishing kinds of blocker (a credential, a runner outage) in the escape.
- Any change to what counts as a blocker: ground rule 2's definition stands.
