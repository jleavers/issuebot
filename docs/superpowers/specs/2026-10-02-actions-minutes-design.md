# GitHub Actions minutes on the dashboard, and a Slack alert when they run low

Date: 2026-10-02
Status: draft, awaiting review

## Problem

The dashboard's limits tile shows what a Claude subscription is rationed by -- the 5-hour and
7-day usage windows -- and nothing about the other ration every deployment spends: the
billing account's included GitHub Actions minutes. When those run out, every workflow in a
private repository stops starting, and the symptom is a red check with zero steps whose
annotation blames payments (`docs/operations.md`, "Checks that never ran"). That happened in
September 2026, and again at the start of October a diagnostic left in a sibling deployment's
CI ran it repeatedly and spent 2,244 of the month's 3,000 minutes in a day and a half. Nothing
on the dashboard said so; the billing page on github.com did.

The goal is that figure on the dashboard, as a third window in the limits tile, and a Slack
message when it crosses a threshold -- without adding meaningfully to GitHub API traffic, and
without putting the account owner's credential anywhere a session runs.

## What GitHub provides

Established by a read-only probe against the owner's account on 2026-10-02, not taken from the
documentation alone.

- **Endpoint.** `GET /users/{account}/settings/billing/usage/summary?product=actions` returns
  the current calendar month (`timePeriod: {year, month}`) as `usageItems[]`, one per SKU, each
  with `unitType`, `grossQuantity`, `discountQuantity` and `netQuantity`. The SKUs seen were
  `actions_linux` and `actions_windows` (`unitType: "minutes"`) and `actions_storage`
  (`"gigabyte-hours"`). The detailed `GET .../billing/usage` report spells the same unit
  `"Minutes"` and the SKUs as display names (`"Actions Linux"`), so a parser compares the unit
  case-insensitively.
- **Credential.** The endpoint answers only the account itself, through a *classic* token
  carrying the `user` scope: the response header is `X-Accepted-OAuth-Scopes: user`, a token
  without it gets a 404, and GitHub's documentation says the billing usage endpoints do not
  accept fine-grained tokens. No narrower credential exists. A deployment's bot token
  (`jleavers-issuebot` and its siblings, since #246--#248) cannot read the owner's billing at
  all, so this is necessarily a second, separate token.
- **Budget.** The call is REST and counts against the token's own `core` budget (5,000 an
  hour), not the GraphQL points the workers poll with and have run short of
  (`X-RateLimit-Resource: core`).
- **The figure the billing page shows is every Actions minute, public repositories included,
  with Windows at 1x.** On 2026-10-02 the summary gave 2,192 Linux + 114 Windows = 2,306 while
  the page read 2,308 a minute later; the detailed report split the Linux figure into 2,130
  private and 62 public (this repository). Private-only (2,244) and a 2x Windows multiplier
  (2,420) were both ruled out against the page. So "used" is the sum of `grossQuantity` over
  the items whose unit is minutes, from the summary alone: one call.
- **The allowance is not in the API.** Nothing returns the 3,000 included minutes of the
  account's plan, so it is a setting.
- **The cycle is the calendar month (UTC).** Confirmed by the operator: the allowance reset on
  1 October. The summary's `timePeriod` names it, so the period is read from the response
  rather than from issuebot's clock.

## Design

### 1. Settings, and where the credential lives

Three environment variables, read by `issuebot web` and by nothing else:

| Variable | What it is |
|---|---|
| `ISSUEBOT_GITHUB_BILLING_TOKEN` | A classic token from the billing account with only `user` ticked. Unset: no polling, no alert. |
| `ISSUEBOT_ACTIONS_INCLUDED_MINUTES` | The plan's monthly allowance (3000 for GitHub Pro). No default: a guessed one would misreport silently. Set but not a positive integer: `issuebot web` refuses to start, naming it. |
| `SLACK_WEBHOOK_URL` | The hub checkout's existing webhook, now passed to `web` as well as the worker. Unset: the tile works and no alert is posted. |

The poller runs in the **web service on the hub**, and only there. That is one poller for the
host however many workers it runs, and the web never runs a session: the worker container is
where a hostile issue's session executes (as `agent-N`), and putting the owner's credential
there would run against #246--#248, which moved the workers *off* the owner's account.

