# Phase 5: Slack notifications

Status: Draft for review (2026-09-03)

Parent: [Issuebot phased design](2026-09-02-issuebot-phased-design.md), Phase 5.
Builds on: [Phase 1: Foundations](2026-09-02-phase-1-foundations-design.md) (the event
bus and sink protocol) and [Phase 4: Orchestrator](2026-09-03-phase-4-orchestrator-design.md)
(which events are published and when). This spec owns the detail of Phase 5 only; the
architecture (§2.1, §2.2), the label state machine (§2.3), the security posture (§2.9)
and the configuration schema (§2.11) live in the parent.

## 1. Goal

A Slack channel sees every state transition and every blocked run as it happens, with
a link to the issue and to the pull request when there is one, without the
orchestrator ever waiting on Slack. Failed runs reach the channel through the blocked
escape by default and per attempt when the operator opts in to `run_ended`. Operators
can confirm a webhook works from `issuebot validate` before spending Claude budget.

In scope: the `issuebot.notifications` package (message formatting, the webhook
transport, the `SlackSink`); wiring the sink into `issuebot worker` and `issuebot
run-once`; the `notifications.slack` check in `issuebot validate` and a
`--slack-probe` flag; one bounded amendment to the Phase 4 orchestrator so a
transition that lands while a worker is being stopped still reaches the bus; a
documentation pass over the Phase 4 spec's known prose drift.

Out of scope (roadmap Phase 5): a Slack app, threads, interactive buttons. Also out of
scope, decided here: Block Kit layouts and attachments (plain `mrkdwn` text is enough
for one-line notifications), per-channel routing, and any change to the orchestrator's
reload path (§8).

Frozen inputs, used as they are: `issuebot.orchestrator` except for the one amendment
in §7, `issuebot.agent`, `issuebot.github`, `EventBus`/`EventSink`, `EVENT_KINDS`
(`NotificationSent` already exists; no kind is added), `SlackSettings` (no field is
added; every knob that is not in §2.11 is a module constant).

## 2. Layout after this phase

```
src/issuebot/
├── cli.py                          + _slack_sink, _slack_post seam, --slack-probe, _slack_check
├── orchestrator/orchestrator.py    handle_worker_exit publishes the final transition before releasing
└── notifications/
    ├── __init__.py                 re-exports
    ├── messages.py                 format_event, issue_link, pr_link (pure)
    └── slack.py                    PostResult, Poster, redact, urllib_post, subscribed_kinds,
                                    SlackSink, constants
tests/
├── test_notifications_messages.py  one case per kind, link and emoji rules
├── test_notifications_slack.py     transport against a local http.server; sink with a fake poster
├── test_cli.py                     + validate check states, --slack-probe, run-once and worker wiring
└── test_orchestrator.py            + success during shutdown publishes the agent transition
```

`issuebot.notifications` imports `config`, `events` and `log` only. Nothing in
`orchestrator` or `agent` imports it; `cli` wires it. It cannot live inside
`issuebot.events` because `issuebot.config.settings` imports `issuebot.events.types`
for `EVENT_KINDS`, and a sink that takes `SlackSettings` would close an import cycle.

No new dependency: the transport is `urllib.request` run in a worker thread through
`asyncio.to_thread`. `pyproject.toml` and `uv.lock` do not change.

## 3. Delivery model

