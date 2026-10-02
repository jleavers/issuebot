# Claude Usage Limits Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Post to Slack when the Claude subscription's 7-day usage window passes 75% and 90%, and when either window is spent and a worker stops claiming issues, once per window instance however many workers see it.

**Architecture:** The worker records, on its `usage` dispatch hold, the reset claude reported (`until`) and the window it refused on (`window`); both reach the snapshot JSON. The hub's `web` gains a second background task beside the Actions minutes poller, `ClaudeLimitsWatcher`, which reads every `runtime_snapshot` row once a minute, works out which alerts are due, and claims each in a new `claude_limit_alerts` table, keyed by `(limit_window, resets_at)`, before posting it through the existing Slack webhook.

**Tech Stack:** Python 3.14, FastAPI, psycopg 3, PostgreSQL 18, pytest with pytest-asyncio (`asyncio_mode = "auto"`), docker compose.

**Spec:** `docs/superpowers/specs/2026-10-02-claude-limits-alert-design.md`

## Global Constraints

- The only setting is the existing `SLACK_WEBHOOK_URL`, read by `issuebot web`. No new environment variable, no compose change.
- Constants, exactly: `INTERVAL_S = 60.0`, `RETRY_BACKOFF_S = 900.0`, `SEVEN_DAY_THRESHOLDS = (75, 90)`, `HIT_PERCENT = 100`, `SEVEN_DAY = "seven_day"`, `UNKNOWN_WINDOW = "unknown"`.
- The table, exactly: `claude_limit_alerts (limit_window text NOT NULL, resets_at timestamptz NOT NULL, alerted_percent integer NOT NULL DEFAULT 0, PRIMARY KEY (limit_window, resets_at))`, migration `0006_claude_limit_alerts.sql`.
- Wording, exactly: `:warning: Claude: the 7-day usage window is 77% used; it resets Fri 9 Oct, 05:00 UTC.` and `:rotating_light: Claude: the 5-hour usage limit is reached; issuebot stops claiming issues until 20:00 UTC (in 2 h 13 min).` Times are UTC: same UTC date as now is `HH:MM UTC`, otherwise `Ddd D Mon, HH:MM UTC`. Use `.day` with `%a`/`%b`, never the glibc-only `%-d`.
- The hold's `until` is only a reset claude reported, never the orchestrator's one-interval fallback.
- The watcher holds the webhook and nothing else, calls no API, and never logs the webhook URL (`urllib_post` already redacts it from every error).
- `jleavers/issuebot` is public: never name a private repository anywhere in the tree, a commit message or a fixture.
- A global hook rejects any Bash command whose text contains the dot-env filename, including `.env.example` and `git add` of it. Edit such files with the Edit tool; stage with `git add -u` after `git status --short`.
- `docs/package-layout.md` has about 16 KB of headroom under its 160 KiB budget (`tests/test_instruction_bounds.py`). Keep this plan's additions there under 2 KB. Add nothing to `CLAUDE.md`.
- Never push to `main`, never run `git reset --hard`, `git clean -fd` or `rm -rf`. Work on branch `dashboard/claude-limits-alert`.
- The live hub (`db`, `web`, `worker`, `egress`) runs from this checkout's compose project. Never run `docker compose down`, and never `up`, `restart` or `rm` any service but `test-db`. Never pass `ISSUEBOT_DB_PORT` inline.
- Every commit message ends with the committing model's own `Co-Authored-By:` line, as its harness gives it, then `Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa`.
- Run the suite with `uv run pytest`. The DB tests skip without `DATABASE_URL`. Task 2 and Task 6 run them against a throwaway `test-db`: `docker compose --profile test up -d --wait test-db`, then `DATABASE_URL=postgresql://issuebot@$(docker compose port test-db 5432)/issuebot uv run pytest ...`, then `docker compose rm -sf test-db`.

## Review Focus

1. **A refusal claude did not date.** This is a text-only "usage limit" result with no `rate_limit_event` reset. The orchestrator then waits one poll interval, a fallback that moves each time it is taken. The hold must carry no `until`, so the web posts nothing rather than a fresh hit every interval. Pinned in Task 1 (`test_a_refusal_claude_did_not_date_names_no_until`) and Task 3 (`test_an_expired_or_undated_hold_posts_nothing`).
2. **Several workers on one subscription.** Holds with one `until` and window, and readings with one reset at different utilisations, must post once, at the highest utilisation. Pinned in Task 3 (`test_every_worker_on_one_wall_posts_one_hit`, `test_the_highest_reading_of_one_window_is_the_one_posted`).
3. **A stopped worker's stale snapshot.** A hold whose `until` has passed, or a reading whose window has reset, posts nothing. A still-future hold from a stopped worker still does. Pinned in Task 3 (`test_an_expired_reading_posts_nothing`, `test_an_expired_or_undated_hold_posts_nothing`).
4. **A web restart in the middle of a window.** Nothing repeats. Pinned in Task 2 (claim semantics) and Task 3 (`test_a_restart_posts_nothing_new`).
5. **A dead webhook.** The claim is released each time, the next attempt waits `RETRY_BACKOFF_S`, the webhook URL never reaches a log line, and a recovered webhook posts once. Pinned in Task 3 (`test_a_dead_webhook_is_retried_only_after_the_backoff`).

---

### Task 1: The usage hold names its window and when it lifts

**Files:**
- Modify: `src/issuebot/agent/runner.py` (`TurnResult.usage_window`; `run_turn` sets it)
- Create: `tests/fixtures/claude/refused.jsonl`
- Modify: `tests/fakes/claude` (a `refused` scenario)
- Modify: `src/issuebot/agent/session.py` (`RunResult.usage_window`; `_State.usage_window`)
- Modify: `src/issuebot/orchestrator/admission.py` (`Hold.until`, `Hold.window`)
- Modify: `src/issuebot/orchestrator/state.py` (`DispatchHold.until`, `DispatchHold.window`)
- Modify: `src/issuebot/orchestrator/orchestrator.py` (`_usage_until`, `_usage_window`, `_hold_usage`, `_release_usage_hold`, `_current_hold`, `_hold_snapshot`, `_settle_dispatch_hold`, `_usage_limited`)
- Modify: `src/issuebot/web/views.py` (`dispatch_hold` passes `until` and `window` through)
- Modify: `tests/test_agent_runner.py`, `tests/test_agent_session.py`, `tests/test_orchestrator.py`, `tests/test_web_app.py`, `tests/test_web_pages.py`

**Interfaces:**
- Consumes: `UsageLimit(window: str, resets_at: datetime)` and `StreamParser.usage_limit` (`agent/runner.py`, existing).
- Produces:
  - `TurnResult.usage_window: str | None = None`, `RunResult.usage_window: str | None = None`
  - `Hold(kind, reason, key=None, until=None, window=None)` (`orchestrator/admission.py`)
  - `DispatchHold(*, kind, reason, since, until: datetime | None = None, window: str | None = None)`, serialised by `RuntimeSnapshot.to_dict()` as ISO strings under `dispatch_hold.until` / `dispatch_hold.window`
  - `views.dispatch_hold(row)` returns `{"kind", "reason", "since", "until", "window"}`, with `until` and `window` as `str | None`

- [ ] **Step 1: A refused turn for the fake claude**

Create `tests/fixtures/claude/refused.jsonl` with three lines. Make the first line a copy of the first line of `tests/fixtures/claude/success.jsonl` (the `system`/`init` line): `head -n 1 tests/fixtures/claude/success.jsonl > tests/fixtures/claude/refused.jsonl`. Then append these two lines exactly, each on one line. They are the real refusal from run `20260922T121334Z-881640`, already used as `REJECTED_LINE`/`REFUSED_RESULT` in `tests/test_agent_runner.py`:

```
{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1790080200,"rateLimitType":"five_hour","unifiedWindows":{"five_hour":{"utilization":1,"resetsAt":1790080200},"seven_day":{"utilization":0.63,"resetsAt":1790312400}}},"session_id":"00000000-0000-4000-8000-000000000000"}
{"type":"result","subtype":"success","is_error":true,"duration_ms":310,"num_turns":1,"session_id":"00000000-0000-4000-8000-000000000000","total_cost_usd":0,"result":"You've hit your session limit · resets 12:30pm (UTC)","terminal_reason":"api_error","usage":{"input_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0}}
```

In `tests/fakes/claude`, add a scenario beside the others, before the final `sys.stderr.write(f"unknown CLAUDE_FAKE_SCENARIO ...")`:

```python
if scenario == "refused":
    # Claude declining the turn because the account's window is spent (#173's incident).
    replay("refused", delay)
    sys.exit(1)
```

- [ ] **Step 2: Write the failing runner test**

In `tests/test_agent_runner.py`, after `test_the_parser_keeps_a_refusal_beside_the_windows`, add:

```python
async def test_run_turn_carries_a_refusal_s_window_and_reset(workspace: Path) -> None:
    """The hold, and the hub's Slack alert after it, are keyed on both (spec 2026-10-02,
    claude-limits-alert): four workers on one subscription report one reset."""
    result = await run(runner_for(workspace, scenario="refused"), workspace)
    assert result.error_category == "usage_limited"
    assert (result.usage_reset_at, result.usage_window) == (USAGE_RESET, "five_hour")
```

Run: `uv run pytest tests/test_agent_runner.py -k refusal_s_window -q`
Expected: FAIL with `AttributeError: 'TurnResult' object has no attribute 'usage_window'`.

- [ ] **Step 3: Carry the window on the turn result**

In `src/issuebot/agent/runner.py`, in `TurnResult`, after `usage_reset_at`:

```
    # Which window refused it, as claude names it (``five_hour``, ``seven_day``, ...): what the
    # hold reports beside the reset. None for every turn that was not refused.
    usage_window: str | None = None
```