Compose passes the three to `web` as optional pass-throughs (`${...:-}`), the way
`ISSUEBOT_WEB_PASSWORD` is passed. **The worker loads the whole `.env` through `env_file`,**
so a token written into the hub's `.env` would otherwise sit in the hub worker's environment
too. The worker's `environment:` block, which overrides `env_file`, therefore sets
`ISSUEBOT_GITHUB_BILLING_TOKEN: ""`, and a test pins it. Sessions would not have received it
regardless -- `agent_environment` is an allow-list and `ISSUEBOT_` is not on it -- but the
worker process should not hold a credential it never uses, and a test pins that too.

### 2. Fetching: `issuebot.github.billing`

Pure parsing and two fetches, through the existing `GhRunner` (the only place that spawns
`gh`, with its timeout, output cap and fixed environment), constructed with the billing token:

- `fetch_login(runner) -> str`: `gh api /user`, `.login`.
- `fetch_actions_usage(runner, account) -> ActionsUsage`: the summary above.
- `parse_summary(document) -> ActionsUsage(period: date, used_minutes: float)`: `period` is
  the first day of `timePeriod`'s month; `used_minutes` sums `grossQuantity` over items whose
  `unitType` is `minutes` in any case. A missing `timePeriod` or a non-numeric quantity is a
  `GitHubError("response", ...)`.

Failures surface as `GitHubError`, as everywhere else in the package.

### 3. Storage: migration `0005_actions_minutes.sql`

The reading is stored rather than held in memory, because the Slack alert must remember what it
has already sent across web restarts -- every upgrade restarts the web -- and the reading and
that memory belong in the same row. It also keeps the tile filled through a restart, and lets
`tools/screenshots/seed.py` show the window by inserting a row.

```sql
CREATE TABLE actions_minutes (
    account          text PRIMARY KEY,   -- the token's login, as GET /user returned it
    period           date,               -- first day of the month the reading is for
    used_minutes     numeric,            -- null until the first successful read
    included_minutes integer,            -- ISSUEBOT_ACTIONS_INCLUDED_MINUTES at that read
    observed_at      timestamptz,        -- when issuebot read it
    alerted_percent  integer NOT NULL DEFAULT 0,  -- highest threshold posted for `period`
    error            text,               -- the last failure, short and issuebot-worded
    error_at         timestamptz
);
```

The web has never written a table before (its one write so far is `NOTIFY`), and it never
migrates: the hub's worker does, as it starts. During an upgrade the two start together, so
the web can meet a database without the table. Every read and write here treats PostgreSQL's
`undefined_table` as "not yet": a read returns no row (no window drawn), a write is logged as
`actions_minutes_unavailable` and the cycle ends -- with no row there is nothing to claim, so
no alert can be sent twice. The table appears within seconds and the next cycle proceeds.
Neither path may surface as the dashboard's "database unavailable" banner.

### 4. The poller: `issuebot.web.actions`

A background task the FastAPI app starts and stops with its lifespan, built by the CLI only
when the token is set (`create_app(..., actions=None)` otherwise, which is also what tests and
the screenshot server pass). One cycle, then every `POLL_INTERVAL_S` (3,600, a constant):

1. If the account is not yet known, `fetch_login`; on failure, log and end the cycle (retried
   next hour). The login is then kept for the process's life.
2. `fetch_actions_usage`. On success, upsert the row: `period`, `used_minutes`,
   `included_minutes`, `observed_at`, and clear `error`. A `period` newer than the stored one
   resets `alerted_percent` to 0 in the same statement. On failure, record `error` and
   `error_at` and keep the previous reading (inserting a reading-less row if there is none).
3. After a success, the alert check (section 6).