`EventBus.publish` is synchronous and runs on whichever task publishes (the
orchestrator's state task, or a worker task inside `run_session`). The sink therefore
does no I/O in `handle`; Phase 1 §6 already reserves this shape for the Slack and
PostgreSQL sinks.

```
publish(event) ──▶ SlackSink.handle: filter, format, put_nowait ──▶ queue (≤ QUEUE_LIMIT)
                                                                        │
                                       one drain task: post, retry, publish NotificationSent
```

- **`handle`** returns at once. It ignores anything that is not an `IssueEvent`, ignores
  `notification_sent` unconditionally (before the allow-list, so a user who adds it to
  `events` still cannot make the sink notify about its own notifications), ignores kinds
  outside the allow-list, formats the text (§4) and appends a `_Pending(event, text)` to
  an unbounded `asyncio.Queue`. When `qsize() >= QUEUE_LIMIT` (100) the event is dropped,
  `dropped` is incremented and `slack_queue_full` is logged at WARNING with the kind and
  issue number. Events published before `start()` are buffered; events published after
  `close()` are dropped with a DEBUG log.
- **The drain task** (`start(bus)` creates it; the task is stored on the sink) takes one
  pending item at a time and posts it with up to `MAX_ATTEMPTS` (3) tries:

  | Response | Action |
  |---|---|
  | 2xx | `sent += 1`; log `slack_notification_sent` (kind, issue number, attempt); publish `NotificationSent(channel="slack", about_kind=event.kind)` on the bus |
  | 429 | wait `Retry-After` seconds when the header is a number, else the backoff for this attempt; cap the wait at `RETRY_AFTER_CAP_S` (30); retry |
  | 5xx, no response (timeout, connection refused, DNS) | wait the backoff (`RETRY_DELAYS_S = (1.0, 4.0)` before attempts 2 and 3); retry |
  | any other 4xx | permanent: no retry |

  After the last try, or a permanent rejection, `failed += 1` and
  `slack_notification_failed` is logged at WARNING with the kind, issue number, attempts,
  status and the redacted error. Every wait goes through an injectable `sleep`, so the
  retry rules are tested without sleeping. A failure inside the drain loop other than a
  post result (a bug) is logged `slack_deliver_crashed` and the loop continues with the
  next item; the task never dies on its own.
- **`close()`** puts a sentinel on the queue and waits up to `DRAIN_TIMEOUT_S` (10) for the
  drain task to reach it. On timeout the task is cancelled and `slack_drain_timeout` is
  logged with the number of items left. Either way `slack_sink_closed` is logged with
  `sent`, `failed` and `dropped`. The CLI calls `close()` after the orchestrator's own
  shutdown has finished (§6), so the `RunEnded` and release events of that shutdown are
  delivered too; ten seconds fits inside the compose `stop_grace_period` (120 s) with
  the orchestrator's worst case (`hooks.timeout_ms` + 20 s).

Publishing `NotificationSent` from the drain task is safe: the task runs on the event
loop, `publish` isolates every sink, and the log sink writes the line that the live
check and Phase 6 read.

## 4. Messages (`messages.py`)

Pure functions; no I/O.

```python
def issue_link(repo: str, event: IssueEvent) -> str:
    """<https://github.com/{repo}/issues/{n}|{identifier}>"""


def pr_link(url: str) -> str:
    """<url|PR #n> when the URL ends in /pull/<digits>, else <url|pull request>."""


def format_event(event: Event, *, repo: str, labels: GitHubLabels) -> str | None:
    """One line of Slack mrkdwn for the seven notifiable kinds; None for anything else."""
```

Events carry the issue number and identifier but not its URL, so the sink is
constructed with `github.repo` and derives the link. `labels` maps a target label back
to its role for the emoji; a name that matches no configured label (case-insensitive)
gets `:label:`.

| Kind | Text (`<issue>` is `issue_link`) |
|---|---|
| `state_changed` | `{emoji} <issue> `from` → `to` by {actor}[ · {pr_link}]`; a `None` label reads `no label`; emoji by the role of `to`: todo `:inbox_tray:`, in_progress `:hammer_and_wrench:`, review `:eyes:`, rework `:repeat:`, complete `:white_check_mark:`, other or none `:label:`; actor `issuebot` → `by issuebot`, `agent` → `by the agent`, `human` → `by a human` |
| `blocked` | `:no_entry: <issue> blocked: {reason}` |
| `run_started` | `:rocket: <issue> run started (attempt {attempt})` |
| `run_ended` | succeeded: `:white_check_mark: <issue> run succeeded: {turns} turn(s), {duration}, ${cost:.2f}`; otherwise `:x: <issue> run {failed \| timed out \| stalled \| cancelled}: {error or outcome} ({turns} turn(s), {duration}, ${cost:.2f})` |
| `pr_opened` | `:link: <issue> opened {pr_link}` |
| `issue_completed` | `:tada: <issue> complete[ · {pr_link} merged]` |
| `issue_cancelled` | `:wastebasket: <issue> cancelled: {reason}` |
| `notification_sent` | `None` |

Duration renders as `{m}m{ss}s`. The payload is `{"text": <line>}` and nothing else;
Slack treats webhook text as `mrkdwn`, so `<url|label>` links and backticks render. The
live check (§12) is where link unfurling is judged; if the previews are noisy the
payload gains `unfurl_links: false` in one place.

## 5. Transport and the sink (`slack.py`)

```python
QUEUE_LIMIT = 100
MAX_ATTEMPTS = 3
POST_TIMEOUT_S = 10.0
RETRY_DELAYS_S: tuple[float, ...] = (1.0, 4.0)
RETRY_AFTER_CAP_S = 30.0
DRAIN_TIMEOUT_S = 10.0
REDACTED = "<webhook url>"


@dataclass(frozen=True, slots=True)
class PostResult:
    status: int | None  # HTTP status; None when no response arrived
    retry_after_s: float | None = None
    error: str | None = None  # never contains the webhook URL

    @property
    def ok(self) -> bool: ...  # 2xx
    @property
    def retryable(self) -> bool: ...  # no response, 429, or 5xx


class Poster(Protocol):
    async def __call__(self, url: str, payload: bytes, *, timeout_s: float) -> PostResult: ...


def slack_payload(text: str) -> bytes:
    """{"text": text} as UTF-8 JSON; the only payload shape issuebot sends."""


def redact(text: str, url: str) -> str:
    """Replace the URL and its path (the part that carries the secret) with REDACTED."""


async def urllib_post(url: str, payload: bytes, *, timeout_s: float = POST_TIMEOUT_S) -> PostResult:
    """POST JSON with urllib in a worker thread. Never raises; http and https only."""


def subscribed_kinds(slack: SlackSettings) -> frozenset[str]:
    """The allow-list minus notification_sent."""


class SlackSink:
    name = "slack"

    def __init__(
        self,
        slack: SlackSettings,
        *,
        repo: str,
        labels: GitHubLabels,
        post: Poster = urllib_post,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None: ...

    kinds: frozenset[str]  # subscribed_kinds(slack)
    sent: int
    failed: int
    dropped: int

    def handle(self, event: Event) -> None: ...  # §3
    def start(self, bus: EventBus) -> None: ...  # creates the drain task; a second call raises
    async def close(self) -> None: ...  # §3; a no-op before start()
```

`urllib_post` builds a `Request` with `Content-Type: application/json; charset=utf-8`
and `method="POST"`, refuses any scheme other than `http` and `https` without touching
the network (`status=None`, `error="unsupported URL scheme ..."`), maps `HTTPError` to its
status (reading a numeric `Retry-After`), and maps `URLError`, `OSError` and `TimeoutError`
to `status=None` with the exception text passed through `redact`. `http` is accepted by
the transport because the tests post to a local `http.server`; `validate` is where an
`http` webhook is rejected (§6). The sink requires a `webhook_url`; the CLI does not
construct one otherwise.

## 6. CLI wiring

**Sink construction.** `_slack_sink(settings) -> SlackSink | None` returns a sink when
`webhook_url` is set and `subscribed_kinds` is non-empty, passing the module-level
`_slack_post` seam (default `urllib_post`) so tests substitute a fake poster the way
they substitute `_adapter_factory`.

**`run-once`** and **`worker`** build the bus as `EventBus([LogSink(), sink])` when a sink
exists, call `sink.start(bus)` on the running loop before the session or the orchestrator
starts, and `await sink.close()` in a `finally` after it has returned (in `_run_worker`,
after the signal handlers are removed, so the orchestrator's shutdown has already
published its last events). A `run-once` invocation is therefore also the cheapest way
to see a real message: it posts `todo → in-progress` at the claim.

**`validate`.** The `notifications.slack` line becomes a real check and the count stays
at twelve:

| State | Line |
|---|---|
| `webhook_url` unset, `events` empty | `[ OK ] notifications.slack: not configured (events: [])` |
| `webhook_url` unset, `events` non-empty | `[WARN] notifications.slack: not configured; export SLACK_WEBHOOK_URL to notify on <kinds>, or set notifications.slack.events: [] to silence this` |
| set, scheme not `https` or no host | `[FAIL] notifications.slack: webhook_url is not an https URL` |
| set, host not `hooks.slack.com` or path not under `/services/` | `[WARN] notifications.slack: configured (<kinds>); the URL is not a hooks.slack.com/services/ webhook (a compatible endpoint is fine)` |
| set, `events` empty | `[WARN] notifications.slack: configured but events is empty; nothing will be sent` |
| set and well formed | `[ OK ] notifications.slack: configured (<kinds>)` |

`<kinds>` is the sorted allow-list minus `notification_sent`. The URL itself never
appears in any line. Slack stays optional: nothing here blocks the worker, and the
orchestrator's startup does not probe Slack.

**`validate --slack-probe`** posts one message through `_slack_post` to a configured,
well-formed webhook: `:wave: issuebot validate: Slack notifications are configured for
<kinds> (<repo>)`. Delivered, the line reads `configured (<kinds>); test message
delivered`; otherwise `[FAIL] notifications.slack: test message not delivered: HTTP
<status>` or `...: <redacted error>`. The flag has no effect when Slack is not
configured or the URL fails the shape check. It is the only place outside the sink that
posts, and it is not interactive.

**Reload.** The orchestrator rebuilds its adapter and workspace manager when
`WORKFLOW.md` changes, but the bus and the sink are owned by the CLI. A change to
`notifications.slack` (the webhook variable or the allow-list) takes effect on the next
start; the `workflow_reloaded` log line lists `notifications` under `changed` when the
section differs, which is the operator's cue. This is documented in `CLAUDE.md` and
`README.md` rather than solved with a reload hook, which would need an orchestrator
change for a setting that changes about as often as the secret it references.

## 7. Phase 4 amendment: the transition that lands during a stop

Phase 4 §6.8 releases a worker whose `stop_cause` is `moved`, `missing`, `shutdown` or
`closed` before it reaches the row that publishes `observe_transition(entry.issue,
final_issue)`. A worker that reaches `review` while `shutdown()` is stopping it (the
agent's last action landed, `run_session` refreshed the issue and returned
`succeeded`) therefore never publishes the agent's `StateChanged` or its `PrOpened`;
the log and, from this phase, the Slack channel miss the most important transition.
The Phase 4 final review parked this as follow-up M2 "because it matters once Phase 5
or 6 sinks exist".

Amendment (`handle_worker_exit`): when the task returned a `succeeded` result whose
`final_issue` is set and open, publish `observe_transition(entry.issue, final_issue)`
immediately after the `terminal_issue` row and before every release row; the
continuation row no longer publishes it itself. Consequences: a `moved` stop publishes
nothing new (reconcile already updated `entry.issue`, so the diff is empty unless a
pull request appeared in between, which is then correctly reported); the `max_turns`
escape may now be preceded by a `PrOpened` when the agent opened a pull request but did
not set `review`; no signature changes. The Phase 4 spec's §6.8 gains an "amended by
Phase 5" paragraph. One new test: a worker that succeeds during `shutdown()` leaves
`state_changed(actor=agent)` and `pr_opened` on the bus, releases the issue and schedules
nothing.

The same docs task applies the prose fixes the Phase 4 review listed as M4, in the
Phase 4 spec only (the code is the reference): `blocked_escape` returns the
`EscapeOutcome` strings `"applied"`, `"skipped"` and `"failed"` (not `True`/`False`) and
`finish_terminal` returns `FinishOutcome` (§5, §6.7, §6.8, §11); a `path_for` failure
skips the orphan for that tick instead of dispatching a fresh session (§6.4); a fired
`escape` retry runs before, not after, the refresh (§6.7); the exit handler finishes a
`terminal_issue` before the release rows and `closed` is a release cause once a later
refresh has seen the issue open again (§6.5, §6.8, decided 2026-09-03 as Phase 4 ruling
R13). The other parked follow-ups (M3, M5 to M8) stay parked.

## 8. Configuration

No new setting. `notifications.slack.webhook_url` (`SecretStr | None`, resolved from
`$SLACK_WEBHOOK_URL`) and `notifications.slack.events` (default `[state_changed,
blocked]`, validated against `EVENT_KINDS`) are used as they are. Queue length, attempt
count, timeouts and backoff are constants in `slack.py` (§5); making them settings is a
one-line change each if a deployment ever needs it. The dogfood `WORKFLOW.md` keeps its
`events: [state_changed, blocked]`; the dot-env example's comment on
`SLACK_WEBHOOK_URL` describes the expected URL shape.

## 9. Security

- The webhook URL is the credential. The sink keeps it in a private attribute, never logs
  it, and every error string that leaves the transport has passed through `redact`,
  which replaces both the full URL and its path. Log lines carry statuses, kinds, issue
  numbers, attempt counts and redacted errors only. `validate --show-config` already masks
  it as a `SecretStr`.
- `validate` fails a webhook that is not `https`, so the secret never travels in clear.
- Label names come from configuration and URLs come from GitHub. The blocked reason and
  the run error can carry the agent's last result line or stderr (already in the log and
  the workpad comment); those free-text fields are mrkdwn-escaped (`&`, `<`, `>`) so agent
  output cannot form links or channel mentions. No issue body reaches Slack.