In `run_turn`, where the `TurnResult(...)` is built with `usage_reset_at=(...)`, add after it:

```
                usage_window=(
                    parser.usage_limit.window if parser.usage_limit is not None else None
                ),
```

Run: `uv run pytest tests/test_agent_runner.py -k refusal_s_window -q`
Expected: PASS.

- [ ] **Step 4: Write the failing session test**

In `tests/test_agent_session.py`, add `USAGE_RESET = datetime(2026, 9, 22, 12, 30, tzinfo=UTC)` beside the module's other constants. In `ScriptedRunner.run_turn`, add these two arguments to the `TurnResult(...)` it returns:

```
            usage_reset_at=USAGE_RESET if category == "usage_limited" else None,
            usage_window="five_hour" if category == "usage_limited" else None,
```

Then add after `test_failed_turn_fails_the_run_and_still_runs_after_run`:

```python
async def test_a_usage_limited_turn_carries_its_window_and_reset_to_the_result(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    result = await h.run(ScriptedRunner("usage_limited"))
    assert result.error_category == "usage_limited"
    assert (result.usage_reset_at, result.usage_window) == (USAGE_RESET, "five_hour")
```

Run: `uv run pytest tests/test_agent_session.py -k usage_limited -q`
Expected: FAIL with `AttributeError: 'RunResult' object has no attribute 'usage_window'`.

- [ ] **Step 5: Carry the window through the session**

In `src/issuebot/agent/session.py`:
- In `RunResult`, after `usage_reset_at`, add:
  ```python
      # Which window refused it, beside ``usage_reset_at``; None whenever that is.
      usage_window: str | None = None
  ```
- In `_State`, after `usage_reset_at: datetime | None = None`, add `usage_window: str | None = None`.
- In `_State.result()`, after `usage_reset_at=self.usage_reset_at,`, add `usage_window=self.usage_window,`.
- In the failed-turn branch, after `state.usage_reset_at = turn.usage_reset_at`, add `state.usage_window = turn.usage_window`.

Run: `uv run pytest tests/test_agent_session.py -q`
Expected: PASS.

- [ ] **Step 6: Write the failing orchestrator tests**

In `tests/test_orchestrator.py`, in the `# --- a spent usage window` section, add `"usage_window": "five_hour",` to the `USAGE_LIMITED` dict after `"usage_reset_at": USAGE_RESET,`. Then append to that section, after `test_the_usage_hold_keeps_its_since_across_a_second_refusal`:

```python
async def test_the_usage_hold_names_its_window_and_when_it_lifts(tmp_path: Path) -> None:
    """What the hub's web keys its Slack alert on (spec 2026-10-02, claude-limits-alert)."""
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **USAGE_LIMITED)
    await h.tick()
    hold = h.orchestrator.snapshot().dispatch_hold
    assert hold is not None
    assert (hold.kind, hold.until, hold.window) == ("usage", USAGE_RESET, "five_hour")
    data = json.loads(json.dumps(h.orchestrator.snapshot().to_dict()))
    assert (data["dispatch_hold"]["until"], data["dispatch_hold"]["window"]) == (
        USAGE_RESET.isoformat(),
        "five_hour",
    )


async def test_a_later_refusal_moves_the_hold_s_until_out_and_keeps_its_since(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, max_concurrent=2)
    h.add_issue(1, "todo")
    h.add_issue(2, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **USAGE_LIMITED)
    await h.tick()
    first = h.orchestrator.snapshot().dispatch_hold
    assert first is not None
    h.clock.advance(5)
    later = USAGE_RESET + timedelta(days=3)
    await h.exit(
        h.run_for(2), **{**USAGE_LIMITED, "usage_reset_at": later, "usage_window": "seven_day"}
    )
    await h.tick()
    second = h.orchestrator.snapshot().dispatch_hold
    assert second is not None
    assert second.since == first.since
    assert (second.until, second.window) == (later, "seven_day")


async def test_a_refusal_claude_did_not_date_names_no_until(tmp_path: Path) -> None:
    """Review Focus 1: the one-interval fallback moves every time it is taken, so carrying it
    would hand the web a fresh key -- and Slack a fresh alert -- every poll interval."""
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **{**USAGE_LIMITED, "usage_reset_at": None, "usage_window": None})
    await h.tick()
    hold = h.orchestrator.snapshot().dispatch_hold
    assert hold is not None and hold.kind == "usage"
    assert (hold.until, hold.window) == (None, None)
```

Also update the two exact-shape assertions on the serialised hold, `test_the_snapshot_hold_survives_the_round_trip_through_json` (~line 2672) and the github-hold `to_dict` assertion (~line 3845). Add `"until": None, "window": None,` after `"since": h.now().isoformat(),` in each expected dict.

Run: `uv run pytest tests/test_orchestrator.py -k "usage or round_trip or to_dict" -q`
Expected: the three new tests and the two updated dicts FAIL. The errors are a `TypeError` on the unknown `usage_window` keyword, an `AttributeError` on `until`, or a dict mismatch.

- [ ] **Step 7: Give `Hold` and `DispatchHold` the two fields**

In `src/issuebot/orchestrator/admission.py`, extend `Hold` (it is positional, so append with defaults) and its docstring:

```python
@dataclass(frozen=True, slots=True)
class Hold:
    """Why this worker will not claim anything at all, before it reaches the snapshot.

    ``kind`` outranks by the order of ``DispatchHoldKind``; ``key`` is what makes two holds
    the same one when the wording is not, so a hold that lasts keeps its ``since``. ``until``
    and ``window`` are a usage hold's: the reset claude reported and the window it refused on.
    """

    kind: DispatchHoldKind
    reason: str
    key: str | None = None
    until: datetime | None = None
    window: str | None = None
```

In `src/issuebot/orchestrator/state.py`, in `DispatchHold`, after `since: datetime`, add:

```
    # A usage hold's: the reset claude reported and the window it refused on (spec 2026-10-02,
    # claude-limits-alert) -- what the hub's web keys its Slack alert on. None on every other
    # hold, and on a usage hold whose refusal claude did not date.
    until: datetime | None = None
    window: str | None = None
```

- [ ] **Step 8: Record them in the orchestrator**

In `src/issuebot/orchestrator/orchestrator.py`:

In `__init__`, after `self._usage_reset_at: datetime | None = None`, add:

```
        # What the snapshot says of it: the reset claude reported -- never the one-interval
        # fallback, which moves every time it is taken -- and the window that came with it.
        self._usage_until: datetime | None = None
        self._usage_window: str | None = None
```

Replace `_hold_usage`'s signature and body with the following. Keep the existing docstring, and append the one sentence shown:

```
    def _hold_usage(self, error: str, reset_at: datetime | None, window: str | None = None) -> None:
        """...existing docstring...

        ``until`` and ``window`` in the snapshot take claude's own reset only, and move out with
        it, so the two always describe the same refusal.
        """
        fallback = self._now() + timedelta(milliseconds=self._workflow.config.polling.interval_ms)
        due = reset_at or fallback
        if self._usage_reset_at is None or due > self._usage_reset_at:
            self._usage_reset_at = due
        if reset_at is not None and (self._usage_until is None or reset_at > self._usage_until):
            self._usage_until = reset_at
            self._usage_window = window
        self._usage_reason = f"claude usage limit reached: {error}"
```

In `_release_usage_hold`, add `self._usage_until = None` and `self._usage_window = None`.

In `_current_hold`, replace `return Hold("usage", self._usage_reason, key=USAGE_HOLD_KEY)` with:

```
            return Hold(
                "usage",
                self._usage_reason,
                key=USAGE_HOLD_KEY,
                until=self._usage_until,
                window=self._usage_window,
            )
```

Replace `_hold_snapshot` with:

```
    def _hold_snapshot(
        self,
        kind: DispatchHoldKind,
        reason: str,
        *,
        key: str | None = None,
        until: datetime | None = None,
        window: str | None = None,
    ) -> None:
        """Carry why dispatch is held into the snapshot; a hold that lasts keeps its ``since``.

        ``key`` is what makes two holds the same one when the wording is not: an unreadable
        ``claude`` can garble its output differently on every probe, and the operator should
        still see how long the hold has really lasted. ``until`` and ``window`` follow the
        newest reason, as the reason itself does.
        """
        identity = (kind, reason if key is None else key)
        if self._dispatch_hold is not None and identity == self._hold_identity:
            self._dispatch_hold = replace(
                self._dispatch_hold, reason=reason, until=until, window=window
            )
            return
        self._hold_identity = identity
        self._dispatch_hold = DispatchHold(
            kind=kind, reason=reason, since=self._now(), until=until, window=window
        )
```

In `_settle_dispatch_hold`, replace `self._hold_snapshot(hold.kind, hold.reason, key=hold.key)` with `self._hold_snapshot(hold.kind, hold.reason, key=hold.key, until=hold.until, window=hold.window)`. Wrap it across lines if ruff asks.

In `_usage_limited`, replace `self._hold_usage(error, result.usage_reset_at)` with `self._hold_usage(error, result.usage_reset_at, result.usage_window)`.

Run: `uv run pytest tests/test_orchestrator.py tests/test_orchestrator_state.py -q`
Expected: PASS.

- [ ] **Step 9: Write the failing views tests**

In `tests/test_web_app.py`, in `test_dispatch_hold_ignores_a_snapshot_that_names_no_reason`, add `"until": None, "window": None` to both expected dicts (lines ~775 and ~778). In `test_state_names_the_reason_dispatch_is_held`, add `"until": None, "window": None,` to the expected `worker["dispatch_hold"]` dict. In `tests/test_web_pages.py`, in the assertion on `live["worker"]["dispatch_hold"]` (~line 1075), add `"until": None, "window": None,`. Then add to `tests/test_web_app.py`, after `test_dispatch_hold_ignores_a_snapshot_that_names_no_reason`:

