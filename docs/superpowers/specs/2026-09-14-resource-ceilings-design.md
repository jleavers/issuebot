# Resource ceilings: one cap per boundary an outsider can grow (#110)

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
- **`GhCliAdapter.count_own_label_additions`** (`github/ghcli.py`, landed by #104 while this
  was in review): the conflict bounce's read of the issue's `LABELED_EVENT` timeline, one
  GraphQL page at a time, under the same rule: at most `MAX_TIMELINE_PAGES` (10) pages, past
  which it is a `response` error, so `conflict_rework_failed` is logged and the bounce retried
  next tick rather than a history anyone with triage can lengthen being read to its end.
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

## Not done here

- `blocked_escape` still needs the workpad before it moves the label, so an issue whose
  thread has outgrown `MAX_COMMENT_PAGES` before the escape is bounded (ten pages every five
  minutes, each failure logged) but not escaped. Whether the escape should move the label
  first and note the block best-effort is a separate decision.
- #104's `WORKSPACE_ENV_LIMIT` and blocking read are the same defect in a different resource
  and are fixed there.