- The drain thread's `urlopen` has a socket timeout (`POST_TIMEOUT_S`), so a hung Slack
  endpoint costs at most ten seconds per attempt and never blocks the loop.

## 10. Testing

All hermetic; no network. Tests that spawn the fakes are `skipif win32`; the local HTTP
server and the sink tests run everywhere.

| File | Covers |
|---|---|
| `test_notifications_messages.py` | one line per kind with the exact text; `None` for `notification_sent` and for a bare `Event`; `pr_link` with and without a numeric tail; the emoji per role, case-insensitive label match, `:label:` for an unknown name and for `None`; the three actor phrasings; `run_ended` per outcome with and without an error; duration formatting |
| `test_notifications_slack.py` | `redact` strips the URL and its path and leaves other text alone; `PostResult.ok`/`retryable`; `urllib_post` against a `ThreadingHTTPServer` on `127.0.0.1:0` whose handler records the body and headers and answers from a script: 200 (JSON body with `text`, content type), 429 with `Retry-After: 2` (parsed), 429 with a non-numeric header (`None`), 500, a closed port (`status=None`, error set, no exception), an unsupported scheme (no request made); the sink with a fake poster and a recording `sleep`: filters (`notification_sent` even when listed, unsubscribed kinds, non-issue events), formats, buffers before `start`, delivers in order, publishes `NotificationSent` with the right `about_kind` and never re-enqueues it, retries 429 honouring `Retry-After` and the cap, retries 5xx with the backoff and gives up after three tries (`failed`, one warning), drops a 400 at once, drops when the queue is full (`dropped`, log) and keeps delivering, `close()` drains what is queued, `close()` times out on a hanging poster (patched `DRAIN_TIMEOUT_S`) and cancels the task, `start()` twice raises, `close()` before `start()` is a no-op, a poster that raises is logged and does not stop the loop |
| `test_cli.py` | `validate` prints each of the six `notifications.slack` lines and the count stays twelve; `--slack-probe` posts once through `_slack_post` with the expected text and reports delivered / HTTP status / redacted error, and posts nothing when Slack is not configured; `run-once` with `SLACK_WEBHOOK_URL` set builds the bus `["log", "slack"]`, the claim's `state_changed` reaches the fake poster before the command returns, and the summary still prints; `worker` with the variable set passes a bus with the Slack sink to the orchestrator factory and an event the stub publishes in `run()` is posted before `main` returns; without the variable the bus is `["log"]` as today |
| `test_orchestrator.py` | success during `shutdown()` (§7) |