```python
def test_dispatch_hold_passes_a_usage_hold_s_until_and_window_through() -> None:
    row = snapshot()
    row.data["dispatch_hold"] = {
        "kind": "usage",
        "reason": "claude usage limit reached: You've hit your session limit",
        "since": "2026-09-04T11:59:00+00:00",
        "until": "2026-09-04T14:13:00+00:00",
        "window": "five_hour",
    }
    assert dispatch_hold(row) == {
        "kind": "usage",
        "reason": "claude usage limit reached: You've hit your session limit",
        "since": "2026-09-04T11:59:00+00:00",
        "until": "2026-09-04T14:13:00+00:00",
        "window": "five_hour",
    }
    row.data["dispatch_hold"]["until"] = 7
    row.data["dispatch_hold"]["window"] = ["five_hour"]
    held = dispatch_hold(row)
    assert held is not None and (held["until"], held["window"]) == (None, None)
```

Run: `uv run pytest tests/test_web_app.py tests/test_web_pages.py -k dispatch_hold -q`
Expected: FAIL (dict mismatch: no `until` or `window` key).

- [ ] **Step 10: Pass them through in `views.dispatch_hold`**

In `src/issuebot/web/views.py`, replace `dispatch_hold`'s `return {...}` with:

```
    kind = hold.get("kind")
    until = hold.get("until")
    window = hold.get("window")
    return {
        "kind": kind if isinstance(kind, str) and kind else "unknown",
        "reason": reason,
        "since": hold.get("since"),
        # A usage hold's reset and window (spec 2026-10-02, claude-limits-alert); None on every
        # other hold, and on a snapshot written before a worker recorded them.
        "until": until if isinstance(until, str) else None,
        "window": window if isinstance(window, str) else None,
    }
```

(The existing `kind = hold.get("kind")` line stays where it is if it already precedes the return. Do not duplicate it.)

- [ ] **Step 11: Run the suite and lint**

Run: `uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests`
Expected: PASS, ruff clean. If another test asserts the exact shape of a `dispatch_hold` dict and now fails, add `"until": None, "window": None` to its expectation. Do not change the code to satisfy it.

- [ ] **Step 12: Commit**

```bash
git add src/issuebot/agent/runner.py src/issuebot/agent/session.py src/issuebot/orchestrator src/issuebot/web/views.py tests/fixtures/claude/refused.jsonl tests/fakes/claude tests/test_agent_runner.py tests/test_agent_session.py tests/test_orchestrator.py tests/test_web_app.py tests/test_web_pages.py
git commit -F - <<'EOF'
orchestrator: the usage hold names the window claude refused on and when it reopens

<the committing model's Co-Authored-By line>
Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
EOF
```

---

### Task 2: The `claude_limit_alerts` table, its read, claim and release

**Files:**
- Create: `src/issuebot/db/migrations/0006_claude_limit_alerts.sql`
- Create: `src/issuebot/db/claude_limits.py`
- Modify: `src/issuebot/db/database.py` (three methods)
- Modify: `tests/test_db_migrate.py`, `tests/test_db_database.py`
- Create: `tests/test_db_claude_limits.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `async Database.claude_limit_alerted(*, limit_window: str, resets_at: datetime) -> int`: 0 when there is no row
  - `async Database.claim_claude_limit_alert(*, limit_window: str, resets_at: datetime, target: int) -> bool`
  - `async Database.release_claude_limit_alert(*, limit_window: str, resets_at: datetime, target: int, previous: int) -> None`

- [ ] **Step 1: Update the migration tests to expect a sixth migration**

In `tests/test_db_migrate.py`:
- Add `"claude_limit_alerts",` to `TABLES`.
- Rename `test_the_package_ships_the_five_migrations` to `test_the_package_ships_the_six_migrations`. Append `"0006_claude_limit_alerts",` to its label list, change the versions to `[1, 2, 3, 4, 5, 6]`, and add `assert "CREATE TABLE claude_limit_alerts" in migrations[5].sql`.
- In `test_migrate_applies_every_migration_once`: append `"0006_claude_limit_alerts",` to the applied tuple, and change each `5` there to `6`. That is the tuple's version, `((), 6)` and `schema_version(conn) == 6`.
- The regex `knows \(5\)` (~line 132) becomes `knows \(6\)`.
- ~Line 212: `assert result.version == 6 and result.applied == ("0004_run_turns_repo", "0005_actions_minutes", "0006_claude_limit_alerts")`. Wrap the tuple over lines for ruff.
- ~Line 272: `result.version == 6`, and append `"0006_claude_limit_alerts",` to that applied tuple.

In `tests/test_db_database.py` (`test_probe_before_and_after_migrate`): `(0, 6, True)` for `before`, append `"0006_claude_limit_alerts",` to `result.applied`, and `(6, False, False)` for `after`.

- [ ] **Step 2: Write the failing DB tests**

Create `tests/test_db_claude_limits.py`:

```python
"""The claude_limit_alerts table against a real PostgreSQL (skipped without DATABASE_URL)."""

from datetime import UTC, datetime, timedelta

from issuebot.db import Database

RESETS = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)
WEEK = "seven_day"


async def _database(db_url: str) -> Database:
    database = Database(db_url)
    await database.migrate()
    return database


async def test_a_window_instance_nobody_has_alerted_reads_zero(db_url: str) -> None:
    database = await _database(db_url)
    assert await database.claude_limit_alerted(limit_window=WEEK, resets_at=RESETS) == 0


async def test_a_claim_inserts_then_raises_and_refuses_an_equal_or_lower_target(
    db_url: str,
) -> None:
    database = await _database(db_url)
    claim = database.claim_claude_limit_alert
    assert await claim(limit_window=WEEK, resets_at=RESETS, target=75) is True
    assert await claim(limit_window=WEEK, resets_at=RESETS, target=75) is False
    assert await claim(limit_window=WEEK, resets_at=RESETS, target=90) is True
    assert await claim(limit_window=WEEK, resets_at=RESETS, target=75) is False
    assert await database.claude_limit_alerted(limit_window=WEEK, resets_at=RESETS) == 90


async def test_a_window_instance_is_its_window_and_its_reset(db_url: str) -> None:
    """Review Focus 4's memory: next week, and the other window, start from nothing."""
    database = await _database(db_url)
    await database.claim_claude_limit_alert(limit_window=WEEK, resets_at=RESETS, target=90)
    next_week = RESETS + timedelta(days=7)
    assert await database.claude_limit_alerted(limit_window=WEEK, resets_at=next_week) == 0
    assert await database.claude_limit_alerted(limit_window="five_hour", resets_at=RESETS) == 0
    assert await database.claim_claude_limit_alert(
        limit_window="five_hour", resets_at=RESETS, target=100
    )


async def test_a_release_restores_only_its_own_claim(db_url: str) -> None:
    database = await _database(db_url)
    await database.claim_claude_limit_alert(limit_window=WEEK, resets_at=RESETS, target=75)
    await database.claim_claude_limit_alert(limit_window=WEEK, resets_at=RESETS, target=90)
    # A release for a target no longer held changes nothing...
    await database.release_claude_limit_alert(
        limit_window=WEEK, resets_at=RESETS, target=75, previous=0
    )
    assert await database.claude_limit_alerted(limit_window=WEEK, resets_at=RESETS) == 90
    # ...and its own puts back what was there before it.
    await database.release_claude_limit_alert(
        limit_window=WEEK, resets_at=RESETS, target=90, previous=75
    )
    assert await database.claude_limit_alerted(limit_window=WEEK, resets_at=RESETS) == 75
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_db_migrate.py -k six_migrations -q`
Expected: FAIL (the sixth migration does not exist). The DB tests skip without `DATABASE_URL`; they are run in Step 6.

- [ ] **Step 4: Write the migration and the SQL**

Create `src/issuebot/db/migrations/0006_claude_limit_alerts.sql`:

```sql
-- The Claude usage alert's memory (spec 2026-10-02, claude-limits-alert): one row per window
-- instance -- claude's window name and the moment it reopens -- written by the hub's web when it
-- posts to Slack. One subscription reports one reset to every worker, so the workers that share
-- it share a row, and a restart, which every upgrade is, never posts the same alert twice.
-- (`window` is a reserved word, hence `limit_window`.)

CREATE TABLE claude_limit_alerts (
    limit_window    text NOT NULL,              -- five_hour, seven_day, or another claude names
    resets_at       timestamptz NOT NULL,       -- when that window reopens
    alerted_percent integer NOT NULL DEFAULT 0, -- highest posted: 75 or 90, or 100 for a hit
    PRIMARY KEY (limit_window, resets_at)
);
```

Create `src/issuebot/db/claude_limits.py`:

```python
"""The claude_limit_alerts table: the Claude usage alert's memory (spec 2026-10-02,
claude-limits-alert). The web's watcher is its one reader and writer.

A claim is an upsert that writes only when it raises the stored percent, so the process whose
claim wrote is the one that posts; a release puts the read value back only while the claim is
still the one it took.
"""

CLAUDE_ALERTED = """
SELECT alerted_percent FROM claude_limit_alerts
WHERE limit_window = %(limit_window)s AND resets_at = %(resets_at)s
"""

CLAIM_CLAUDE_ALERT = """
INSERT INTO claude_limit_alerts AS c (limit_window, resets_at, alerted_percent)
VALUES (%(limit_window)s, %(resets_at)s, %(target)s)
ON CONFLICT (limit_window, resets_at) DO UPDATE SET alerted_percent = EXCLUDED.alerted_percent
WHERE c.alerted_percent < EXCLUDED.alerted_percent
"""