Cost: one `/user` call per web start and one summary call an hour -- about 25 requests a day
for the whole host, on the billing token's budget. Logging names a failure when it starts and
when it clears, not on every failed cycle, so an expired token is one line rather than 24 a
day. The stored and displayed `error` is issuebot's own short wording from the error's kind
and HTTP status ("token rejected (401)", "no access to this account's billing (404): the
token needs the user scope and must belong to the account"); `gh`'s stderr goes to the log
only.

`ISSUEBOT_ACTIONS_INCLUDED_MINUTES` is validated by the CLI before the app is built, so a
malformed value fails start-up loudly rather than becoming a dash nobody explains.

### 5. Display

**The window.** A third window in the limits tile, after the two Claude windows, in their idiom:
percent used, the `<progress>` meter, and the label `Actions`. Its tooltip: `2,308 of 3,000
min used, 692 left, resets 1 Nov, read 12 min ago`. Rules, in `views.actions_window(row, now)`:

- **Which repositories show it**: the row whose `account` matches the repository's owner,
  compared case-insensitively (GitHub logins are). A repository owned by an organisation or
  another user spends someone else's minutes, so it draws no window.
- **Percent** is `used / included`, rounded and clamped to 0--100. Past the allowance it reads
  100% and the tooltip says `N min over the included 3,000`.
- **Rollover**: a reading whose `period` is before the current UTC month reads 0% with
  `resets` naming the month after, the rule the Claude windows already follow once their
  reset time has passed.
- **Dash states**: `included_minutes` null reads a dash whose tooltip names
  `ISSUEBOT_ACTIONS_INCLUDED_MINUTES`; a row with an error and no reading reads a dash whose
  tooltip is the error.
- **A failed refresh over a good reading** keeps the figure and appends `last refresh failed
  <age>: <error>` to the tooltip.
- **Independence**: the window is drawn whether the Claude windows show figures, N/A (an API
  key) or a dash. The template's `limits` / `limits_unavailable` branch is left as it is, and
  the Actions window follows either branch.

The window is drawn from the row alone, not from the web's own settings, so a web without the
token still shows the last reading with its age. Taking the window away for good is removing
the token and deleting the row; `docs/dashboard.md` says how.

**Fit.** At 1,400 px the six tiles leave each of three windows about 60 px, enough for `77%`
in the 20 px value font; the label is 11 px. No grid change.

**JSON.** `GET /api/v1/repos/{owner}/{name}/state` gains `actions_minutes`, `null` when no
window would be drawn, otherwise `{account, period ("2026-10"), used_minutes,
included_minutes, remaining_minutes, percent, resets_at, observed_at, error}`.

### 6. The Slack alert

Posted by the web's poller, after a successful read, when `SLACK_WEBHOOK_URL` and the allowance
are both set.

- **Thresholds**: 75%, 90% and 100% of the allowance, as constants (as `SlackSink`'s retry
  policy is). Each is posted at most once per calendar month for the account.
- **Crossing**: the target is the highest threshold the reading is at or past. If it is above
  `alerted_percent`, the poller *claims* it with
  `UPDATE ... SET alerted_percent = :target WHERE account = :account AND period = :period AND
  alerted_percent < :target`, and posts only if that updated a row. A jump past several
  thresholds in one reading posts the highest alone.
- **Delivery**: one attempt through `notifications.slack.urllib_post` (never raises, redacts
  the URL from any error). On failure the claim is released (`alerted_percent` back to its
  previous value, conditional on it still being the target), so the next hourly cycle retries.
  The claim-then-post order means two web processes against one database could not both post.
- **Wording**, formatted by a pure `notifications.messages.format_actions_alert`:
  - below 100%: `:warning: GitHub Actions: jleavers has used 2,308 of 3,000 included minutes
    this month (77%); 692 left until 1 Nov.`
  - at 100%: `:rotating_light: GitHub Actions: jleavers has used all 3,000 included minutes
    this month (3,012 used). Runs in private repositories are now billed or refused,
    depending on the account's budget, until 1 Nov.`
- **Not governed by `notifications.slack.events`**: that allow-list is the worker's, read from
  the workflow, which the web never reads. The alert is on whenever its three settings are.
- **Logged** as `actions_minutes_alert_sent` / `actions_minutes_alert_failed`; it is not an
  issue event and does not enter the events table.

### 7. Failure modes

| Situation | Window | Alert | Log |
|---|---|---|---|
| Token unset | none (or the last stored reading, ageing) | none | none |
| `/user` fails (expired, wrong scope) | the last stored reading if any, ageing; else none, since the account is unknown and no row can be written | none | once per change |
| Summary fails, no earlier reading | dash, tooltip is the error | none | once per change |
| Summary fails over a good reading | the reading, tooltip adds the failure | none | once per change |
| Allowance unset | dash naming the setting | none | at start |
| Allowance malformed | -- | -- | `issuebot web` refuses to start |
| Table not migrated yet | none | none | `actions_minutes_unavailable` |
| Webhook unset | normal | none | none |
| Post fails | normal | retried next cycle | `actions_minutes_alert_failed` |
| New month | 0% until the month's first read; alert memory resets with it | | |

### 8. Security

- **What the token can do**: the classic `user` scope reads and writes the owner's profile,
  reads their private email addresses, follows and unfollows, and reads billing. It reaches no
  repository. It is the narrowest credential GitHub offers for this endpoint.
- **Where it is**: in the web container's environment only. The web runs no session, serves
  browsers only behind HTTP Basic on a loopback-published port, and already holds the
  database DSN, which is the larger prize. The worker's environment blanks it (section 1).
- **Where it never goes**: into an argument list (`GhRunner` passes it as `GH_TOKEN` in the
  child's environment), a log line, the database, a page or the JSON. Errors stored and shown
  are issuebot's own wording.
- **The webhook**: the web now holds `SLACK_WEBHOOK_URL` too; `urllib_post` already keeps it
  out of every error.
- **Operator guidance** (`docs/security-model.md`): give the token an expiry; its lapse shows
  as the window's error and in the web's log, and minting a new one is a `.env` edit and a web
  restart.

### 9. Documentation

- `docs/dashboard.md`, "The hero's six tiles": the third window, what "used" counts and why
  (section "What GitHub provides"), its states, removing it, and the alert.
- `docs/security-model.md`: a section on the dashboard's billing token -- its reach, why the web
  and not the workers, the worker blanking it, expiry.
- `README.md` configuration reference: the two new variables, and `SLACK_WEBHOOK_URL` now also
  reaching the web. The `user` scope's reach is stated beside the token's setting, where the
  operator mints it, rather than only in the security model.
- `.env.example`: both keys, shipped empty, commented.
- `compose.yaml`: `web`'s environment and its "no GitHub ... credential" comment; the worker's
  blanking line.
- `docs/operations.md`, "Checks that never ran": one sentence pointing at the Actions window as
  the first thing to look at.
- `docs/package-layout.md`: `issuebot.github` (billing), `issuebot.web` (the poller, the window,
  `create_app`'s new parameter), `issuebot.db` (the table, its tolerance of a missing table),
  `issuebot.notifications` (`format_actions_alert`).
- `CLAUDE.md`: the `issuebot web` line in Commands names the optional billing token. Nothing
  more there; the file has a budget.
- `tools/screenshots/seed.py` inserts a row for the placeholder account, and
  `docs/images/dashboard.png` is regenerated, as `CONTRIBUTING.md` requires when the board
  changes.

### 10. Testing

- `github.billing`: the 2026-10-02 summary verbatim as a fixture (it names no repository);
  storage excluded from the sum; unit compared case-insensitively; a missing `timePeriod` and
  a non-numeric quantity raise; the fetch's `gh` arguments carry no token.
- `web.actions`, with a fake runner, store and post: the login resolved once; a reading stored;
  a failure keeping the reading; a crossing posting once; a jump posting the highest only; a
  failed post releasing the claim and the next cycle retrying; a new period resetting
  `alerted_percent`; no post without a webhook or an allowance; log lines on change only.
- `views`: percent, clamping, rollover to 0%, over-allowance wording, owner matching
  case-insensitively, absent for another owner, the dash states, the JSON shape.
- Template: three windows; N/A + N/A + Actions; two windows when there is no row.
- CLI: a malformed allowance refuses start; no token builds no poller.
- Compose: the three variables reach `web`; the worker blanks the token; `.env.example` ships
  both new keys empty (`tests/test_compose_credentials.py`).
- `agent_environment` drops `ISSUEBOT_GITHUB_BILLING_TOKEN`.
- Database (skipped without `DATABASE_URL`): the migration applies; upsert, period reset,
  claim and release semantics; reads and writes against a database without the table.

## Out of scope

- Organisation-owned repositories' billing (a different endpoint and an org admin's token).
- Spend beyond the allowance in money, and budgets.
- A burn-rate projection ("runs out on the 9th"), which would have caught a runaway loop
  earlier than a threshold; a natural follow-up once readings accumulate.
- Configurable thresholds.
- A per-repository breakdown, which needs the detailed report and would name private
  repositories on a page that may be screenshotted.