The existing `test_validate_configured_database_and_slack` uses
`https://hooks.example/x`, which the new shape check warns about; it becomes the "warns
about a non-Slack host" case.

## 11. Decisions made in this phase

1. **Stdlib `urllib.request` through `asyncio.to_thread`**, no `httpx`. One small POST
   per event, one worker thread owned by the drain task, a socket timeout, and no change
   to the dependency list, the lockfile or the image. `httpx` would add six packages and
   an async client lifecycle for a call that fits in twenty lines.
2. **`issuebot.notifications`, not `issuebot.events`**, to avoid the
   `config → events → config` import cycle a settings-taking sink would create.
3. **In-process queue, one drain task, bounded retry, drop and log**: cap 100, three
   attempts, `Retry-After` honoured on 429 and capped at 30 s, backoff 1 s then 4 s, 4xx
   other than 429 permanent, `close()` drains for at most 10 s. Failures are logged and
   counted, never raised.
4. **The CLI owns the sink's lifetime** in both `worker` and `run-once`: start before,
   close after, so shutdown events are delivered and `run-once` doubles as a manual Slack
   test.
5. **The allow-list is by kind and regular.** `run_ended` covers every outcome and
   stays opt-in; failed attempts reach the channel through `blocked` by default (the
   orchestrator retries with backoff and escalates, and the escalation is what needs a
   human). The default `[state_changed, blocked]` from §2.11 is unchanged.