RELEASE_CLAUDE_ALERT = """
UPDATE claude_limit_alerts SET alerted_percent = %(previous)s
WHERE limit_window = %(limit_window)s AND resets_at = %(resets_at)s
    AND alerted_percent = %(target)s
"""
```

- [ ] **Step 5: Add the three methods to `Database`**

In `src/issuebot/db/database.py`, add `from issuebot.db.claude_limits import CLAIM_CLAUDE_ALERT, CLAUDE_ALERTED, RELEASE_CLAUDE_ALERT` beside the `issuebot.db.actions` import. After `release_actions_alert`, add:

```
    async def claude_limit_alerted(self, *, limit_window: str, resets_at: datetime) -> int:
        """The highest Claude usage alert already posted for this window instance; 0 for none."""
        params = {"limit_window": limit_window, "resets_at": resets_at}
        async with self._open() as conn:
            row = await (await conn.execute(CLAUDE_ALERTED, params)).fetchone()
        return int(row[0]) if row is not None else 0

    async def claim_claude_limit_alert(
        self, *, limit_window: str, resets_at: datetime, target: int
    ) -> bool:
        """Take ``target`` for this window instance before posting it; False if it was taken."""
        params = {"limit_window": limit_window, "resets_at": resets_at, "target": target}
        async with self._open() as conn:
            cursor = await conn.execute(CLAIM_CLAUDE_ALERT, params)
            return cursor.rowcount == 1

    async def release_claude_limit_alert(
        self, *, limit_window: str, resets_at: datetime, target: int, previous: int
    ) -> None:
        """Give a claim back after a failed post, so a later cycle tries again."""
        params = {
            "limit_window": limit_window,
            "resets_at": resets_at,
            "target": target,
            "previous": previous,
        }
        async with self._open() as conn:
            await conn.execute(RELEASE_CLAUDE_ALERT, params)
```

- [ ] **Step 6: Run the hermetic suite, then the DB tests against a throwaway server**

Run: `uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests`
Expected: PASS (DB tests skipped), ruff clean.

Then:
```bash
docker compose --profile test up -d --wait test-db
DATABASE_URL=postgresql://issuebot@$(docker compose port test-db 5432)/issuebot uv run pytest tests/test_db_claude_limits.py tests/test_db_migrate.py tests/test_db_database.py -q
docker compose rm -sf test-db
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/issuebot/db tests/test_db_claude_limits.py tests/test_db_migrate.py tests/test_db_database.py
git commit -F - <<'EOF'
db: a claude_limit_alerts table, one row per usage window instance, claimed before a post

<the committing model's Co-Authored-By line>
Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
EOF
```

---

### Task 3: The alert's wording, and the watcher

**Files:**
- Modify: `src/issuebot/notifications/messages.py` (`format_claude_limit_alert`)
- Modify: `src/issuebot/web/views.py` (rename `_moment` to `parse_moment`)
- Create: `src/issuebot/web/claude_limits.py`
- Modify: `tests/test_notifications_messages.py`
- Create: `tests/test_web_claude_limits.py`

**Interfaces:**
- Consumes: `DispatchHold(..., until=, window=)` and its `to_dict()` (Task 1); the three `Database` methods (Task 2), through the `ClaudeLimitsStore` protocol below; `Database.queries()` yielding an object with `async snapshots() -> dict[str, SnapshotRow]` (existing); `notifications.slack.urllib_post`, `slack_payload`, `Poster`, `PostResult`, `POST_TIMEOUT_S` (existing); `web.actions.WEBHOOK_ENV` (existing).
- Produces:
  - `issuebot.notifications.messages.format_claude_limit_alert(*, window: str | None, percent: int, resets_at: datetime, now: datetime) -> str`
  - `issuebot.web.views.parse_moment(value: object) -> datetime | None`
  - `issuebot.web.claude_limits`: `INTERVAL_S`, `RETRY_BACKOFF_S`, `SEVEN_DAY`, `SEVEN_DAY_THRESHOLDS`, `HIT_PERCENT`, `UNKNOWN_WINDOW`, `webhook_setting(environ) -> SecretStr | None`, `Alert(limit_window, resets_at, target, percent)`, `due_alerts(snapshots, now) -> list[Alert]`, `ClaudeLimitsStore` (Protocol), `ClaudeLimitsWatcher(webhook_url, *, store, post=urllib_post, now=..., interval_s=INTERVAL_S, backoff_s=RETRY_BACKOFF_S)` with `start()`, `async stop()` and `async poll_once()`

- [ ] **Step 1: Write the failing message tests**

In `tests/test_notifications_messages.py`, change `from datetime import date` to `from datetime import UTC, date, datetime, timedelta, timezone`, and add `format_claude_limit_alert` to the `from issuebot.notifications.messages import ...` line. Append:

```python
# --- the Claude usage alert (spec 2026-10-02, claude-limits-alert) ----------------------------

AT = datetime(2026, 10, 2, 17, 47, tzinfo=UTC)
WEEK_RESET = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)


def test_a_claude_warning_names_the_window_its_share_and_its_reset() -> None:
    text = format_claude_limit_alert(window="seven_day", percent=77, resets_at=WEEK_RESET, now=AT)
    assert text == (
        ":warning: Claude: the 7-day usage window is 77% used; it resets Fri 9 Oct, 05:00 UTC."
    )


def test_a_claude_limit_hit_says_when_work_resumes() -> None:
    reset = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)
    text = format_claude_limit_alert(window="five_hour", percent=100, resets_at=reset, now=AT)
    assert text == (
        ":rotating_light: Claude: the 5-hour usage limit is reached; issuebot stops claiming "
        "issues until 20:00 UTC (in 2 h 13 min)."
    )


def test_a_seven_day_hit_names_the_day_it_lifts() -> None:
    text = format_claude_limit_alert(window="seven_day", percent=100, resets_at=WEEK_RESET, now=AT)
    assert text.startswith(":rotating_light: Claude: the 7-day usage limit is reached;")
    assert text.endswith("until Fri 9 Oct, 05:00 UTC (in 6 d 11 h).")


def test_a_reset_in_another_zone_is_written_in_utc() -> None:
    reset = datetime(2026, 10, 2, 21, 0, tzinfo=timezone(timedelta(hours=1)))
    text = format_claude_limit_alert(window="five_hour", percent=100, resets_at=reset, now=AT)
    assert "until 20:00 UTC (in 2 h 13 min)." in text


@pytest.mark.parametrize(
    ("resets_in", "words"),
    [
        (timedelta(seconds=30), "in 1 min"),
        (timedelta(minutes=42), "in 42 min"),
        (timedelta(hours=2, minutes=13), "in 2 h 13 min"),
        (timedelta(days=1, hours=3, minutes=5), "in 1 d 3 h"),
    ],
)
def test_a_limit_hit_says_how_long_until_it_lifts(resets_in: timedelta, words: str) -> None:
    text = format_claude_limit_alert(
        window="five_hour", percent=100, resets_at=AT + resets_in, now=AT
    )
    assert text.endswith(f"({words}).")


def test_a_window_claude_has_not_named_before_is_escaped() -> None:
    text = format_claude_limit_alert(
        window="<!channel>", percent=100, resets_at=AT + timedelta(hours=1), now=AT
    )
    assert text.startswith(
        ":rotating_light: Claude: the usage limit (&lt;!channel&gt;) is reached;"
    )


def test_a_hit_with_no_window_names_none() -> None:
    text = format_claude_limit_alert(
        window=None, percent=100, resets_at=AT + timedelta(hours=1), now=AT
    )
    assert text.startswith(":rotating_light: Claude: the usage limit is reached;")
```

The expected strings: 2 October 2026 is a Friday; from 17:47 that day, 20:00 is 2 h 13 min away and Friday 9 October 05:00 is 6 d 11 h 13 min away, which reads `in 6 d 11 h` (whole days and hours, minutes dropped past a day).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_notifications_messages.py -q`
Expected: FAIL with `ImportError: cannot import name 'format_claude_limit_alert'`.

- [ ] **Step 3: Write `format_claude_limit_alert`**

In `src/issuebot/notifications/messages.py`, add `import math`. Change `from datetime import date` to `from datetime import UTC, date, datetime`. Append:

```python
# The Claude usage windows by the names claude gives them (`rateLimitType`, `unifiedWindows`).
_CLAUDE_WINDOWS = {"five_hour": "5-hour", "seven_day": "7-day"}


def format_claude_limit_alert(
    *, window: str | None, percent: int, resets_at: datetime, now: datetime
) -> str:
    """The Claude usage line (spec 2026-10-02, claude-limits-alert): below 100 a warning that a
    window is filling, at 100 a limit that has stopped the board. Times are UTC; a window name
    claude has not used before is shown as reported, escaped."""
    when = _utc_moment(resets_at, now)
    if percent < 100:
        return (
            f":warning: Claude: {_claude_window(window, 'window')} is {percent}% used; "
            f"it resets {when}."
        )
    return (
        f":rotating_light: Claude: {_claude_window(window, 'limit')} is reached; issuebot "
        f"stops claiming issues until {when} ({_time_until(resets_at, now)})."
    )


def _claude_window(window: str | None, noun: str) -> str:
    if window in _CLAUDE_WINDOWS:
        return f"the {_CLAUDE_WINDOWS[window]} usage {noun}"
    if window:
        return f"the usage {noun} ({_escape(window)})"
    return f"the usage {noun}"


def _utc_moment(moment: datetime, now: datetime) -> str:
    """``20:00 UTC`` on today's UTC date, else ``Fri 9 Oct, 05:00 UTC`` (no glibc-only ``%-d``)."""
    moment, today = moment.astimezone(UTC), now.astimezone(UTC).date()
    clock = f"{moment:%H:%M} UTC"
    if moment.date() == today:
        return clock
    return f"{moment:%a} {moment.day} {moment:%b}, {clock}"


def _time_until(moment: datetime, now: datetime) -> str:
    """``in 42 min``, ``in 2 h 13 min`` or ``in 6 d 11 h``: minutes rounded up, never negative."""
    minutes = max(math.ceil((moment - now).total_seconds() / 60), 0)
    if minutes < 60:
        return f"in {minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"in {hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"in {days} d {hours} h"
```

