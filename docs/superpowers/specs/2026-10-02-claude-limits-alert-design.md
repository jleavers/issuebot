# A Slack alert for the Claude usage limits

Date: 2026-10-02
Status: draft, awaiting review

## Problem

The dashboard's limits tile shows how much of the Claude subscription's two usage windows has
been spent -- the 5-hour window and the 7-day one -- but nobody is told when either runs out.
When Claude refuses a turn on usage, the worker holds dispatch until the window reopens
(#173's usage hold) and the board stops; the only sign is the worker line on a dashboard
nobody is watching at that moment. The 7-day window is the one that matters most: spent early,
it stops every deployment on the host for days.

The GitHub Actions minutes alert (spec 2026-10-02, `actions-minutes`, PR #265) posts to Slack
at 75%, 90% and 100% of a monthly allowance. This is the same idea for Claude, shaped by how
differently the two figures behave.

## What the worker already knows

Established from the code and from the live store on 2026-10-02.

- **Readings.** `claude` reports the windows in a `rate_limit_event` line during a turn
  (`agent/runner.py`, `parse_rate_limits`): for `five_hour` and `seven_day`, a `utilization`
  between 0 and 1 and a `resets_at`. The worker keeps the newest reading in memory and writes
  it into its repository's row of `runtime_snapshot` (`data.rate_limits`, with
  `observed_at`). A reading only moves while one of that worker's sessions is running a turn.
- **Readings go stale.** On 2026-10-02 three of the host's four snapshots carried readings
  from days earlier (one from 18 September), because those workers had not run a turn since.
  The newest was minutes old.
- **One subscription reports one reset time.** Two workers' snapshots carried identical
  `resets_at` values for both windows (`2026-10-01T11:40:00+00:00`, `2026-10-02T05:00:00+00:00`),
  and every reset seen fell on a whole minute. So `(window, resets_at)` names one window of one
  subscription, whichever worker read it.
- **Refusals.** When claude refuses a turn on usage, the `rate_limit_event` says
  `status: "rejected"` with a `rateLimitType` (the window: `five_hour`, `seven_day`, and
  possibly others) and a `resetsAt` (`parse_usage_limit` -> `UsageLimit(window, resets_at)`).
  The reset travels as `usage_reset_at` through the turn result, the session state and
  `RunResult` to the orchestrator, which holds dispatch until then (`_hold_usage`,
  `_settle_usage_hold`). The window name is dropped after the runner.
- **The hold in the snapshot** is `DispatchHold(kind, reason, since)`. For a usage hold `kind`
  is `usage` and `reason` is `claude usage limit reached: <claude's sentence>`; neither the
  reset time nor the window is recorded there.
- **Nothing posts to Slack** for a reading or for a usage hold today.

## Design

### 1. What is posted, and when

- **7-day warnings.** When a 7-day reading's utilisation reaches 75%, and again at 90%, post
  once for that window. A window is identified by its reset time, so the next week starts
  afresh. A reading that jumps past both thresholds posts the higher alone.
  - `:warning: Claude: the 7-day usage window is 77% used; it resets Fri 9 Oct, 05:00 UTC.`
- **A limit hit.** When any worker holds dispatch because claude refused a turn on usage,
  post once for that window and reset time, however many workers hit the same wall:
  - 5-hour: `:rotating_light: Claude: the 5-hour usage limit is reached; issuebot stops
    claiming issues until 20:00 UTC (in 2 h 13 min).`
  - 7-day: `:rotating_light: Claude: the 7-day usage limit is reached; issuebot stops claiming
    issues until Fri 9 Oct, 05:00 UTC (in 6 d 12 h).`
  - another window claude names: `... the usage limit (<name as claude reports it>) is
    reached; ...`; no window at all: `... the usage limit is reached; ...`.
- **The 7-day window has no separate 100% warning.** Reaching 100% is what makes claude refuse,
  and the refusal is the hit message; a reading-driven 100% would say the same thing twice.
- **The 5-hour window has no warnings.** It resets several times a day; 75% and 90% on it
  would be noise. It speaks only when it actually stops the board.
- **No "resumed" message.** The hit names the time work resumes.
- **Times are UTC**, as the dashboard's stamps are. A reset on the same UTC date as the moment
  of posting is `HH:MM UTC`; any other is `Ddd D Mon, HH:MM UTC` (day and month by `%a` and
  `%b` with `.day`, not the glibc-only `%-d`, since the repository is also used from Windows).
  The relative part is `in N min` under an hour, `in N h M min` under a day, else `in N d M h`.
- **Who posts.** The hub's `web`, whenever it has `SLACK_WEBHOOK_URL` -- the webhook the
  Actions alert already uses, passed to `web` since PR #265. The billing token is not needed.
  `notifications.slack.events` does not govern it: that list is the worker's, read from the
  workflow, which the web never reads.
- **The dashboard does not change.** The limits tile already shows both windows.

### 2. The worker: the usage hold says when it lifts and which window

The hit message needs the reset time and the window, and the reset time is also what lets four
workers on one subscription post once. Both are known to the worker and lost before the
snapshot:

- `UsageLimit.window` travels beside `usage_reset_at`, as `usage_window: str | None`: from the
  runner's turn result, through the session state and `RunResult`, to `_usage_limited`.
- The orchestrator keeps, beside `_usage_reset_at`, the reset claude reported
  (`_usage_until`) and the window that came with it (`_usage_window`). `_usage_until` takes only
  a reset claude reported, never the one-interval fallback a refusal without one is given: that
  fallback moves every time it is taken, and carrying it would hand the web a fresh key -- and
  Slack a fresh alert -- every poll interval. It moves out only, as `_usage_reset_at` does, and
  the window moves with it, so `until` and `window` always describe the same refusal. `Hold`
  (`orchestrator/admission.py`) and `DispatchHold` (`orchestrator/state.py`) gain
  `until: datetime | None = None` and `window: str | None = None`, set only for the usage hold,
  from `_usage_until` and `_usage_window`. A hold that lasts keeps its `since`, as it does
  today, and takes the newer `until` and `window` along with its newer reason.
- The snapshot's `dispatch_hold` therefore carries `until` and `window` with no change to how
  it is written. `views.dispatch_hold` passes them through when present, so `/api/v1/state`
  carries them too; older snapshots without them read as before.

Nothing else in the worker changes, and the worker posts nothing.

### 3. Storage: migration `0006_claude_limit_alerts.sql`

The alert's memory, so a restart -- which every upgrade is -- and a second web process never post
the same alert twice:

```sql
CREATE TABLE claude_limit_alerts (
    limit_window    text NOT NULL,              -- claude's window name: five_hour, seven_day, ...
    resets_at       timestamptz NOT NULL,       -- when that window reopens: which instance of it
    alerted_percent integer NOT NULL DEFAULT 0, -- highest posted: 75 or 90, or 100 for a hit
    PRIMARY KEY (limit_window, resets_at)
);
```

(`window` is a reserved word in PostgreSQL, hence `limit_window`.)

Claim-then-post, as the Actions alert does:

- **Read** the row's `alerted_percent` for `(limit_window, resets_at)`, 0 when there is none.
- **Claim** only when the target is above it: an upsert that inserts the target or raises the
  stored value, conditional on the stored value being below the target, reporting whether it
  wrote. Only the process whose claim wrote posts.
- **Release** after a failed post: set the value back to what was read, conditional on it
  still being the target.

Rows are a handful a week (at most two warnings per 7-day window and one per hit); nothing
prunes them.

### 4. The watcher: `issuebot.web.claude_limits`

A background task in the web, started and stopped by the app's lifespan beside the Actions
poller, built by the CLI whenever `SLACK_WEBHOOK_URL` is set. One cycle at start, then every
`INTERVAL_S` (60, a constant). Each cycle is one read of `runtime_snapshot` through the existing
`queries.snapshots()` -- a handful of rows, no API call, no GitHub or Claude credential:

1. **Parse** each snapshot's JSON defensively, as `views` does: the 7-day reading
   (`rate_limits.seven_day.utilization`, `resets_at`) and the usage hold
   (`dispatch_hold.kind == "usage"`, `until`, `window`). Anything unreadable is skipped.
2. **7-day warnings.** Drop readings whose `resets_at` has passed. For each distinct
   `resets_at` left, take the highest utilisation any snapshot reports (one subscription is
   one key; utilisation only rises within a window, so the highest is the newest). The target
   is the highest of `SEVEN_DAY_THRESHOLDS` (75, 90) with `utilization * 100 >= threshold`
   (the message shows the utilisation rounded to a whole percent); claim
   `("seven_day", resets_at)` at it and post the warning if the claim wrote.
3. **Hits.** For each usage hold whose `until` is still in the future, claim
   `(window or "unknown", until)` at 100 and post the hit if the claim wrote. A hold without
   `until` -- a worker not yet upgraded, or a refusal claude did not date -- is skipped. A 7-day
   warning whose reset is also a 7-day hit in the same cycle is skipped: the hit says more.
4. **Posting** is one attempt through `notifications.slack.urllib_post`, which never raises and
   keeps the URL out of every error. A failed post logs `claude_limits_alert_failed`, releases
   the claim, and holds further posting for that key for `RETRY_BACKOFF_S` (900, a constant,
   kept in memory): a dead webhook is tried every fifteen minutes, not every minute. A
   successful one logs `claude_limits_alert_sent`.

The message text is a pure `notifications.messages.format_claude_limit_alert(...)`, whole
percent and mrkdwn-escaped window names, beside `format_actions_alert`.

`create_app` gains a second keyword, `claude_limits: ClaudeLimitsWatcher | None = None`,
beside `actions`, started and stopped by the same lifespan and kept on `app.state`. The CLI
builds the watcher from `SLACK_WEBHOOK_URL` (stripped; empty is unset) with the database as its
store and query source; the web's log line at start says whether it is on.

Cost: one indexed read of a table with one row per repository, once a minute. No API traffic.

### 5. Failure modes

| Situation | Effect | Log |
|---|---|---|
| `SLACK_WEBHOOK_URL` unset | no watcher; nothing posts | the web's start line says so |
| Hub `web` down | nothing posts; on return, a still-future hold or an unexpired reading past a threshold posts once | |
| No issuebot turn lately | readings do not move, so no new warning; use from interactive Claude sessions on the same subscription shows at issuebot's next turn | |
| A worker not yet upgraded | its usage hold has no `until`, so no hit message from it | |
| A hold outranked in the snapshot | the snapshot shows one hold; a `preflight` or `auth` hold outranks `usage`, so a usage hit under it is not seen until that clears | |
| Window name claude has not used before | the message names it as reported, escaped | |
| Post fails | claim released; that key retried after 15 minutes | `claude_limits_alert_failed` |
| Database error | the cycle ends; the next retries | `claude_limits_store_failed` |
| Two hub webs on one store | the conditional claim lets one post | |

### 6. Security

The watcher holds the webhook and nothing else: no GitHub token, no Claude credential, and it
calls no API. It reads only `runtime_snapshot`, which the web already reads to draw the
dashboard, and writes only its own table. Slack text carries a window name from claude and
times; the window name is escaped like every other free text posted.

### 7. Documentation

- `docs/dashboard.md`: a short section beside "GitHub Actions minutes" on the Claude limits
  alert -- what posts and when, that it needs only the webhook, and that readings only move
  while issuebot runs turns.
- `docs/operations.md`: a short `### A spent Claude usage window` under "When things go wrong"
  -- the usage hold has no operator text yet -- saying what the worker does, what
  `issuebot status` shows, that the hold lifts on its own, and that Slack now says when.
- `README.md`: the sentence saying the hub's `web` reads `SLACK_WEBHOOK_URL` for the Actions
  alert covers the Claude alert too.
- `docs/package-layout.md`: short additions for `orchestrator` (the hold's `until` and
  `window`), `db` (the table and its read, claim and release), `web` (`claude_limits.py` and
  `create_app(claude_limits=)`) and `notifications` (`format_claude_limit_alert`). Under 2 KB
  in total.
- `CLAUDE.md`: nothing; the file has a budget.
- No screenshot regeneration: the dashboard does not change.

### 8. Testing

- Worker: a refusal's `rateLimitType` reaches `RunResult.usage_window`; the usage hold in the
  snapshot carries `until` and `window`; a refusal claude did not date leaves `until` empty; a
  second refusal with a later reset moves `until` out
  and keeps `since`; `views.dispatch_hold` passes the two through and tolerates their absence.
- `web.claude_limits`, with fake queries, store and poster: 75 then 90 post once each; a jump
  posts 90 alone; a new `resets_at` starts afresh; expired readings and holds are ignored; the
  highest utilisation per reset time is used; a hold without `until` is skipped; two workers'
  holds on one wall post once; a later wall posts again; a restart (a new watcher, same store)
  posts nothing new; a failed post releases the claim and is retried only after the backoff; a
  database error is logged and the next cycle retries.
- Messages: exact wording for both kinds, the same-day and other-day time forms, the three
  relative forms, unknown and absent window names, escaping.
- Database (skipped without `DATABASE_URL`): the migration applies; claim inserts and raises,
  refuses an equal or lower target, and release restores only its own claim.
- CLI: the watcher is built when the webhook is set, with or without the billing token; not
  without it.
- App: the lifespan starts and stops both background tasks.

## Out of scope

- Warnings on the 5-hour window, and configurable thresholds.
- A "resumed" message when a hold lifts.
- Reading the windows outside issuebot's own turns (claude reports them only in a turn).
- Telling subscriptions apart in the message: this host runs one.
- Any change to the dashboard.