6. **`NotificationSent` is published after each successful post** with `channel="slack"`
   and `about_kind` set to the notified kind, and the sink ignores that kind before the
   allow-list, so it cannot notify about itself.
7. **Text-only payload**, `mrkdwn` links and backticks, an emoji per target role. No
   Block Kit, no attachments; unfurling is judged at the live check.
8. **The webhook URL is redacted from every error string** at the transport boundary,
   and `validate` requires `https`.
9. **`validate` warns, never fails, when the webhook is unset** and the allow-list is
   non-empty; `events: []` silences it. A well-formed `hooks.slack.com/services/` URL is
   `ok`; another `https` host warns; anything else fails.
10. **`validate --slack-probe` is in scope**: one test message through the same
    transport, reported on the same check line. It is not interactive and it is what the
    live check runs before spending Claude budget.
11. **Webhook and allow-list changes need a restart**, documented; no reload hook.
12. **Phase 5 takes the Phase 4 follow-up M2** (the transition dropped when a worker
    succeeds during a stop) as a bounded amendment to `handle_worker_exit` with a spec
    note, and the M4 prose pass on the Phase 4 spec as part of the docs task. M3 and M5
    to M8 stay parked.
13. **No new settings**; the operational knobs are constants.

## 12. Done when

- `uv run pytest -q` passes with no network; ruff and pre-commit clean; CI green;
  `docker compose build` succeeds; `pyproject.toml` and `uv.lock` unchanged.