Run: `uv run pytest tests/test_notifications_messages.py -q`
Expected: PASS.

- [ ] **Step 4: Make the snapshot's timestamp reader public**

In `src/issuebot/web/views.py`, rename `_moment` to `parse_moment`, at its definition and its one call in `_window_percent`. Give it this docstring: `"""An ISO 8601 timestamp out of the snapshot's JSON, read as UTC when it names no zone; None for anything else."""`. The watcher reads the same JSON, so it shares the parser rather than copying it.

- [ ] **Step 5: Write the failing watcher tests**

Create `tests/test_web_claude_limits.py`:

```python
"""The Claude usage watcher and its Slack alert, against fake snapshots, store and webhook."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from fakes.web import NOW, limits, snapshot
from issuebot.db import StoreUnavailableError
from issuebot.db.queries import SnapshotRow
from issuebot.notifications.slack import PostResult
from issuebot.orchestrator.state import DispatchHold
from issuebot.web.claude_limits import (
    RETRY_BACKOFF_S,
    ClaudeLimitsWatcher,
    webhook_setting,
)

WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"
# fakes.web's NOW is Friday 4 September 2026, 12:00 UTC; `limits()` resets its 7-day window
# three days later, on Monday 7 September at 12:00.
WEEK_RESET = NOW + timedelta(days=3)
WALL = NOW + timedelta(hours=2, minutes=13)


def usage_hold(until: datetime | None = WALL, window: str | None = "five_hour") -> DispatchHold:
    return DispatchHold(
        kind="usage",
        reason="claude usage limit reached: You've hit your session limit",
        since=NOW - timedelta(minutes=1),
        until=until,
        window=window,
    )


class Store:
    """The table's semantics in memory, and the snapshots the watcher reads."""

    def __init__(self, *rows: SnapshotRow) -> None:
        self.rows = {f"acme/repo-{n}": row for n, row in enumerate(rows)}
        self.alerted: dict[tuple[str, datetime], int] = {}
        self.claims: list[tuple[str, datetime, int]] = []
        self.reads = 0
        self.unreachable = 0
        self.release_fails = False

    def show(self, *rows: SnapshotRow) -> None:
        self.rows = {f"acme/repo-{n}": row for n, row in enumerate(rows)}

    @asynccontextmanager
    async def queries(self) -> AsyncIterator["Store"]:
        if self.unreachable:
            self.unreachable -= 1
            raise StoreUnavailableError("cannot connect: refused")
        yield self

    async def snapshots(self) -> dict[str, SnapshotRow]:
        self.reads += 1
        return dict(self.rows)

    async def claude_limit_alerted(self, *, limit_window: str, resets_at: datetime) -> int:
        return self.alerted.get((limit_window, resets_at), 0)

    async def claim_claude_limit_alert(
        self, *, limit_window: str, resets_at: datetime, target: int
    ) -> bool:
        if self.alerted.get((limit_window, resets_at), 0) >= target:
            return False
        self.alerted[(limit_window, resets_at)] = target
        self.claims.append((limit_window, resets_at, target))
        return True

    async def release_claude_limit_alert(
        self, *, limit_window: str, resets_at: datetime, target: int, previous: int
    ) -> None:
        if self.release_fails:
            raise StoreUnavailableError("cannot connect: refused")
        if self.alerted.get((limit_window, resets_at)) == target:
            self.alerted[(limit_window, resets_at)] = previous


class Webhook:
    def __init__(self, status: int | None = 200) -> None:
        self.status = status
        self.texts: list[str] = []

    async def __call__(self, url: str, payload: bytes, *, timeout_s: float) -> PostResult:
        assert url == WEBHOOK
        self.texts.append(json.loads(payload)["text"])
        ok = self.status == 200
        return PostResult(status=self.status, error=None if ok else "HTTP Error 500")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make(
    store: Store,
    webhook: Webhook | None = None,
    clock: Clock | None = None,
    *,
    interval_s: float = 60.0,
) -> ClaudeLimitsWatcher:
    moment = clock or Clock()
    return ClaudeLimitsWatcher(
        SecretStr(WEBHOOK),
        store=store,
        post=webhook or Webhook(),
        now=lambda: moment.now,
        interval_s=interval_s,
    )


# --- the setting -------------------------------------------------------------------------------


def test_the_webhook_turns_the_watcher_on_and_its_absence_off() -> None:
    assert webhook_setting({}) is None
    assert webhook_setting({"SLACK_WEBHOOK_URL": "  "}) is None
    setting = webhook_setting({"SLACK_WEBHOOK_URL": f" {WEBHOOK}\n"})
    assert setting is not None and setting.get_secret_value() == WEBHOOK


# --- 7-day warnings ----------------------------------------------------------------------------


async def test_a_seven_day_reading_past_75_posts_once() -> None:
    store, webhook = Store(snapshot(rate_limits=limits(seven=0.77))), Webhook()
    watcher = make(store, webhook)
    await watcher.poll_once()
    await watcher.poll_once()
    assert webhook.texts == [
        ":warning: Claude: the 7-day usage window is 77% used; it resets Mon 7 Sep, 12:00 UTC."
    ]
    assert store.claims == [("seven_day", WEEK_RESET, 75)]


async def test_90_follows_75_in_the_same_window() -> None:
    store, webhook = Store(snapshot(rate_limits=limits(seven=0.77))), Webhook()
    watcher = make(store, webhook)
    await watcher.poll_once()
    store.show(snapshot(rate_limits=limits(seven=0.91)))
    await watcher.poll_once()
    assert [claim[2] for claim in store.claims] == [75, 90]
    assert "is 91% used" in webhook.texts[1]


async def test_a_jump_past_both_thresholds_posts_90_alone() -> None:
    store, webhook = Store(snapshot(rate_limits=limits(seven=0.95))), Webhook()
    await make(store, webhook).poll_once()
    assert store.claims == [("seven_day", WEEK_RESET, 90)] and len(webhook.texts) == 1


async def test_below_75_posts_nothing_and_the_5_hour_window_never_warns() -> None:
    store, webhook = Store(snapshot(rate_limits=limits(five=0.99, seven=0.74))), Webhook()
    await make(store, webhook).poll_once()
    assert (store.claims, webhook.texts) == ([], [])


async def test_a_new_week_starts_afresh() -> None:
    store, webhook = Store(snapshot(rate_limits=limits(seven=0.80))), Webhook()
    watcher = make(store, webhook)
    await watcher.poll_once()
    store.show(snapshot(rate_limits=limits(seven=0.80, seven_resets_in=timedelta(days=10))))
    await watcher.poll_once()
    assert [claim[1] for claim in store.claims] == [WEEK_RESET, NOW + timedelta(days=10)]


async def test_the_highest_reading_of_one_window_is_the_one_posted() -> None:
    """Review Focus 2: two workers on one subscription, one fresher than the other."""
    store = Store(
        snapshot(rate_limits=limits(seven=0.70, observed_ago=timedelta(days=2))),
        snapshot(rate_limits=limits(seven=0.80)),
    )
    webhook = Webhook()
    await make(store, webhook).poll_once()
    assert store.claims == [("seven_day", WEEK_RESET, 75)]
    assert len(webhook.texts) == 1 and "is 80% used" in webhook.texts[0]


async def test_an_expired_reading_posts_nothing() -> None:
    """Review Focus 3: a stopped worker's reading from a window that has since reset."""
    stale = limits(seven=0.95, seven_resets_in=timedelta(minutes=-1))
    store, webhook = Store(snapshot(rate_limits=stale)), Webhook()
    await make(store, webhook).poll_once()
    assert webhook.texts == []


# --- limit hits --------------------------------------------------------------------------------


async def test_every_worker_on_one_wall_posts_one_hit() -> None:
    """Review Focus 2: every worker on the subscription is refused against the same reset."""
    store = Store(snapshot(dispatch_hold=usage_hold()), snapshot(dispatch_hold=usage_hold()))
    webhook = Webhook()
    await make(store, webhook).poll_once()
    assert webhook.texts == [
        ":rotating_light: Claude: the 5-hour usage limit is reached; issuebot stops claiming "
        "issues until 14:13 UTC (in 2 h 13 min)."
    ]
    assert store.claims == [("five_hour", WALL, 100)]


async def test_a_later_wall_posts_again() -> None:
    store, webhook = Store(snapshot(dispatch_hold=usage_hold())), Webhook()
    watcher = make(store, webhook)
    await watcher.poll_once()
    store.show(snapshot(dispatch_hold=usage_hold(until=WALL + timedelta(hours=5))))
    await watcher.poll_once()
    assert len(webhook.texts) == 2


async def test_an_expired_or_undated_hold_posts_nothing() -> None:
    """Review Focus 1 and 3: a hold claude did not date, and one whose window has reopened."""
    store = Store(
        snapshot(dispatch_hold=usage_hold(until=None)),
        snapshot(dispatch_hold=usage_hold(until=NOW - timedelta(minutes=1))),
    )
    webhook = Webhook()
    await make(store, webhook).poll_once()
    assert (store.claims, webhook.texts) == ([], [])


async def test_a_hold_claude_did_not_name_is_keyed_unknown() -> None:
    store, webhook = Store(snapshot(dispatch_hold=usage_hold(window=None))), Webhook()
    await make(store, webhook).poll_once()
    assert store.claims == [("unknown", WALL, 100)]
    assert webhook.texts[0].startswith(":rotating_light: Claude: the usage limit is reached;")


async def test_a_seven_day_hit_says_so_once_instead_of_a_warning() -> None:
    """The 7-day window's warning and its hit share a key; a cycle that sees both posts the hit."""
    store = Store(
        snapshot(
            rate_limits=limits(seven=1.0),
            dispatch_hold=usage_hold(until=WEEK_RESET, window="seven_day"),
        )
    )
    webhook = Webhook()
    await make(store, webhook).poll_once()
    assert store.claims == [("seven_day", WEEK_RESET, 100)]
    assert len(webhook.texts) == 1 and webhook.texts[0].startswith(":rotating_light:")


async def test_another_kind_of_hold_posts_nothing() -> None:
    held = DispatchHold(kind="auth", reason="not logged in", since=NOW, until=WALL)
    store, webhook = Store(snapshot(dispatch_hold=held)), Webhook()
    await make(store, webhook).poll_once()
    assert webhook.texts == []


async def test_snapshots_it_cannot_read_are_skipped() -> None:
    row = snapshot()
    row.data["rate_limits"] = {"seven_day": {"utilization": "most", "resets_at": "soon"}}
    row.data["dispatch_hold"] = {"kind": "usage", "reason": "x", "until": "not a time"}
    store, webhook = Store(row), Webhook()
    await make(store, webhook).poll_once()
    assert webhook.texts == []


# --- memory and failure ------------------------------------------------------------------------


async def test_a_restart_posts_nothing_new() -> None:
    """Review Focus 4: a second process over the same table."""
    store, webhook = Store(snapshot(rate_limits=limits(seven=0.80))), Webhook()
    await make(store, webhook).poll_once()
    await make(store, webhook).poll_once()
    assert len(webhook.texts) == 1


async def test_a_dead_webhook_is_retried_only_after_the_backoff() -> None:
    """Review Focus 5."""
    store, webhook, clock = Store(snapshot(rate_limits=limits(seven=0.80))), Webhook(500), Clock()
    with capture_logs() as logs:
        watcher = make(store, webhook, clock)
        await watcher.poll_once()
        assert store.alerted[("seven_day", WEEK_RESET)] == 0
        clock.advance(60)
        await watcher.poll_once()
        assert len(webhook.texts) == 1
        clock.advance(RETRY_BACKOFF_S)
        webhook.status = 200
        await watcher.poll_once()
        await watcher.poll_once()
    assert len(webhook.texts) == 2
    assert store.alerted[("seven_day", WEEK_RESET)] == 75
    assert [entry["event"] for entry in logs if entry["event"].startswith("claude_limits_")] == [
        "claude_limits_alert_failed",
        "claude_limits_alert_sent",
    ]
    assert WEBHOOK not in repr(logs)


async def test_a_failed_release_still_logs_the_failed_post() -> None:
    store, webhook = Store(snapshot(rate_limits=limits(seven=0.80))), Webhook(500)
    store.release_fails = True
    with capture_logs() as logs:
        watcher = make(store, webhook)
        with pytest.raises(StoreUnavailableError):
            await watcher.poll_once()
    assert any(entry["event"] == "claude_limits_alert_failed" for entry in logs)


# --- the loop ----------------------------------------------------------------------------------


async def test_start_runs_a_cycle_and_stop_ends_the_loop() -> None:
    store = Store(snapshot(rate_limits=limits(seven=0.80)))
    watcher = make(store, interval_s=3600.0)
    watcher.start()
    watcher.start()  # a second start is a no-op, not a second loop
    for _ in range(50):
        await asyncio.sleep(0)
    await watcher.stop()
    await watcher.stop()
    assert store.reads == 1


async def test_a_database_error_is_logged_and_the_next_cycle_retries() -> None:
    store = Store(snapshot(rate_limits=limits(seven=0.80)))
    store.unreachable = 1
    webhook = Webhook()
    with capture_logs() as logs:
        watcher = make(store, webhook, interval_s=0)
        watcher.start()
        for _ in range(100):
            if webhook.texts:
                break
            await asyncio.sleep(0)
        await watcher.stop()
    assert len(webhook.texts) == 1
    assert any(entry["event"] == "claude_limits_store_failed" for entry in logs)
```

