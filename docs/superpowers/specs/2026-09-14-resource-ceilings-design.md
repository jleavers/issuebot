# Resource ceilings: one cap per boundary an outsider can grow (#110, #139)

## Invariant

Every resource an outside party can grow -- a response's pages and bytes, a run's wall clock,
a tick's rate, a blocking wait on shared state -- carries an explicit ceiling at the boundary
where that resource is consumed, and no per-step timer is accepted as standing in for one.

## What was wrong

Each bound in the tree was attached to the step that was convenient to wrap, and then read as
if it covered the resource next to it:

| Bound as written | What it bounded | What it was taken for |
|---|---|---|
| `claude.turn_timeout_ms` wrapping `stdout.readline` | silence since the last line | a turn's, and so a run's, length |
| `github.request_timeout_ms` wrapping `process.communicate` | one `gh` process's wall clock | the response's size and page count |
| `connect_timeout` on the PostgreSQL handshake | the handshake | the statement waiting on the migration's advisory lock |
| the refresh queue | nothing | the tick rate |

`github/status.py` capping its read at `MAX_BODY_BYTES` was the one place with the right shape.

## Where each cap lives

One cap per boundary, at the seam that already owns the operation, never at its callers.

- **`GhRunner.run`** (`github/runner.py`): stdout and stderr are read incrementally; past
  `MAX_OUTPUT_BYTES` (32 MiB: GitHub's 65,536-character body ceiling is 256 KiB of UTF-8 at
  four bytes a character, so a page of a hundred is about 26 MiB) the process is killed and a
  non-retryable `response` `GitHubError` is raised. The `gh_invocation` debug line carries
  `overrun=True`. `request_timeout_ms` keeps bounding the wall clock.
