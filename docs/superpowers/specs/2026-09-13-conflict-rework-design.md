# The worker bounces a conflicting review PR to rework

Date: 2026-09-13
Status: approved, not implemented

## Problem

Sessions run concurrently, and every one of them forks its branch from the same default
branch. When two of them open pull requests, the first to merge makes the second conflict,
often over something as small as a README line. By then the second session has ended and
its issue sits in `review`, so nothing touches the branch again until a human notices the
red merge box, sets `rework`, and waits for a session to resolve it.

PR #83 made the session keep its own branch mergeable: Step 5 of the prompt merges the
default branch before the push, and Step 6, which every revisit runs first, reads the pull
request's `mergeable` back and merges again on `CONFLICTING`. That catches a conflict the
session can see. A conflict that appears *after* the issue reaches `review` is invisible to
it, and that is the common case: with two agents and one human merging, the sibling PR
usually lands after the session is over.

So the requirement is: **the worker notices a `review` issue whose pull request has become
`CONFLICTING` and moves it to `rework` itself, bounded, so the existing rework path resolves
it while the workspace is still on disk.**

## Decision

On every poll the worker fetches `review` issues alongside the candidates, and for each
whose linked pull request is open and `conflicting` it appends one block to the workpad,
sets `rework`, and publishes the transition as its own. The next tick dispatches the issue
exactly as it would a human-set rework: same session, same prompt, where Step 6 resolves the
conflict before anything else. A single setting, `agent.max_conflict_reworks`, caps the
bounces per issue (default 3); at the cap the worker writes one more block saying it has
stopped and leaves the issue in `review` for a person. `0` turns the feature off.

### Why not the alternatives

**Claim it straight into `in_progress`** with a `conflict` flag rendered into the prompt was
rejected. It is one poll interval faster, but it adds a prompt variable and a second
transition, the board never shows why the issue left `review`, and the bounce count would
have to live in memory or the database. Through `rework`, everything a human sees, the
label move, the workpad block and the Slack line, already exists.

**A marker label** (`issuebot/conflict` beside `review`) with a human doing the bounce was
rejected because it is not automatic, which is the whole ask. It remains what the off switch
degrades to: with the setting at `0` the worker does nothing, and the PR's own merge box is
the marker.

**Counting bounces in memory** was rejected because restarting is how the worker is
deployed, and a count that resets on restart is a cap that does not hold. The workpad is
the source of truth the rest of the system already writes to, and a count a person can read
there is a count they can argue with.

## Design

### 1. Data: the pull request's mergeability

The issue query's pull request fragment asks for one more field:

```graphql
closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {
  nodes { number url state mergedAt mergeable }
}
```

`mergeable` is GitHub's `MergeableState`: `MERGEABLE`, `CONFLICTING` or `UNKNOWN`. Asking
for it is also what makes GitHub compute it, so a pull request that reads `UNKNOWN` on one
poll usually answers on the next; the worker never acts on `UNKNOWN`.

`LinkedPr` gains `mergeable: Mergeable`, a `Literal["mergeable", "conflicting", "unknown"]`
defaulting to `"unknown"`, so every existing constructor (the normaliser on an older
response, the fake, the tests) keeps working. The normaliser lowercases the three values and
treats anything else, or the field's absence, as `unknown`. `_select_pr` carries it through
unchanged; the ranking that picks one PR out of several is not affected.

`FakeGitHub`'s `_FakePr` gains `mergeable: Mergeable = "mergeable"`, and a test sets it the
way it sets `state` today.

The database's `issues` row does not change. It stores the PR's number, URL and state
because those are history; mergeability is a moment, and the dashboard has no use for it.

### 2. One setting

```yaml
agent:
  max_conflict_reworks: 3   # 0 turns the automatic bounce off
```

`AgentSettings.max_conflict_reworks: int = Field(default=3, ge=0)`. One knob rather than a
flag and a limit: `0` is off, anything else is the cap.

When the setting is above `0`, the tick's fetch includes `review` on every poll, not only
when an `on_issues` observer is attached. `OBSERVED_STATES` stays what it is; the choice of
states moves into a small pure function, `fetch_states(observed: bool, conflicts: bool)`,
so the two reasons for wanting `review` are one line each and testable without a tick.

### 3. Detection