- `uv run issuebot validate` on the committed `WORKFLOW.md` prints twelve checks, with
  the `notifications.slack` line warning when `SLACK_WEBHOOK_URL` is unset.
- Live check from the developer host against `jleavers/issuebot-scratch` (issue #3 in
  `review` with PR #4 open, `~/issuebot-scratch/WORKFLOW.md` recreated from the repo
  file with the repo, the workspace root, an explicit `claude.stall_timeout_ms` and
  `run_ended` added to the allow-list), with `GH_TOKEN` and a test channel's
  `SLACK_WEBHOOK_URL` exported in the same command and neither ever printed:
  1. `validate --slack-probe` reports twelve checks, none failed, and the operator sees
     the test message in the channel.
  2. A new `todo` issue (a `divide` function) is claimed and runs to a pull request and
     `review`; the channel shows `todo → in-progress` by issuebot, `in-progress → review`
     by the agent with the pull request link, and the run's `run_ended` line with its
     cost; the log shows one `notification_sent` per message.
  3. The operator merges PR #4; the next terminal sweep sets `complete` on issue #3 and
     the channel shows `review → complete` by issuebot.
  4. `SIGTERM` while idle stops the worker within a second; the log ends with
     `slack_sink_closed` reporting the sent count and zero failed or dropped.
- `CLAUDE.md` describes `issuebot.notifications`, the wiring, the `validate` check and
  the restart rule; `README.md` mentions `SLACK_WEBHOOK_URL` and `validate --slack-probe`;
  the roadmap's Phase 5 section records what was decided and deferred; the Phase 4 spec
  carries the §7 amendment note and the prose fixes; the dot-env example describes the
  webhook.