- [ ] **Step 6: Run them to verify they fail**

Run: `uv run pytest tests/test_web_claude_limits.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.web.claude_limits'`.

- [ ] **Step 7: Write the watcher**

Create `src/issuebot/web/claude_limits.py`:

```python
"""The Claude usage limits watcher, and the Slack alert it posts (spec 2026-10-02,
claude-limits-alert).

Runs in the hub's web process beside the Actions minutes poller, whenever the web has
``SLACK_WEBHOOK_URL``. Every worker writes its newest Claude usage reading and its dispatch hold
into its repository's ``runtime_snapshot`` row; once a minute this reads every row -- one query,
no API call, no credential but the webhook -- and posts when the 7-day window passes 75% or 90%,
or when a worker holds dispatch because claude refused a turn on usage.

``claude_limit_alerts`` is the alert's memory, one row per window instance: claude's window name
and the moment it reopens. One subscription reports one reset to every worker, so every worker
on one wall posts once, and a restart -- which every upgrade is -- finds what was already posted.
A claim is taken before the post and given back if the post fails; a cancellation (shutdown)
between the two leaves the claim held, which keeps delivery at most once.
"""

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import SecretStr

from issuebot.db.errors import DatabaseError
from issuebot.db.queries import SnapshotRow
from issuebot.log import get_logger
from issuebot.notifications.messages import format_claude_limit_alert
from issuebot.notifications.slack import POST_TIMEOUT_S, Poster, slack_payload, urllib_post
from issuebot.web.actions import WEBHOOK_ENV
from issuebot.web.views import parse_moment

INTERVAL_S = 60.0
# A failed post is tried again no sooner than this, so a dead webhook is not called every minute.
RETRY_BACKOFF_S = 900.0
SEVEN_DAY = "seven_day"
# Percent of the 7-day window, each posted at most once per window. The 5-hour window has no
# warnings -- it resets several times a day -- and neither has a 100% one: claude refusing is
# the hit, and the hit is posted from the hold. Constants, as SlackSink's retry policy is.
SEVEN_DAY_THRESHOLDS: tuple[int, ...] = (75, 90)
HIT_PERCENT = 100
# The key's window for a refusal claude did not name.
UNKNOWN_WINDOW = "unknown"


def webhook_setting(environ: Mapping[str, str]) -> SecretStr | None:
    """The webhook the alert posts to, or None when it is unset and the watcher is off."""
    webhook = environ.get(WEBHOOK_ENV, "").strip()
    return SecretStr(webhook) if webhook else None


@dataclass(frozen=True, slots=True)
class Alert:
    """One alert the snapshots call for: the memory's key, the claim's target, the figure shown."""

    limit_window: str  # seven_day for a warning; the window claude refused on, or "unknown"
    resets_at: datetime  # when that window reopens: which instance of it
    target: int  # 75 or 90 for a warning, 100 for a hit
    percent: int  # what the message says: the utilisation rounded, or 100 for a hit


def due_alerts(snapshots: Mapping[str, SnapshotRow], now: datetime) -> list[Alert]:
    """The alerts the workers' snapshots call for at ``now``, before the memory is asked.

    Total over the JSON, as ``views`` is: a field it cannot read is no reading and no hold. A
    reading or a hold whose window has already reopened is history, not news.
    """
    highest: dict[datetime, float] = {}
    walls: dict[tuple[str, datetime], None] = {}
    for row in snapshots.values():
        reading = _seven_day(row.data)
        if reading is not None and reading[1] > now:
            utilization, resets_at = reading
            # One subscription is one key, and use only rises within a window: the highest
            # reading is the newest, whichever worker saw it.
            highest[resets_at] = max(utilization, highest.get(resets_at, 0.0))
        wall = _usage_wall(row.data)
        if wall is not None and wall[1] > now:
            walls[wall] = None
    alerts = []
    for resets_at, utilization in sorted(highest.items()):
        if (SEVEN_DAY, resets_at) in walls:
            continue  # the 7-day hit for the same window says more
        target = max((t for t in SEVEN_DAY_THRESHOLDS if utilization * 100 >= t), default=0)
        if target:
            percent = round(min(utilization, 1.0) * 100)
            alerts.append(Alert(SEVEN_DAY, resets_at, target, percent))
    alerts.extend(Alert(window, until, HIT_PERCENT, HIT_PERCENT) for window, until in walls)
    return alerts


def _seven_day(data: Mapping[str, Any]) -> tuple[float, datetime] | None:
    limits = data.get("rate_limits")
    window = limits.get(SEVEN_DAY) if isinstance(limits, dict) else None
    if not isinstance(window, dict):
        return None
    utilization = window.get("utilization")
    resets_at = parse_moment(window.get("resets_at"))
    if resets_at is None or isinstance(utilization, bool):
        return None
    if not isinstance(utilization, int | float):
        return None
    return float(utilization), resets_at


def _usage_wall(data: Mapping[str, Any]) -> tuple[str, datetime] | None:
    """A usage hold's window and reset; None for any other hold, or one claude did not date."""
    hold = data.get("dispatch_hold")
    if not isinstance(hold, dict) or hold.get("kind") != "usage":
        return None
    until = parse_moment(hold.get("until"))
    if until is None:
        return None
    window = hold.get("window")
    return (window if isinstance(window, str) and window else UNKNOWN_WINDOW), until


class SnapshotReader(Protocol):
    async def snapshots(self) -> dict[str, SnapshotRow]: ...


class ClaudeLimitsStore(Protocol):
    """What the watcher needs of ``issuebot.db.Database``."""

    def queries(self) -> AbstractAsyncContextManager[SnapshotReader]: ...

    async def claude_limit_alerted(self, *, limit_window: str, resets_at: datetime) -> int: ...

    async def claim_claude_limit_alert(
        self, *, limit_window: str, resets_at: datetime, target: int
    ) -> bool: ...

    async def release_claude_limit_alert(
        self, *, limit_window: str, resets_at: datetime, target: int, previous: int
    ) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ClaudeLimitsWatcher:
    """One cycle at start, then one every ``interval_s``; ``poll_once`` is the test seam."""

    def __init__(
        self,
        webhook_url: SecretStr,
        *,
        store: ClaudeLimitsStore,
        post: Poster = urllib_post,
        now: Callable[[], datetime] = _utcnow,
        interval_s: float = INTERVAL_S,
        backoff_s: float = RETRY_BACKOFF_S,
    ) -> None:
        self._webhook_url = webhook_url
        self._store = store
        self._post = post
        self._now = now
        self._interval_s = interval_s
        self._backoff = timedelta(seconds=backoff_s)
        # Per key, when a failed post may be tried again; in memory, since a restart may retry.
        self._retry_after: dict[tuple[str, datetime], datetime] = {}
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger(__name__)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="claude-limits")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except DatabaseError as exc:
                self._log.warning("claude_limits_store_failed", error=exc.message)
            except Exception:
                # Never let the watcher die quietly: the next cycle is the retry.
                self._log.exception("claude_limits_poll_crashed")
            await asyncio.sleep(self._interval_s)

    async def poll_once(self) -> None:
        now = self._now()
        self._retry_after = {key: at for key, at in self._retry_after.items() if key[1] > now}
        async with self._store.queries() as queries:
            snapshots = await queries.snapshots()
        for alert in due_alerts(snapshots, now):
            await self._alert(alert, now)

    async def _alert(self, alert: Alert, now: datetime) -> None:
        key = (alert.limit_window, alert.resets_at)
        retry_at = self._retry_after.get(key)
        if retry_at is not None and now < retry_at:
            return
        previous = await self._store.claude_limit_alerted(
            limit_window=alert.limit_window, resets_at=alert.resets_at
        )
        if alert.target <= previous:
            return
        claimed = await self._store.claim_claude_limit_alert(
            limit_window=alert.limit_window, resets_at=alert.resets_at, target=alert.target
        )
        if not claimed:
            return
        window = None if alert.limit_window == UNKNOWN_WINDOW else alert.limit_window
        text = format_claude_limit_alert(
            window=window, percent=alert.percent, resets_at=alert.resets_at, now=now
        )
        result = await self._post(
            self._webhook_url.get_secret_value(), slack_payload(text), timeout_s=POST_TIMEOUT_S
        )
        fields = {
            "window": alert.limit_window,
            "resets_at": alert.resets_at.isoformat(),
            "threshold": alert.target,
        }
        if result.ok:
            self._retry_after.pop(key, None)
            self._log.info("claude_limits_alert_sent", **fields)
            return
        # Logged before the release: a store failure there must not hide the failed post.
        self._log.warning(
            "claude_limits_alert_failed", **fields, status=result.status, error=result.error
        )
        self._retry_after[key] = now + self._backoff
        await self._store.release_claude_limit_alert(
            limit_window=alert.limit_window,
            resets_at=alert.resets_at,
            target=alert.target,
            previous=previous,
        )
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_claude_limits.py tests/test_notifications_messages.py tests/test_web_app.py tests/test_web_pages.py -q && uv run ruff check src tests && uv run ruff format --check src tests`
Expected: PASS, ruff clean. If a `capture_logs` test sees no entries, the logger was bound before capture began: build the watcher inside the `with capture_logs()` block, as the tests above do.