- **`GhCliAdapter.find_workpad_comment`** (`github/ghcli.py`): the one caller that paginated,
  and `gh api --paginate` has no page cap, so the loop is explicit: one page at a time, oldest
  first, returning at the first marker comment by the account, and giving up after
  `MAX_COMMENT_PAGES` (10) with a `response` error. Not `None`: that answer would have the
  session open a second workpad on every turn. The blocked escape and the conflict bounce go
  through the same call, so a thread past the cap fails those loudly too
  (`blocked_escape_failed`) rather than reading forever.
  *Amended by #128:* the conflict bounce and the budget escape still fail loudly past the cap
  (the latter is #157's to revisit); the blocked escape no longer does -- it moves the label
  first and logs `blocked_escape_workpad_unreadable`. See below.
- **`GhCliAdapter.count_own_label_additions`** (`github/ghcli.py`, landed by #104 while this
  was in review): the conflict bounce's read of the issue's `LABELED_EVENT` timeline, one
  GraphQL page at a time, under the same rule: at most `MAX_TIMELINE_PAGES` (10) pages, past
  which it is a `response` error, so `conflict_rework_failed` is logged rather than a history
  anyone with triage can lengthen being read to its end. A cap bounds one read, not how often
  it is repeated, so the bounce does not simply retry next tick: `conflict_rework` returns
  `gave_up` for a `response` error (GitHub answered, and the answer is one issuebot refuses:
  a property of the issue, not of the moment) and the orchestrator keys that to the issue's
  `updated_at` (`_conflict_gave_up`, logged once as `conflict_rework_abandoned`), skipping the
  issue until it changes. Every other error (a transport error, a rate limit, a 5xx) is still
  `failed` and tried again next tick, as before.
- **`WorkspaceManager._run_argv`** (`agent/workspace.py`, #139): the other subprocess seam, and
  the same shape. `gh repo clone` and every hook ran under `process.communicate()`, which
  buffers both pipes in the worker before anything looks at them, with `hooks.timeout_ms`
  bounding the wall clock and nothing bounding the bytes -- and the worker is the process that
  supervises every concurrent session, so a flood there is not one session's. The party growing
  it is the one the deployment invites: `hooks.after_create` is where the *target* repository's
  dependency install runs, so a `postinstall` that prints, or a build that warns per file over
  a large tree, can produce gigabytes inside sixty seconds. Both streams are now read as they
  arrive and capped at `MAX_HOOK_OUTPUT_BYTES` (4 MiB each), past which the process *group* is
  killed -- the group, and through sudo under `agent.run_as`, because the writer is as often a
  grandchild of the hook shell as the shell itself and runs at a uid the worker cannot signal.
  The overrun is a fact of its own on `HookResult` rather than something read off `returncode`,
  since a hook can exit inside the pipe buffer before the reader catches up; it makes `ok`
  false, it is `overrun=True` and `max_output_bytes` in the `hook_failed` line, and it is what
  `summary` says, so the run's error quotes the cause (`before_run hook failed: output exceeded
  4194304 bytes`) rather than a truncated line of the flood. The wording does not claim the
  kill, which a hook that exited inside the pipe buffer before the reader caught up never
  received. Nor is the kill the cap's only stop: `os.killpg` raises `PermissionError` for a
  group at another uid, which is every hook's group under `agent.run_as` where the delegated
  kill did not take, so a kill that cannot land is a `hook_kill_failed` warning (`_kill_quietly`,
  at every one of the three sites, since two of them are building the `HookResult` that reports
  the failure or re-raising a cancellation) and `hooks.timeout_ms` bounds what the cap could
  not, while the reads go on dropping the bytes. The memory is bounded either way, which is the
  part that is not allowed to depend on a signal being deliverable. Much smaller than
  `GhRunner`'s 32 MiB because the resource differs: a hook's output is diagnostic, only
  `_OUTPUT_TAIL` of either stream survives into `HookResult`, and the cap is sized to what a
  chatty-but-honest install may print rather than to what issuebot needs to keep. The reader
  itself is `issuebot.pipes.read_capped`, a leaf module like `issuebot.dsn`, since this seam and
  `GhRunner`'s are the same primitive and a second copy would be a second thing to get right.
- **`GhCliAdapter._issues_with_label`** (`github/ghcli.py`, #139): the board poll's own cursor
  loop, which paginates as surely as `gh api --paginate` does -- "the one caller that
  paginated" above was true of the flag and not of pagination -- and which runs once per role
  on every tick, accumulating full `Issue` records whose bodies anyone who can get an issue
  labelled may grow to 64 KiB each. It now reads at most `MAX_ISSUE_PAGES` (10, a thousand
  issues under one state label) and **fails the read past it**, with a `response` error, rather
  than returning the pages it has.

  That decision is the one thing this cap had to settle that the others did not. `GhRunner`,
  `find_workpad_comment` and `count_own_label_additions` all fail too, and safely, because a
  refused answer is obviously not an answer. A short board is not: the query is `CREATED_AT`
  ascending, so truncation drops the *newest* issues, and the worker would claim from what was
  left believing it had seen the whole board -- starving whatever sorts last for as long as the
  board stayed over the ceiling, with nothing in a log, on a dashboard or on the issues
  themselves to say so. Failing is loud and already handled: `_fetch_issues` counts consecutive
  `GitHubError`s and holds dispatch at `MAX_FETCH_FAILURES` with a `github` hold whose reason is
  the error (#88), which `issuebot status`, `/healthz` and the dashboard's worker line all
  carry. The board stops moving *and says why*, which is the answer a deployment can act on.

  The terminal sweep's read of *closed* issues carries its own, looser ceiling,
  `MAX_TERMINAL_PAGES` (50), because it bounds a different resource: `finish_terminal` leaves
  `complete` on a closed issue for ever, so that role's pages grow with everything issuebot has
  ever finished, rather than with a working set a human queues and
  `agent.max_concurrent_agents` drains. One number for both would either strangle the sweep on
  a long-lived deployment or leave the poll a ceiling far above what it needs. That the sweep
  re-reads every completed issue at all is its own defect, not this cap's; it is filed
  separately (#149).

  The sweep also reads its roles *independently* (`_collect(per_role=True)`), and that is the
  second decision this cap had to make. All-or-nothing is right for the poll, where four roles
  are not a board; it is wrong here, and dangerously so. `terminal_sweep` is the only path to
  `finish_terminal`, and so the only thing that closes issues out, removes workspaces and,
  through `_prune_accounts`, releases session accounts -- while the role that can actually
  reach the ceiling is `complete`, which grows with everything issuebot has ever finished. One
  overgrown role refusing the whole read would stop all three on a deployment that had merely
  succeeded often enough, from a warning line: a worse failure than the cost the cap is for,
  and one the poll's `github` hold does not cover, since the sweep's failures reach no dispatch
  hold and no health surface. So a role past its ceiling is an `issue_role_skipped` warning
  naming it, the other four are still swept, and the sweep repeats.

  Skipping a role is not free, and it is worth being exact about what it costs, because the
  obvious reading -- that a `complete` issue is inert, since `finish_terminal` classifies it
  `unchanged` -- is wrong: `remove_workspace` runs outside that branch, so re-reading the role
  is the only thing that ever retries a workspace removal that failed at the time, and an
  account stays bound while its tree is on disk. That retry is what a skipped role loses, for
  the issues in that role. It is a far smaller loss than refusing the read, which loses the
  retry *and* everything else, and #149 -- not re-reading completed issues at all -- is where
  it is properly answered.

  *Amended by #149:* the sweep no longer asks for `complete` at all, so the role that could
  reach the ceiling is not read and the isolation is headroom for a backlog rather than the
  thing standing between a long-lived deployment and a sweep that never runs. See
  "The sweep's repetition" below.

  The isolation is by *type* and not by category. `PageCeilingError` is a `response`
  `GitHubError` with a name, and `_collect` catches that name alone: the category also covers a
  GraphQL `errors` payload, which is how a server-side query timeout arrives and what a large
  label-filtered query is exactly what provokes, and a malformed answer. Isolating the category
  would have had the sweep work quietly from four roles because GitHub had a bad minute. Those,
  and transport errors, still fail the whole read as they did before.
- **The session** (`agent/session.py`, `agent/runner.py`): `agent.run_timeout_ms` (default
  four hours) is a monotonic deadline fixed from `_State.started`, before the clone and the
  `before_run` hook, and handed to every `run_turn(deadline=)`. The reader waits for the
  shorter of the silence timer and the time left; a turn still running at the deadline is
  terminated with the new `run_timeout` category (outcome `timed_out`, the `turn_timeout` turn
  event), and `_turn_loop` refuses to start a turn past it. The orchestrator escapes a
  `run_timeout` while `in_progress` at once, as it does `max_turns`: a retry never resumes
  the session, so retrying would spend the same clock again from cold, `max_attempts` times
  over, and the issue's ceiling would be a multiple of the setting. `claude.turn_timeout_ms` keeps
  its name and its meaning; the README row now says it bounds silence.
- **The orchestrator's wait loop** (`orchestrator/orchestrator.py`): `_wait_for_next_tick`
  records when it started and admits a refresh only `MIN_REFRESH_INTERVAL_S` (5 s, the web's
  own throttle) after that. A refresh inside the interval brings the wait's deadline forward
  to the admissible moment and the loop keeps waiting, so a burst is one tick and none is
  dropped. **The channel is per repository** (`db/listen.py`, `refresh_channel(repo)`,
  `issuebot_refresh_<sha256(repo)[:16]>`, a digest because an identifier is 63 bytes), so a
  NOTIFY reaches the one worker it is for and never every worker on the store.
- **`db.connection.connect`**: every connection runs `SET lock_timeout` (`LOCK_TIMEOUT_S`,
  10 s) and `SET statement_timeout` (`STATEMENT_TIMEOUT_S`, 60 s) after its time zone, as
  statements rather than a libpq `options` keyword, which would replace the `options` a URL
  carries of its own. A migration blocked on the advisory lock is a `MigrationError`, exit 1,
  and the restart policy shows it. The statement timeout bounds each statement of a migration
  as well, so a future backfill over a large table sets `SET LOCAL statement_timeout` inside
  its own transaction rather than inheriting sixty seconds.

## The sweep's repetition (#149)

A ceiling bounds one read. It does not bound how often the read is repeated, and the terminal
sweep is where the two came apart -- with issuebot itself in the role of the party growing the
resource.

`finish_terminal` leaves `issuebot/complete` on a closed issue, deliberately: the label is how
the dashboard's closed column is built. So that role never shrinks, and
`fetch_terminal_issues` asked for every closed issue carrying each of the *five* state labels
on the first tick and every tenth. The pages, and the 64 KiB bodies in them, grew with
everything the deployment had ever finished, for as long as it ran. Every issue in that role
reached `finish_terminal` only to be classified `unchanged`; past `MAX_TERMINAL_PAGES` the
role was skipped with an `issue_role_skipped` warning and the sweep paid fifty pages to find
that out, on every sweep, for ever.

**The decision: do not ask for the role.** `TERMINAL_SWEEP_ROLES` (`github/state.py`) is every
state a closed issue has to be moved *off* -- `todo`, `in_progress`, `review`, `rework` -- and
not the one it comes to rest in. Those four are a working set the sweep itself drains: it
reads them, finishes every issue in them, and the next sweep finds what has closed since.

A windowed query over `complete` was the alternative, and it was rejected. It answers the cost
and nothing else: the two things the re-read was quietly doing both turn on an issue whose
`updatedAt` has not moved for months, which is exactly what a window excludes. It would also
have been a second, subtler bound to reason about -- how wide, measured from when, and what a
worker that was down for longer than the window misses -- in place of not making the request.

That the read has a ceiling at all stays true and stays useful, and `MAX_TERMINAL_PAGES` keeps
its looser number for reasons that are no longer about growth. The first sweep of a repository
that already holds a backlog of closed-but-labelled issues is legitimately large, and the
sweep is the only thing that drains it: a ceiling reached there refuses the one read that
would bring the role back under it. And the two reads fail differently -- the poll's is
all-or-nothing because four roles are not a board, this one skips the role and sweeps the rest
-- so a number tuned for one is not a number for the other.

### What the re-read was load-bearing for, and where each half went

**The store's refresh.** `terminal_sweep` hands `_report_issues` each issue as it *found* it,
carrying the label `finish_terminal` is about to take off it. The row the store then holds is
a moment out of date, and what put it right was the next sweep re-reading the issue in its new
role -- at the price of re-reading every other completed issue with it. The sweep now reads
back, by number, only the issues it actually relabelled (`_report_moved` →
`fetch_issues_by_ids`), which is bounded by the work of one sweep and costs no request at all
in the steady state, where a sweep moves nothing. A read-back that fails is
`terminal_refresh_failed` at WARNING and leaves the row as the sweep found it: the label move
itself reaches the store through `state_changed` and `issue_completed`, so what a failure
costs is an issue's stored label list and title, never its state or the board.

Every answer is checked against the state the sweep moved the issue *to*, and one that still
shows the old role is dropped (`terminal_refresh_stale`). The move is written through `gh
issue edit` and read back over GraphQL milliseconds later, so an answer from a replica that
has not caught up is a real possibility -- and it would carry the newest `seen_at`, which is
what `UPSERT_ISSUE` orders by, so reporting it would overwrite the state `state_changed` had
just recorded. The old scheme repaired that on the next sweep; nothing reads a `complete`
issue again now, so it would be permanent. The refresh is the cheap half and the state is the
load-bearing half: when they disagree the refresh gives way.

What *does* stop happening is the periodic re-read of an issue that is already `complete` and
that this sweep did not touch -- so a title, a label, a URL or a linked pull request's state
and merge time, edited or moved on a closed issue months after issuebot finished with it, no
longer reaches `issues`. Those are columns the dashboard displays and nothing in issuebot
reads to make a decision: the board and its counts are `state`, written by the event, and a
closed issue's row is not a live thing to keep polling. That is the trade, stated plainly.

**The retry of a failed workspace removal.** `remove_workspace` runs outside
`finish_terminal`'s `unchanged` branch, so re-reading the role was also the only thing that
ever retried a removal that failed at the time -- and under a pool an account stays bound
while its tree is on disk (#121). Simply dropping the role would have dropped that retry with
it, which is the part of #149 that is not visible from the query.

The answer is that the retry is a property of the workspace, not of the issue list.
`.issuebot/finished` is the intent recorded ahead of the act, so a removal that failed leaves
a workspace that says what should have happened to it, and `finished_keys()` reads the
candidates off the disk. The sweep then removes each again through `_workspaces_for_key`, as
the account bound to that workspace, since the unlink is the session's files'. The resource is
now bounded by the failures that put those directories there rather than by everything
issuebot has ever completed, and the record is on disk, so it survives a restart where a memo
in the process would not.

It is written at two moments, and both are needed. `finish_terminal` marks *before* it moves
the label, because a worker killed between the move and the removal would otherwise leave an
issue at rest in `complete` -- which nothing reads again -- beside a workspace nothing would
remove. And `remove` re-asserts the mark in its `finally`, where a removal is known to have
failed, because `rmtree` is not atomic: it walks the workspace's entries in readdir order and
stops at the first it cannot unlink, having already taken everything it reached, which on half
the orderings is `.issuebot` with the mark inside it. A partial removal that ate its own mark
would leave a workspace nothing ever removes again, and under a pool an account bound to it
for ever. The state directory is recreated, closed, when that is what is missing.

The marker is created exclusively, through `Boundary.create_marker`, for the reason the
`created` sentinel is (#75): `.issuebot` is shared with the session under `agent.run_as`, so a
name already there may be the session's, and `finished_keys` re-checks that the workspace is
the worker's own directory and the mark the worker's own regular file, reached through no
symbolic link (#104). Those two together would be a trap on their own -- a session that
pre-placed the name would make `create_marker` fail while `finished_keys` refused what it
found, and the retry would be silently gone -- so a name that is not the worker's own file is
taken back (`workspace_mark_replaced`, then `remove_marker` and create). The directory is the
worker's; only the names inside it are shared. Writing the mark is still best effort: what it
buys is the retry, so a workspace too damaged to carry one is still a workspace to remove, and
the failure is `workspace_mark_failed` at WARNING.

`create_or_reuse` clears the mark on the reuse path (`Boundary.remove_marker`), because a
reopened issue is not one issuebot is done with; and the sweep skips any key with a session
running or a retry pending -- the same set `_prune_accounts` keeps a binding for -- so a run
in flight is never swept out from under. A clear that fails is said and not raised
(`workspace_unmark_failed`): what an uncleared mark costs is a clone the next sweep removes
and the run after that re-makes, never work, which is pushed.
`_retry_finished_workspaces` runs before `_prune_accounts`, so a workspace this sweep finally
removed gives its account back on the same sweep.

### What this does not cover

Two cases the `complete` re-read reached and the mark does not, both stated rather than
closed. A removal whose *mark* could not be written and which then fails is not retried; the
two failures are independent and both are logged, and the second attempt in `remove`'s
`finally` is a third chance at the first. And a workspace already orphaned when a deployment
takes this change carries no mark, since nothing wrote one: the old sweep was retrying those
for as long as their issues sat in `complete`, and after the upgrade an operator removes what
is left under `workspace.root` by hand. Neither is a growing resource -- both are bounded by
failures that have already happened -- which is what makes them acceptable here.

## Not done here

- `blocked_escape` still needs the workpad before it moves the label, so an issue whose
  thread has outgrown `MAX_COMMENT_PAGES` before the escape is bounded (ten pages every five
  minutes, each failure logged) but not escaped. Whether the escape should move the label
  first and note the block best-effort is a separate decision.
  *Amended by #128:* it is, and the answer is label-first. A non-retryable read -- the cap, a
  malformed page -- moves the issue to `review` on the first attempt and then appends the
  block blind, as a fresh marker comment, best-effort; a retryable one keeps the retry. The
  read is the block's run-marker idempotence, so the trade-off taken is a possible duplicate
  note against an issue that never leaves `in_progress`.
- #104's `WORKSPACE_ENV_LIMIT` and blocking read are the same defect in a different resource
  and are fixed there.
- **`_conflict_gave_up` is keyed on `issue.updated_at`**, which is "until the issue changes" as
  the acceptance criterion asks -- but a commenter moves `updated_at`, so an issue whose label
  history is past `MAX_TIMELINE_PAGES` can be made to cost ten pages again per comment. That is
  a far smaller ceiling than the per-poll cost it replaces, and the memo is the bound on
  repetition rather than on the read, which `MAX_TIMELINE_PAGES` already holds. A floor (N
  ticks, or a state-label change) would close it; it is not closed here.