After every successful fetch, in `_dispatch_candidates` and in `_poll_issues` alike (a held
worker still polls, and a conflict is about the account's board, not its dispatch), the
orchestrator walks the fetched `review` issues and hands each one that qualifies to
`conflict_rework` (section 4). An issue qualifies when all of these hold:

- its state is `review` and it is `dispatchable` (open, carrying exactly one state label);
- it has a linked pull request whose `state` is `open` and whose `mergeable` is
  `conflicting`;
- it is not in the running table (a session in its review grace) nor in the retry queue.

The last rule keeps the bounce from racing the reconcile: a running entry whose issue moved
`review → rework` would report the move as a human's, and stop the session for the wrong
reason. Once the entry is gone, the next poll bounces the issue.

An issue is considered once per tick, and after a bounce it is no longer in `review`, so
the next fetch does not return it. There is no debounce beyond the state itself:
`CONFLICTING` is the result of GitHub's own test merge, not a heuristic.

### 4. The bounce: `conflict_rework` in `actions.py`

Beside `blocked_escape`, sharing its workpad helpers:

```python
async def conflict_rework(
    adapter: GitHubAdapter,
    bus: EventBus,
    issue: Issue,
    *,
    limit: int,
    now: datetime,
) -> ConflictOutcome   # Literal["reworked", "limit_reached", "limit_noted"]
```

1. Read the workpad (`find_workpad_comment`). Count the blocks whose heading begins
   `### Issuebot merge conflict` and does not begin `### Issuebot merge conflict limit`.
   That count is how many times this issue has already been bounced; it survives a restart
   and a person can read it.
2. **Under the cap** (`count < limit`): `set_state(issue.number, StateLabel.REWORK)` first,
   then append this block (created with the workpad if there is none, as the escape does):

   ```md
   ### Issuebot merge conflict (2026-09-13T10:41:07Z)

   Pull request #51 conflicts with the default branch (bounce 1 of 3).
   Moved to `issuebot/rework`: the next session merges the default branch into the
   branch, resolves the conflict, re-runs validation and returns the issue to
   `issuebot/review`.
   ```

   Label first, note second. If the note fails, the rework session still resolves the
   conflict through Step 6 and the count is short by one, which errs toward one more
   bounce; a note without the label would be counted again on the next tick, which errs
   toward a bounce nobody made. Then publish
   `StateChanged(from=review, to=rework, actor="issuebot", pr_url=...)`, so the Slack line
   reads `review → rework by issuebot · PR #51`. Return `reworked`.
3. **At the cap** (`count >= limit`): if the workpad already carries a
   `### Issuebot merge conflict limit` block, return `limit_noted` and do nothing (the
   orchestrator logs it at debug). Otherwise append:

   ```md
   ### Issuebot merge conflict limit (2026-09-13T11:02:40Z)

   Pull request #51 conflicts with the default branch again. issuebot has moved this
   issue to `issuebot/rework` 3 times for it and will not again. A human resolves the
   conflict on the branch, or moves the issue.
   ```

   Leave the issue in `review`, publish nothing, return `limit_reached`. The block's
   presence is the idempotence.
4. A `GitHubError` anywhere logs `conflict_rework_failed` with the issue and the PR, and
   the next tick retries from step 1.

The transition table gains one row, `(REVIEW, REWORK, Actor.ISSUEBOT)`. `observe_transition`
is untouched: the bounce reports itself the way `claim` does, and the reconcile never sees
it because of the running-table rule above.

Logging: `conflict_rework` (INFO: issue, PR, bounce `n` of `limit`), `conflict_rework_limit`
(WARNING, once, when the limit block is written), `conflict_rework_failed` (WARNING).

### 5. Prompt

The rework context's first line changes from a reviewer alone to either author:

> A reviewer moved this issue from `review` to `rework` because the pull request needs
> more work, or issuebot did because the pull request conflicts with the default branch;
> the workpad's last `### Issuebot merge conflict` block says which.

Nothing else in the prompt changes: Step 6, as of PR #83, already merges the default
branch on `CONFLICTING` before the feedback sweep, and the rework flow already runs Step 6
first.

### 6. Documentation

- `LABEL_STYLES[REWORK]`'s description becomes "Reviewer wants changes, or the PR conflicts
  with the default branch; issuebot will pick it up". `labels ensure` updates it.
- The label tables in `CLAUDE.md`, `README.md` and `docs/BLUEPRINT.md`: `rework` is set by
  a human, or by issuebot when the pull request conflicts.
- The settings reference in the README gains `agent.max_conflict_reworks`.
- `CLAUDE.md`'s `issuebot.orchestrator` paragraph gains a sentence on `conflict_rework` and
  the workpad count.

### 7. Testing

Hermetic, like the rest of the suite.

- `tests/test_github_normalise.py`: `mergeable` parsed for each of the three values; absent or
  unrecognised reads `unknown`.
- `tests/test_github_fake.py`: the field round-trips
  through `_node` and the normaliser.
- `tests/test_github_state.py`: `is_allowed(REVIEW, REWORK, ISSUEBOT)`.
- `tests/test_settings.py`: the default is 3, `0` is accepted, `-1` is rejected.
- `tests/test_orchestrator_actions.py`, with `FakeGitHub` and a recording bus: a bounce moves the label,
  appends the block with `bounce 1 of 3`, and publishes one `StateChanged` by issuebot with
  the PR URL; a workpad with two blocks bounces once more and reads `3 of 3`; with three,
  the limit block is written, the label stays `review`, nothing is published; a second call
  at the limit changes nothing; a missing workpad is created; a `fail_next` on `set_state`
  leaves no block.
- `tests/test_orchestrator.py`, with the fake clock: a conflicting review issue is bounced
  on one tick and dispatched as `rework=True` on the next; one whose entry is still in
  review grace is left alone until the entry is gone; one whose PR is merged, closed or
  `unknown` is left alone; the setting at `0` fetches `review` only when an observer is
  attached (the `fetch_states` table); a held worker still bounces.
- `tests/test_workflow_default.py`: the rework render carries the new wording.

### 8. Rollout

The change is additive: an older worker ignores the field, and the setting's default turns
the behaviour on for every workflow that upgrades, which is the behaviour asked for.
Operators who resolve conflicts by hand on the agent's branch set it to `0`.

## Out of scope

- A conflict on a `rework` issue nobody has dispatched yet: it will be resolved when it is.
- A conflict on an `in_progress` issue: the running session's Step 6 owns it.
- Reacting to the sibling merge itself (a webhook, or watching `main`): polling
  `mergeable` is enough at one poll interval, and it needs no new surface.
- Showing mergeability on the dashboard.