- [ ] **Step 9: Commit**

```bash
git add src/issuebot/notifications/messages.py src/issuebot/web/views.py src/issuebot/web/claude_limits.py tests/test_notifications_messages.py tests/test_web_claude_limits.py
git commit -F - <<'EOF'
web: watch the workers' Claude usage and alert Slack at 75 and 90 percent of the week, and on a hit

<the committing model's Co-Authored-By line>
Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
EOF
```

---

### Task 4: The app runs the watcher, and `issuebot web` builds it

**Files:**
- Modify: `src/issuebot/web/app.py` (`create_app(claude_limits=)`, the lifespan, the module docstring)
- Modify: `src/issuebot/cli.py` (`cmd_web`, `_run_web`, imports)
- Modify: `tests/test_web_claude_limits.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `ClaudeLimitsWatcher`, `webhook_setting` (Task 3); `create_app(..., actions=)` (existing).
- Produces: `create_app(database, *, password, clock=..., now=..., actions=None, claude_limits: ClaudeLimitsWatcher | None = None)`, which sets `app.state.claude_limits`; `_run_web(url, *, password, port, bind, actions=None, claude_webhook: SecretStr | None = None) -> int`.

- [ ] **Step 1: Write the failing app tests**

In `tests/test_web_claude_limits.py`, add `from fastapi.testclient import TestClient`, `from fakes.database import FakeDatabase`, `PASSWORD` to the `from fakes.web import ...` line, and `from issuebot.web import create_app`, each in its sorted place. Then append:

```python
# --- the app -----------------------------------------------------------------------------------


class Task:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    def start(self) -> None:
        self.events.append(f"start {self.name}")

    async def stop(self) -> None:
        self.events.append(f"stop {self.name}")


def test_the_app_starts_and_stops_both_background_tasks() -> None:
    events: list[str] = []
    actions, claude = Task("actions", events), Task("claude", events)
    app = create_app(FakeDatabase(), password=PASSWORD, actions=actions, claude_limits=claude)  # type: ignore[arg-type]
    assert app.state.claude_limits is claude
    with TestClient(app):
        assert events == ["start actions", "start claude"]
    assert events == ["start actions", "start claude", "stop claude", "stop actions"]


def test_the_watcher_runs_without_the_actions_poller() -> None:
    events: list[str] = []
    app = create_app(FakeDatabase(), password=PASSWORD, claude_limits=Task("claude", events))  # type: ignore[arg-type]
    with TestClient(app):
        pass
    assert events == ["start claude", "stop claude"]


def test_an_app_without_a_watcher_has_none() -> None:
    assert create_app(FakeDatabase(), password=PASSWORD).state.claude_limits is None
```

Run: `uv run pytest tests/test_web_claude_limits.py -k app -q`
Expected: FAIL with `TypeError: create_app() got an unexpected keyword argument 'claude_limits'`.

- [ ] **Step 2: Give `create_app` the watcher**

In `src/issuebot/web/app.py`, add `from issuebot.web.claude_limits import ClaudeLimitsWatcher` beside the `issuebot.web.actions` import. Add the keyword parameter `claude_limits: ClaudeLimitsWatcher | None = None,` after `actions` in `create_app`'s signature. Append to its docstring: ``` ``claude_limits`` is the Claude usage watcher the CLI builds when ``SLACK_WEBHOOK_URL`` is set; it starts and stops with the app too.```. Replace the `lifespan` function and the `app.state.actions = actions` line with:

```
    background = [task for task in (actions, claude_limits) if task is not None]

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # The background tasks live as long as the server does: the Actions minutes poller and
        # the Claude usage watcher (specs 2026-10-02), stopped in the reverse order.
        for task in background:
            task.start()
        try:
            yield
        finally:
            for task in reversed(background):
                await task.stop()

    app = FastAPI(
        title="issuebot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.actions = actions
    app.state.claude_limits = claude_limits
```

In the module docstring, replace the sentence that begins `No request writes to a table` with:

```
No request writes to a table -- a request's one write is ``NOTIFY`` --
and the app's table writes are its two background tasks': the Actions minutes poller's, to
``actions_minutes``, when the CLI builds one because the billing token is set
(``issuebot.web.actions``), and the Claude usage watcher's, to ``claude_limit_alerts``, when
``SLACK_WEBHOOK_URL`` is set (``issuebot.web.claude_limits``).
```

Rewrap the paragraph to the file's width.

Run: `uv run pytest tests/test_web_claude_limits.py tests/test_web_actions.py -q`
Expected: PASS. The existing `test_the_app_starts_and_stops_the_poller_with_its_lifespan` in `tests/test_web_actions.py` still passes.

- [ ] **Step 3: Write the failing CLI tests**

In `tests/test_cli.py`, add `from issuebot.web.claude_limits import ClaudeLimitsWatcher` to the imports. After `test_web_refuses_a_malformed_allowance_before_migrating`, add:

```python
SLACK_WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"


def test_web_builds_no_claude_limits_watcher_without_a_webhook(
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    assert main(["web"]) == 0
    ((app, _host, _port),) = fake_serve.calls
    assert app.state.claude_limits is None  # type: ignore[attr-defined]


def test_web_builds_the_claude_limits_watcher_from_the_webhook_alone(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    """No billing token: the Claude alert needs nothing but the webhook (spec 2026-10-02)."""
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", f" {SLACK_WEBHOOK}\n")
    assert main(["web"]) == 0
    ((app, _host, _port),) = fake_serve.calls
    assert isinstance(app.state.claude_limits, ClaudeLimitsWatcher)  # type: ignore[attr-defined]
    assert app.state.actions is None  # type: ignore[attr-defined]
    err = capsys.readouterr().err
    assert "web_started" in err and SLACK_WEBHOOK not in err
```

Run: `uv run pytest tests/test_cli.py -k claude_limits -q`
Expected: `..._from_the_webhook_alone` FAILS, because `app.state.claude_limits` is None. `..._without_a_webhook` already passes, since Step 2 set the attribute; it pins that the CLI keeps it that way.

- [ ] **Step 4: Wire the CLI**

In `src/issuebot/cli.py`, add `from pydantic import SecretStr` if it is not imported (`grep -n "^from pydantic" src/issuebot/cli.py`), and `from issuebot.web.claude_limits import ClaudeLimitsWatcher, webhook_setting` beside the `issuebot.web.actions` import.

In `cmd_web`, replace the final `return asyncio.run(...)` with:

```
    return asyncio.run(
        _run_web(
            url,
            password=password,
            port=args.port,
            bind=args.bind,
            actions=actions,
            claude_webhook=webhook_setting(os.environ),
        )
    )
```

In `_run_web`, add the keyword parameter `claude_webhook: SecretStr | None = None` after `actions`. Append to its docstring: ` The Claude usage watcher runs whenever the webhook is set, token or no token.` After the `if actions is not None:` block, add:

```
    watcher = (
        ClaudeLimitsWatcher(claude_webhook, store=database) if claude_webhook is not None else None
    )
```

Add `claude_limits=watcher is not None,` to the `web_started` log call, after `actions_minutes=...`, and pass `claude_limits=watcher` to `create_app(...)`. Wrap the `_serve(...)` line if ruff asks.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests`
Expected: PASS, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add src/issuebot/web/app.py src/issuebot/cli.py tests/test_web_claude_limits.py tests/test_cli.py
git commit -F - <<'EOF'
cli: issuebot web runs the Claude usage watcher whenever SLACK_WEBHOOK_URL is set

<the committing model's Co-Authored-By line>
Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
EOF
```

---

### Task 5: Documentation

**Files:**
- Modify: `docs/dashboard.md` (a sentence at the end of the limits-tile paragraph; a new `## Claude usage alerts` section after `## GitHub Actions minutes`)
- Modify: `docs/operations.md` (a new `### A spent Claude usage window` between `### Cost` and `### Restarts`)
- Modify: `README.md` (the sentence about the hub's `web` reading `SLACK_WEBHOOK_URL`, ~line 660)
- Modify: `docs/package-layout.md` (the `DispatchHold` sentence in `issuebot.orchestrator`; the `issuebot.db`, `issuebot.web` and `issuebot.notifications` sections)

**Interfaces:** none.

Verify every factual claim below against the code from Tasks 1-4 before committing: event names, constants and behaviour. If the prose says something the code does not do, fix the prose and say so in the report.

- [ ] **Step 1: `docs/dashboard.md`**

At the end of the paragraph that begins `The limits tile is what a Claude subscription is actually rationed by` (it ends `does not blank the tile until the next dispatch.`), append:

```
With `SLACK_WEBHOOK_URL` set, the hub also posts to Slack as the 7-day window fills and when
either window stops the board ([Claude usage alerts](#claude-usage-alerts)).
```

Insert before `## What "issues closed" counts`:

```
## Claude usage alerts

With `SLACK_WEBHOOK_URL` in the hub checkout's `.env` -- the webhook the [GitHub Actions
minutes](#github-actions-minutes) alert uses, and nothing else; no token is needed -- the hub's
`web` reads every worker's snapshot once a minute and posts to Slack:

- when the 7-day window reaches 75%, and again at 90%, once each per week:
  `:warning: Claude: the 7-day usage window is 77% used; it resets Fri 9 Oct, 05:00 UTC.`
- when claude refuses a turn because a window is spent and a worker stops claiming issues, once
  for that window however many workers hit it:
  `:rotating_light: Claude: the 5-hour usage limit is reached; issuebot stops claiming issues
  until 20:00 UTC (in 2 h 13 min).`

The 5-hour window has no warnings, since it resets several times a day, and there is no message
when work resumes: the hit already says when. Times are UTC. A reading only moves while one of
issuebot's own turns is running, so use from interactive Claude sessions on the same
subscription shows at issuebot's next turn, not before. What has been posted is kept in the
`claude_limit_alerts` table, one row per window and reset time, so a restart of `web` never
posts an alert twice, and a failed post is tried again after fifteen minutes.
`notifications.slack.events` does not govern it: that list is the worker's.
```

- [ ] **Step 2: `docs/operations.md`**

Insert before `### Restarts`:

```
### A spent Claude usage window

When claude refuses a turn because the subscription's 5-hour or 7-day window is spent, the
worker holds dispatch until the moment claude says the window reopens, requeues the issue on the
same attempt, and escalates nothing: no issue is at fault and there is nothing to fix.
`issuebot status` prints `dispatch: held (usage)` and the dashboard's worker line shows the
hold. It lifts on its own at the reset. With `SLACK_WEBHOOK_URL` set on the hub, Slack is told
once when the limit is hit, with the time work resumes, and beforehand as the 7-day window passes
75% and 90% ([Claude usage alerts](dashboard.md#claude-usage-alerts)).
```

- [ ] **Step 3: `README.md`**

Change `` On the hub the `web` service reads `SLACK_WEBHOOK_URL` too, to post the [GitHub Actions minutes](docs/dashboard.md#github-actions-minutes) alert. `` (wrapped across lines ~660-662) to:

```
On the hub the `web` service reads
`SLACK_WEBHOOK_URL` too, to post the [GitHub Actions
minutes](docs/dashboard.md#github-actions-minutes) and [Claude
usage](docs/dashboard.md#claude-usage-alerts) alerts.
```

Rewrap to the paragraph's width.

- [ ] **Step 4: `docs/package-layout.md`** (under 2 KB in total)

- `issuebot.orchestrator`: in the sentence `Every hold is carried in the snapshot as dispatch_hold (#29), a DispatchHold(kind, reason, since) ...`, write `DispatchHold(kind, reason, since, until=, window=)`. After the `usage` kind's description `(... the spent-window hold above)`, add: `, which alone also carries until -- the reset claude reported, never the one-interval fallback -- and window, the rateLimitType it refused on (TurnResult and RunResult carry it as usage_window beside usage_reset_at)`. Format the code names in backticks as the surrounding text does.
- `issuebot.db`, appended as a paragraph:
  ```
  `0006_claude_limit_alerts.sql` and `claude_limits.py`: the Claude usage alert's memory, one
  row per window instance `(limit_window, resets_at)`, written only by the web's watcher:
  `Database.claude_limit_alerted` reads it, `claim_claude_limit_alert` writes a target only when
  it raises the stored percent, and `release_claude_limit_alert` gives a failed post's claim back.
  ```
- `issuebot.web`: in the header, `create_app(database, *, password, clock=, now=, actions=)` becomes `create_app(database, *, password, clock=, now=, actions=, claude_limits=)`; append a paragraph:
  ```
  `claude_limits.py`: `ClaudeLimitsWatcher`, built when `SLACK_WEBHOOK_URL` is set
  (`webhook_setting`) and run by the same lifespan, reads every `runtime_snapshot` row once a
  minute; `due_alerts` turns them into 7-day warnings (`SEVEN_DAY_THRESHOLDS`, at the highest
  utilisation per reset time) and hits (a `usage` hold's `until` and `window`), each claimed
  in `claude_limit_alerts` before it is posted, a failed one retried after `RETRY_BACKOFF_S`.
  `views.parse_moment` is the snapshot timestamp reader they share, and `views.dispatch_hold`
  passes a hold's `until` and `window` through to `/state`.
  ```
- `issuebot.notifications`: change `and by web for one message` to `and by web for two messages`, keeping the backticks, and append: ``` `format_claude_limit_alert` is the Claude usage line: a 7-day warning below 100, a limit hit at 100, times in UTC.```

- [ ] **Step 5: Run the documentation tests**

Run: `uv run pytest tests/test_doc_pointers.py tests/test_readme_bounds.py tests/test_instruction_bounds.py tests/test_package_layout.py -q && uv run pre-commit run --all-files`
Expected: PASS. If `test_doc_pointers` rejects an anchor, compare it with the heading's GitHub slug: `Claude usage alerts` becomes `claude-usage-alerts`. If `test_readme_bounds` fails, the edit moved a pinned passage; restore that passage's wording.

- [ ] **Step 6: Commit**

```bash
git add docs/dashboard.md docs/operations.md README.md docs/package-layout.md
git commit -F - <<'EOF'
docs: the Claude usage alert, and what a spent usage window does

<the committing model's Co-Authored-By line>
Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
EOF
```

---

### Task 6: Whole-branch verification

**Files:** none changed; this task runs everything and reports.

- [ ] **Step 1: Lint and the hermetic suite**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files && uv run pytest -q`
Expected: all clean and PASS, with the DB tests skipped.

- [ ] **Step 2: The DB tests against a throwaway server**

```bash
docker compose --profile test up -d --wait test-db
DATABASE_URL=postgresql://issuebot@$(docker compose port test-db 5432)/issuebot uv run pytest -q
docker compose rm -sf test-db
```
Expected: PASS, with nothing skipped for want of a database. Use `rm -sf test-db`, never `docker compose down`.

- [ ] **Step 3: Compose under every profile**

Run: `for p in hub worker hub,worker; do COMPOSE_PROFILES=$p docker compose config --quiet && echo "$p ok"; done`
Expected: three `ok` lines.

- [ ] **Step 4: Report**

Report the commands run and their results, and the commits on the branch (`git log --oneline main..`).

The live check comes after merging and upgrading every checkout (the workers record `until` only once upgraded):
1. `docker compose logs web | grep web_started` shows `claude_limits=True` on the hub.
2. The hub's `web` posts the 7-day warning on its first cycle if the current week's reading is already at or past 75%; `docker compose logs web | grep claude_limits_alert` shows `claude_limits_alert_sent`.
3. The next time claude refuses a turn, Slack shows one `:rotating_light:` line naming the window and the time work resumes, whichever workers were refused.
