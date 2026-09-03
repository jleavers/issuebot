# Phase 5: Slack Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Slack channel sees every state transition and every blocked run as it happens, with the issue and pull-request links, without the orchestrator ever waiting on Slack; `issuebot validate` checks the webhook and can post one test message; the transition an agent lands while its worker is being stopped reaches the bus (a Phase 4 follow-up).

**Architecture:** A new `issuebot.notifications` package in two modules. `messages.py` turns an event into one line of Slack mrkdwn (pure). `slack.py` holds the stdlib `urllib` transport (run in a worker thread, never raises, errors redacted), and `SlackSink`, whose `handle` only formats and enqueues while one drain task posts with bounded retry and publishes `NotificationSent`. The CLI owns the sink's lifetime around `run-once` and `worker`, and `validate` gains a real `notifications.slack` check plus `--slack-probe`. A first task amends the orchestrator's exit handler so a `succeeded` result publishes its final transition before any release row.

**Tech Stack:** Python 3.14, asyncio, `urllib.request` via `asyncio.to_thread`, the Phase 1 `EventBus`/`EventSink`, pydantic settings (`SlackSettings` unchanged), structlog, pytest + pytest-asyncio (`asyncio_mode = "auto"`), `http.server.ThreadingHTTPServer` on loopback for the transport tests, ruff 0.16.5. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-03-phase-5-slack-notifications-design.md` (parent: `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`; Phase 4: `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md`).

## Global Constraints

Every task's requirements include this section. Every implementer and reviewer dispatch must carry it verbatim.

- Python >=3.14, `uv run` for everything; no new dependency (the spec names none: `pyproject.toml` and `uv.lock` do not change in this phase; the transport is stdlib `urllib`).
- Work on branch `phase-5-slack`; the spec and this plan are its first two commits. Never push to `main`, never merge or close PRs, never `rm -rf`, `git reset --hard` or `git clean -fd`; the SDD workspaces under `.superpowers/sdd/` are left for the operator to delete. Linux host: Bash, `&&` chaining.
- A Bash-level hook on this host blocks any shell command whose text contains the dot-env filename (the literal `.` + `env`, including `.example` and heredoc bodies); such files are written with Write/Edit, staged with `git add --all` after `git status --short`; say "dot-env" in commit messages and reports.
- The ruff-format pre-commit hook (v0.16.5) reflows Python fences inside docs/**/*.md; write fences pre-formatted (double quotes, line length 100, trailing commas) and re-`git add` after `pre-commit run --all-files`.
- ruff rules E F I UP B N SIM RUF, target py314. SIM300 ranks literal > ALL_CAPS name > other expression and flags a comparison whose left side ranks higher (`MIN_CLAUDE_VERSION == (2, 1, 259)`, `{...} == ACTIVE_STATES`); apply ruff's fix, never suppress, never a per-file ignore. N818: exception classes end in `Error` or carry `# noqa: N818` by ruling. RUF022 sorts `__all__`; RUF006 stores `create_task` results; RUF005 wants `[*a, *b]` over list concatenation; RUF100 rejects an unused `noqa` (`do_POST` in a `BaseHTTPRequestHandler` needs none). UP037: no quoted annotations. The formatter writes `except A, B:` without parentheses only when there is no `as` clause (PEP 758); `except (OSError, ValueError) as exc:` keeps its parentheses. Syntax-check Python fences with `uv run python`, never the system `python3`.
- Commit messages: conventional prefix plus the attribution trailer the harness requires as the last lines (blank line before them). Before every commit: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`; before every push also `uv run pre-commit run --all-files`.
- Tests hermetic: `FakeGitHub`, `tests/fakes/claude`, `tests/fakes/gh`, `tmp_path`; a `ThreadingHTTPServer` bound to `127.0.0.1:0` is the only socket a test opens (loopback, no network); a fake poster and a fake `sleep` drive the sink. Tests that spawn the fakes or send signals are `skipif(sys.platform == "win32")`. `ThreadingHTTPServer.shutdown()` blocks forever when `serve_forever` never ran; the test helper guards it with `thread.is_alive()`. Run long test commands under `timeout`.
- Frozen inputs, used as they are: `issuebot.orchestrator` except the Task 1 edit to `handle_worker_exit`, `issuebot.agent`, `issuebot.github`, `EventBus`/`EventSink`, `EVENT_KINDS` (`NotificationSent` exists; no kind is added), `SlackSettings` (no field is added; queue, retry and timeout knobs are constants in `slack.py`).
- Package rules: `issuebot.notifications` imports `config`, `events` and `log` only; `orchestrator` and `agent` never import it; `cli` wires it. `messages.py` has no I/O. The sink does no I/O in `handle`.
- Secrets: the webhook URL is never logged or printed; every error string leaving the transport passes through `redact`; `validate` lines describe the URL's shape, never its value; tests assert `"secret" not in` the output or log stream.
- A live worker must not run under the Bash tool's `run_in_background` (the harness kills that shell after a few minutes); start it detached with `setsid nohup ... >> log 2>&1 < /dev/null &` and record the python pid with `pgrep`. A `Monitor` on the log does not wake an idle session; wait with `run_in_background` and a bounded `timeout N bash -c 'until grep -q ...; do sleep 10; done'`.
- The live-check task runs against jleavers/issuebot-scratch (issue #3 in `review`, PR #4 open; host dirs `~/issuebot-scratch` and `~/issuebot-workspaces`) with `GH_TOKEN` and `SLACK_WEBHOOK_URL` exported in the same command from `gh auth token` and a file the operator writes (`~/issuebot-scratch/slack-webhook`, mode 600), spends real Claude budget under the operator's subscription login (about $0.60 per run; no `ANTHROPIC_API_KEY`), and never prints either value. The operator, not the executor, merges the scratch PR the check needs merged.
- The fake `claude` reads `CLAUDE_FAKE_*` only; in orchestrator tests a worker exit needs several loop turns to become visible (the harness's `drain()` loops `sleep(0)` ten times).

---

## File map

| Path | Responsibility | Task |
|---|---|---|
| `src/issuebot/orchestrator/orchestrator.py` | `handle_worker_exit` publishes the final transition before any release row | 1 |
| `tests/test_orchestrator.py` | success during `shutdown()` publishes `state_changed(agent)` and `pr_opened` | 1 |
| `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md` | §6.8 "amended by Phase 5" note (Task 1); the prose pass (Task 5) | 1, 5 |
| `src/issuebot/notifications/__init__.py` | re-exports (messages in Task 2, completed in Task 3) | 2, 3 |
| `src/issuebot/notifications/messages.py` | `issue_link`, `pr_link`, `format_duration`, `format_event` | 2 |
| `tests/test_notifications_messages.py` | one exact line per kind, link and emoji rules | 2 |
| `src/issuebot/notifications/slack.py` | constants, `PostResult`, `Poster`, `slack_payload`, `redact`, `subscribed_kinds`, `urllib_post`, `SlackSink` | 3 |
| `tests/test_notifications_slack.py` | transport against a loopback `http.server`; sink with a fake poster and fake sleep | 3 |
| `src/issuebot/cli.py` | `_slack_post` seam, `_slack_check`, `--slack-probe`, `_slack_sink`, `_build_bus`, `_claim_and_run`, sink lifetime in `run-once` and `worker` | 4 |
| `tests/test_cli.py` | six `notifications.slack` states, the probe, `run-once` and `worker` wiring, updated warning counts | 4 |
| `CLAUDE.md`, `README.md`, roadmap, Phase 4 spec, dot-env example | documentation | 5 |
| (scratch repository, Slack test channel) | live check | 6 |

---

### Task 1: Publish the final transition before releasing a stopped worker (Phase 4 amendment)

**Files:**
- Modify: `src/issuebot/orchestrator/orchestrator.py` (`handle_worker_exit`; new `_publish_final_transition`)
- Modify: `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md` (§6.8 note)
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: the Phase 4 `Orchestrator`, `RunResult.final_issue`, `observe_transition`.
- Produces: no signature change. Behaviour: when a worker task returned a `succeeded` result whose `final_issue` is set and open, `handle_worker_exit` publishes `observe_transition(entry.issue, final_issue)` right after the `terminal_issue` row and before the release rows, whatever the stop cause; the continuation row no longer publishes it itself.

Spec: §7 (the amendment), Phase 4 spec §6.8.

- [ ] **Step 1: Write the failing test**

In `tests/test_orchestrator.py`, insert immediately before `async def test_run_propagates_startup_errors(tmp_path: Path) -> None:` (the "loop" section, after `test_shutdown_cancels_stragglers_after_the_timeout`):

```python
async def test_success_during_shutdown_publishes_the_agent_transition(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")
    await h.tick()
    h.github.open_pr(1, pr_number=2)
    h.github.human_set_state(1, StateLabel.REVIEW)
    h.run_for(1).finish(final_issue=h.github.issue(1))
    await h.orchestrator.shutdown()
    assert h.orchestrator.running == {}
    assert h.orchestrator.retries == {}
    assert h.recorder.kinds == ["state_changed", "state_changed", "pr_opened"]
    agent_move = h.recorder.of(StateChanged)[1]
    assert (agent_move.actor, agent_move.to_label) == ("agent", "issuebot/review")
    assert agent_move.pr_url == "https://github.com/example/repo/pull/2"
    assert h.recorder.of(PrOpened)[0].pr_number == 2
    assert h.orchestrator.snapshot().counters.runs_ended == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py -q -k success_during_shutdown`
Expected: 1 failed, on `assert h.recorder.kinds == ["state_changed", "state_changed", "pr_opened"]` with `AssertionError: assert ['state_changed'] == [...]` (the release row returns before the transition is published).

- [ ] **Step 3: Hoist the publish above the release rows**

In `src/issuebot/orchestrator/orchestrator.py`, `handle_worker_exit`, use the Edit tool. Replace

```python
        if entry.terminal_issue is not None:
            await self._finish(entry.terminal_issue)
            return
        if task.cancelled() or entry.stop_cause in ("moved", "missing", "shutdown", "closed"):
```

with

```python
        if entry.terminal_issue is not None:
            await self._finish(entry.terminal_issue)
            return
        if result is not None and result.outcome == "succeeded":
            self._publish_final_transition(entry, result)
        if task.cancelled() or entry.stop_cause in ("moved", "missing", "shutdown", "closed"):
```

and replace

```python
                await self._escape(entry, reason, result)
                return
            final = result.final_issue
            if final is not None and final.github_state == "open":
                for event in observe_transition(entry.issue, final):
                    self._bus.publish(event)
            self._schedule(
                entry.issue,
                attempt=1,
                kind="continuation",
                delay_ms=CONTINUATION_DELAY_MS,
                error=None,
            )
            return
        await self._after_failure(entry, f"{result.error_category}: {result.error}", result)

    def _add_elapsed(self, entry: RunningEntry) -> None:
```

with

```python
                await self._escape(entry, reason, result)
                return
            self._schedule(
                entry.issue,
                attempt=1,
                kind="continuation",
                delay_ms=CONTINUATION_DELAY_MS,
                error=None,
            )
            return
        await self._after_failure(entry, f"{result.error_category}: {result.error}", result)

    def _publish_final_transition(self, entry: RunningEntry, result: RunResult) -> None:
        """What changed between the entry's snapshot and the session's last refresh (§4.2).

        Published before the release rows so a move that lands while the worker is being
        stopped (shutdown, or a human move seen by reconcile) still reaches the bus.
        """
        final = result.final_issue
        if final is not None and final.github_state == "open":
            for event in observe_transition(entry.issue, final):
                self._bus.publish(event)

    def _add_elapsed(self, entry: RunningEntry) -> None:
```

- [ ] **Step 4: Note the amendment in the Phase 4 spec**

In `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md`, §6.8, insert a paragraph between the paragraph ending `neither running nor retrying; the next tick treats it like any other.` and the one beginning `The continuation retry after a normal exit is Symphony §7.1's re-check`:

```markdown
*Amended by Phase 5 (spec §7):* a `succeeded` result whose `final_issue` is
set and open publishes `observe_transition(entry.issue, final_issue)` right
after the `terminal_issue` row and before every release row, so an agent's
move to `review` that lands while the worker is being stopped (shutdown, or
a human move seen by reconcile) still reaches the bus; the continuation row
no longer publishes it itself, and the `max_turns` escape may be preceded by
a `PrOpened`.
```

(Keep a blank line before and after it.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator.py -q`
Expected: 52 passed (every existing exit test keeps its event list: a `moved` stop finds `entry.issue` already updated by reconcile, so the hoisted publish adds nothing there).

- [ ] **Step 6: Commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/orchestrator/orchestrator.py tests/test_orchestrator.py docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md
git commit -m "fix: publish the agent's transition when a worker succeeds during a stop"
```

Expected before the commit: 502 passed.

---

### Task 2: Message text (`messages.py`)

**Files:**
- Create: `src/issuebot/notifications/__init__.py`, `src/issuebot/notifications/messages.py`
- Test: `tests/test_notifications_messages.py`

**Interfaces:**
- Consumes: `issuebot.config.GitHubLabels`, the `issuebot.events` dataclasses.
- Produces (used by Task 3): `issue_link(repo, event) -> str`, `pr_link(url) -> str`, `format_duration(seconds) -> str`, `format_event(event, *, repo, labels) -> str | None`.

Spec: §4 (the table is the contract; every string below is asserted exactly).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_notifications_messages.py`:

```python
"""Tests for the Slack message text."""

import pytest

from issuebot.config import GitHubLabels
from issuebot.events import (
    Blocked,
    Event,
    IssueCancelled,
    IssueCompleted,
    NotificationSent,
    PrOpened,
    RunEnded,
    RunStarted,
    StateChanged,
)
from issuebot.notifications import format_duration, format_event, issue_link, pr_link

REPO = "example/repo"
LABELS = GitHubLabels()
ISSUE = "<https://github.com/example/repo/issues/42|repo-42>"
PR_URL = "https://github.com/example/repo/pull/7"
PR = f"<{PR_URL}|PR #7>"


def fmt(event: Event, labels: GitHubLabels = LABELS) -> str | None:
    return format_event(event, repo=REPO, labels=labels)


def state_changed(**overrides: object) -> StateChanged:
    fields: dict[str, object] = {
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "from_label": "issuebot/todo",
        "to_label": "issuebot/in-progress",
        "actor": "issuebot",
    }
    fields.update(overrides)
    return StateChanged(**fields)  # type: ignore[arg-type]


def run_ended(**overrides: object) -> RunEnded:
    fields: dict[str, object] = {
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "run_id": "run-1",
        "outcome": "succeeded",
        "error": None,
        "turns": 2,
        "input_tokens": 100,
        "output_tokens": 10,
        "cost_usd": 0.314,
        "duration_s": 102.7,
    }
    fields.update(overrides)
    return RunEnded(**fields)  # type: ignore[arg-type]


# --- links and helpers ---------------------------------------------------------------


def test_issue_link_uses_the_repo_and_the_identifier() -> None:
    assert issue_link(REPO, state_changed()) == ISSUE


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (PR_URL, PR),
        (PR_URL + "/", f"<{PR_URL}/|PR #7>"),
        (
            "https://github.com/example/repo/pull/7/files",
            "<https://github.com/example/repo/pull/7/files|pull request>",
        ),
        ("https://example.test/changes/9", "<https://example.test/changes/9|pull request>"),
    ],
)
def test_pr_link_labels_numeric_tails(url: str, expected: str) -> None:
    assert pr_link(url) == expected


@pytest.mark.parametrize(("seconds", "text"), [(196.7, "3m16s"), (5, "0m05s"), (0, "0m00s")])
def test_format_duration(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text


# --- state_changed ---------------------------------------------------------------------


def test_claim_by_issuebot() -> None:
    assert fmt(state_changed()) == (
        f":hammer_and_wrench: {ISSUE} `issuebot/todo` → `issuebot/in-progress` by issuebot"
    )


def test_agent_move_to_review_links_the_pull_request() -> None:
    event = state_changed(
        from_label="issuebot/in-progress",
        to_label="issuebot/review",
        actor="agent",
        pr_url=PR_URL,
    )
    assert fmt(event) == (
        f":eyes: {ISSUE} `issuebot/in-progress` → `issuebot/review` by the agent · {PR}"
    )


def test_human_move_to_rework() -> None:
    event = state_changed(from_label="issuebot/review", to_label="issuebot/rework", actor="human")
    assert fmt(event) == f":repeat: {ISSUE} `issuebot/review` → `issuebot/rework` by a human"


def test_labels_stripped_reads_no_label() -> None:
    event = state_changed(from_label="issuebot/review", to_label=None)
    assert fmt(event) == f":label: {ISSUE} `issuebot/review` → no label by issuebot"


@pytest.mark.parametrize(
    ("role", "emoji"),
    [
        ("todo", ":inbox_tray:"),
        ("in_progress", ":hammer_and_wrench:"),
        ("review", ":eyes:"),
        ("rework", ":repeat:"),
        ("complete", ":white_check_mark:"),
    ],
)
def test_emoji_follows_the_target_role(role: str, emoji: str) -> None:
    text = fmt(state_changed(to_label=getattr(LABELS, role)))
    assert text is not None
    assert text.startswith(f"{emoji} ")


def test_emoji_matches_configured_labels_case_insensitively() -> None:
    labels = GitHubLabels(review="Bot: Review")
    text = fmt(state_changed(to_label="bot: review"), labels)
    assert text is not None
    assert text.startswith(":eyes: ")


def test_unknown_label_gets_the_generic_emoji() -> None:
    text = fmt(state_changed(to_label="wontfix"))
    assert text == f":label: {ISSUE} `issuebot/todo` → `wontfix` by issuebot"


# --- the other kinds -------------------------------------------------------------------


def test_blocked() -> None:
    event = Blocked(issue_number=42, issue_identifier="repo-42", reason="Turn budget exhausted.")
    assert fmt(event) == f":no_entry: {ISSUE} blocked: Turn budget exhausted."


def test_run_started() -> None:
    event = RunStarted(
        issue_number=42,
        issue_identifier="repo-42",
        run_id="run-1",
        attempt=2,
        session_id=None,
        workspace_path="/workspaces/repo-42",
    )
    assert fmt(event) == f":rocket: {ISSUE} run started (attempt 2)"


def test_run_ended_succeeded() -> None:
    assert fmt(run_ended()) == f":white_check_mark: {ISSUE} run succeeded: 2 turns, 1m42s, $0.31"


def test_run_ended_single_turn_is_singular() -> None:
    assert (
        fmt(run_ended(turns=1)) == f":white_check_mark: {ISSUE} run succeeded: 1 turn, 1m42s, $0.31"
    )


def test_run_ended_failed_with_error() -> None:
    event = run_ended(outcome="failed", error="process_exit: claude exited 1")
    assert (
        fmt(event)
        == f":x: {ISSUE} run failed: process_exit: claude exited 1 (2 turns, 1m42s, $0.31)"
    )


def test_run_ended_timed_out_without_error() -> None:
    event = run_ended(outcome="timed_out", turns=1)
    assert fmt(event) == f":x: {ISSUE} run timed out (1 turn, 1m42s, $0.31)"


def test_pr_opened() -> None:
    event = PrOpened(issue_number=42, issue_identifier="repo-42", pr_number=7, pr_url=PR_URL)
    assert fmt(event) == f":link: {ISSUE} opened {PR}"


def test_issue_completed_with_and_without_a_pull_request() -> None:
    with_pr = IssueCompleted(issue_number=42, issue_identifier="repo-42", pr_url=PR_URL)
    without = IssueCompleted(issue_number=42, issue_identifier="repo-42", pr_url=None)
    assert fmt(with_pr) == f":tada: {ISSUE} complete · {PR} merged"
    assert fmt(without) == f":tada: {ISSUE} complete"


def test_issue_cancelled() -> None:
    event = IssueCancelled(
        issue_number=42, issue_identifier="repo-42", reason="closed without a merged pull request"
    )
    assert fmt(event) == f":wastebasket: {ISSUE} cancelled: closed without a merged pull request"


def test_notification_sent_and_bare_events_are_not_formatted() -> None:
    sent = NotificationSent(
        issue_number=42, issue_identifier="repo-42", channel="slack", about_kind="blocked"
    )
    assert fmt(sent) is None
    assert fmt(Event()) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_notifications_messages.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'issuebot.notifications'`.

- [ ] **Step 3: Create the package and the module**

Create `src/issuebot/notifications/__init__.py` (Task 3 replaces it with the full re-export list):

```python
"""Notification sinks: Slack incoming webhook (Phase 5)."""

from issuebot.notifications.messages import format_duration, format_event, issue_link, pr_link

__all__ = ["format_duration", "format_event", "issue_link", "pr_link"]
```

Create `src/issuebot/notifications/messages.py`:

```python
"""Slack message text for issuebot events: one line of mrkdwn per notifiable kind."""

import re

from issuebot.config import GitHubLabels
from issuebot.events import (
    Blocked,
    Event,
    IssueCancelled,
    IssueCompleted,
    IssueEvent,
    PrOpened,
    RunEnded,
    RunStarted,
    StateChanged,
)

_PR_TAIL = re.compile(r"/pull/(\d+)/?$")
_ACTORS = {"issuebot": "by issuebot", "agent": "by the agent", "human": "by a human"}
_ROLE_EMOJI = {
    "todo": ":inbox_tray:",
    "in_progress": ":hammer_and_wrench:",
    "review": ":eyes:",
    "rework": ":repeat:",
    "complete": ":white_check_mark:",
}
_OTHER_LABEL_EMOJI = ":label:"
_OUTCOME_WORDS = {"timed_out": "timed out"}


def issue_link(repo: str, event: IssueEvent) -> str:
    """``<https://github.com/{repo}/issues/{n}|{identifier}>``."""
    return f"<https://github.com/{repo}/issues/{event.issue_number}|{event.issue_identifier}>"


def pr_link(url: str) -> str:
    """``<url|PR #n>`` when the URL ends in ``/pull/<digits>``, else ``<url|pull request>``."""
    match = _PR_TAIL.search(url)
    label = f"PR #{match.group(1)}" if match else "pull request"
    return f"<{url}|{label}>"


def format_duration(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}m{total % 60:02d}s"


def format_event(event: Event, *, repo: str, labels: GitHubLabels) -> str | None:
    """One line of Slack mrkdwn for the seven notifiable kinds; None for anything else."""
    if not isinstance(event, IssueEvent):
        return None
    issue = issue_link(repo, event)
    match event:
        case StateChanged():
            return _state_changed(event, issue, labels)
        case Blocked():
            return f":no_entry: {issue} blocked: {event.reason}"
        case RunStarted():
            return f":rocket: {issue} run started (attempt {event.attempt})"
        case RunEnded():
            return _run_ended(event, issue)
        case PrOpened():
            return f":link: {issue} opened {pr_link(event.pr_url)}"
        case IssueCompleted():
            merged = f" · {pr_link(event.pr_url)} merged" if event.pr_url else ""
            return f":tada: {issue} complete{merged}"
        case IssueCancelled():
            return f":wastebasket: {issue} cancelled: {event.reason}"
    return None


def _state_changed(event: StateChanged, issue: str, labels: GitHubLabels) -> str:
    emoji = _emoji_for(event.to_label, labels)
    move = f"{_label(event.from_label)} → {_label(event.to_label)}"
    text = f"{emoji} {issue} {move} {_ACTORS[event.actor]}"
    if event.pr_url:
        text += f" · {pr_link(event.pr_url)}"
    return text


def _label(name: str | None) -> str:
    return f"`{name}`" if name else "no label"


def _emoji_for(name: str | None, labels: GitHubLabels) -> str:
    if name is None:
        return _OTHER_LABEL_EMOJI
    lowered = name.lower()
    for role, emoji in _ROLE_EMOJI.items():
        if getattr(labels, role).lower() == lowered:
            return emoji
    return _OTHER_LABEL_EMOJI


def _run_ended(event: RunEnded, issue: str) -> str:
    turns = f"{event.turns} turn" if event.turns == 1 else f"{event.turns} turns"
    stats = f"{turns}, {format_duration(event.duration_s)}, ${event.cost_usd:.2f}"
    if event.outcome == "succeeded":
        return f":white_check_mark: {issue} run succeeded: {stats}"
    word = _OUTCOME_WORDS.get(event.outcome, event.outcome)
    detail = f": {event.error}" if event.error else ""
    return f":x: {issue} run {word}{detail} ({stats})"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_notifications_messages.py -q`
Expected: 29 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/notifications tests/test_notifications_messages.py
git commit -m "feat: add Slack message text for every event kind"
```

Expected before the commit: 531 passed.

---

### Task 3: Transport and sink (`slack.py`)

**Files:**
- Create: `src/issuebot/notifications/slack.py`
- Modify: `src/issuebot/notifications/__init__.py` (full re-export list)
- Test: `tests/test_notifications_slack.py`

**Interfaces:**
- Consumes: `SlackSettings` (`webhook_url: SecretStr | None`, `events: list[str]`), `GitHubLabels`, `EventBus`, `IssueEvent`, `NotificationSent`, `format_event`.
- Produces (used by Task 4): the constants `QUEUE_LIMIT = 100`, `MAX_ATTEMPTS = 3`, `POST_TIMEOUT_S = 10.0`, `RETRY_DELAYS_S = (1.0, 4.0)`, `RETRY_AFTER_CAP_S = 30.0`, `DRAIN_TIMEOUT_S = 10.0`, `REDACTED`; `PostResult(status, retry_after_s, error)` with `ok` and `retryable`; the `Poster` protocol `async (url, payload, *, timeout_s) -> PostResult`; `slack_payload(text) -> bytes`; `redact(text, url) -> str`; `subscribed_kinds(slack) -> frozenset[str]`; `urllib_post` (never raises; http and https only); `SlackSink(slack, *, repo, labels, post=urllib_post, sleep=asyncio.sleep)` with `name = "slack"`, `kinds`, `sent`, `failed`, `dropped`, `handle(event)`, `start(bus)`, `async close()`.

Spec: §3 (delivery model) and §5 (signatures).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_notifications_slack.py`:

```python
"""Tests for the Slack transport (against a local HTTP server) and the sink (with a fake poster)."""

import asyncio
import io
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from issuebot.config import GitHubLabels, SlackSettings
from issuebot.events import Blocked, Event, EventBus, NotificationSent, RunStarted, StateChanged
from issuebot.log import configure_logging
from issuebot.notifications import (
    PostResult,
    SlackSink,
    redact,
    slack_payload,
    subscribed_kinds,
    urllib_post,
)
from issuebot.notifications import slack as slack_module

URL = "https://hooks.slack.com/services/T000/B000/secret"
PATH = "/services/T000/B000/secret"


# --- a local HTTP server -----------------------------------------------------------------


@dataclass
class Received:
    path: str
    content_type: str | None
    body: bytes


class ScriptedServer:
    """Answers each POST from a script of (status, headers) pairs; 200 once the script is empty."""

    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, str]]] = []
        self.received: list[Received] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                server.received.append(Received(self.path, self.headers.get("Content-Type"), body))
                status, headers = server.script.pop(0) if server.script else (200, {})
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args: object) -> None:
                pass

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._http.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self._http.server_address[:2]
        return f"http://{host}:{port}{PATH}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if self._thread.is_alive():
            self._http.shutdown()
        self._http.server_close()


@pytest.fixture
def server() -> Iterator[ScriptedServer]:
    scripted = ScriptedServer()
    scripted.start()
    yield scripted
    scripted.stop()


# --- transport -----------------------------------------------------------------------------


async def test_urllib_post_delivers_json(server: ScriptedServer) -> None:
    result = await urllib_post(server.url, slack_payload("hello"), timeout_s=5)
    assert result == PostResult(status=200)
    assert result.ok
    (request,) = server.received
    assert request.path == PATH
    assert request.content_type == "application/json; charset=utf-8"
    assert json.loads(request.body) == {"text": "hello"}


async def test_urllib_post_reads_a_numeric_retry_after(server: ScriptedServer) -> None:
    server.script = [(429, {"Retry-After": "2"})]
    result = await urllib_post(server.url, slack_payload("x"), timeout_s=5)
    assert (result.status, result.retry_after_s, result.retryable) == (429, 2.0, True)
    assert result.error == "HTTP Error 429: Too Many Requests"


async def test_urllib_post_ignores_a_non_numeric_retry_after(server: ScriptedServer) -> None:
    server.script = [(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})]
    result = await urllib_post(server.url, slack_payload("x"), timeout_s=5)
    assert (result.status, result.retry_after_s) == (429, None)


async def test_urllib_post_reports_server_errors(server: ScriptedServer) -> None:
    server.script = [(500, {})]
    result = await urllib_post(server.url, slack_payload("x"), timeout_s=5)
    assert (result.status, result.retryable, result.ok) == (500, True, False)


async def test_urllib_post_reports_a_closed_port_without_raising() -> None:
    scripted = ScriptedServer()
    url = scripted.url
    scripted.stop()
    result = await urllib_post(url, slack_payload("x"), timeout_s=5)
    assert result.status is None
    assert result.retryable
    assert result.error is not None
    assert "secret" not in result.error


async def test_urllib_post_refuses_other_schemes() -> None:
    result = await urllib_post("ftp://hooks.slack.com/services/secret", slack_payload("x"))
    assert result == PostResult(status=None, error="unsupported URL scheme 'ftp'")


# --- helpers ---------------------------------------------------------------------------


def test_redact_strips_the_url_and_its_path() -> None:
    text = f"failed for {URL} and again for {PATH}; status 500"
    assert redact(text, URL) == "failed for <webhook url> and again for <webhook url>; status 500"
    assert redact("nothing to hide", URL) == "nothing to hide"
    assert redact("root path only", "https://h/") == "root path only"


@pytest.mark.parametrize(
    ("status", "ok", "retryable"),
    [
        (200, True, False),
        (204, True, False),
        (400, False, False),
        (404, False, False),
        (429, False, True),
        (500, False, True),
        (503, False, True),
        (None, False, True),
    ],
)
def test_post_result_flags(status: int | None, ok: bool, retryable: bool) -> None:
    result = PostResult(status=status)
    assert (result.ok, result.retryable) == (ok, retryable)


def test_subscribed_kinds_drops_notification_sent() -> None:
    settings = SlackSettings(events=["notification_sent", "blocked", "state_changed"])
    assert subscribed_kinds(settings) == frozenset({"blocked", "state_changed"})
    assert subscribed_kinds(SlackSettings(events=[])) == frozenset()


# --- the sink --------------------------------------------------------------------------


class FakePoster:
    def __init__(self, *results: PostResult) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []
        self.block: asyncio.Event | None = None
        self.raise_first = False

    async def __call__(self, url: str, payload: bytes, *, timeout_s: float) -> PostResult:
        self.calls.append({"url": url, "text": json.loads(payload)["text"], "timeout_s": timeout_s})
        if self.raise_first:
            self.raise_first = False
            raise RuntimeError("poster bug")
        if self.block is not None:
            await self.block.wait()
        return self.results.pop(0) if self.results else PostResult(status=200)

    @property
    def texts(self) -> list[str]:
        return [call["text"] for call in self.calls]


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(0)


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


@dataclass
class Rig:
    sink: SlackSink
    poster: FakePoster
    sleep: FakeSleep
    recorder: Recorder
    bus: EventBus
    stream: io.StringIO

    @property
    def log_lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]

    def logged(self, event: str) -> list[dict[str, Any]]:
        return [line for line in self.log_lines if line["event"] == event]


def make_rig(*results: PostResult, events: list[str] | None = None) -> Rig:
    stream = io.StringIO()
    configure_logging(fmt="json", level="DEBUG", stream=stream)  # type: ignore[arg-type]
    settings = SlackSettings(
        webhook_url=URL,  # type: ignore[arg-type]
        events=["state_changed", "blocked"] if events is None else events,
    )
    poster, sleep, recorder = FakePoster(*results), FakeSleep(), Recorder()
    sink = SlackSink(settings, repo="example/repo", labels=GitHubLabels(), post=poster, sleep=sleep)
    bus = EventBus([recorder, sink])
    return Rig(sink, poster, sleep, recorder, bus, stream)


def blocked(number: int = 42) -> Blocked:
    return Blocked(issue_number=number, issue_identifier=f"repo-{number}", reason="budget")


def claim(number: int = 42) -> StateChanged:
    return StateChanged(
        issue_number=number,
        issue_identifier=f"repo-{number}",
        from_label="issuebot/todo",
        to_label="issuebot/in-progress",
        actor="issuebot",
    )


async def settle(turns: int = 40) -> None:
    """Let the drain task run: each post and each fake sleep costs a loop turn."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def test_handle_filters_by_kind_and_formats() -> None:
    rig = make_rig(events=["state_changed"])
    rig.bus.publish(claim())
    rig.bus.publish(blocked())
    rig.bus.publish(
        NotificationSent(
            issue_number=42, issue_identifier="repo-42", channel="slack", about_kind="blocked"
        )
    )
    rig.bus.publish(Event())
    rig.sink.start(rig.bus)
    await rig.sink.close()
    assert rig.poster.texts == [
        ":hammer_and_wrench: <https://github.com/example/repo/issues/42|repo-42> "
        "`issuebot/todo` → `issuebot/in-progress` by issuebot"
    ]
    assert rig.poster.calls[0]["url"] == URL
    assert rig.poster.calls[0]["timeout_s"] == slack_module.POST_TIMEOUT_S


async def test_notification_sent_is_never_subscribed() -> None:
    rig = make_rig(events=["notification_sent", "blocked"])
    assert rig.sink.kinds == frozenset({"blocked"})
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await settle()
    assert len(rig.poster.calls) == 1
    assert rig.recorder.kinds == ["blocked", "notification_sent"]
    await settle()
    assert len(rig.poster.calls) == 1
    await rig.sink.close()


async def test_delivery_publishes_notification_sent_about_the_kind() -> None:
    rig = make_rig()
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await settle()
    sent = rig.recorder.events[-1]
    assert isinstance(sent, NotificationSent)
    assert (sent.issue_number, sent.issue_identifier) == (42, "repo-42")
    assert (sent.channel, sent.about_kind) == ("slack", "blocked")
    assert (rig.sink.sent, rig.sink.failed, rig.sink.dropped) == (1, 0, 0)
    (line,) = rig.logged("slack_notification_sent")
    assert (line["kind"], line["issue_number"], line["attempt"]) == ("blocked", 42, 1)
    await rig.sink.close()
    assert "secret" not in rig.stream.getvalue()


async def test_events_before_start_are_buffered_and_delivered_in_order() -> None:
    rig = make_rig()
    rig.bus.publish(claim(1))
    rig.bus.publish(blocked(2))
    rig.bus.publish(claim(3))
    assert rig.poster.calls == []
    rig.sink.start(rig.bus)
    await rig.sink.close()
    assert [text.split("|")[1].split(">")[0] for text in rig.poster.texts] == [
        "repo-1",
        "repo-2",
        "repo-3",
    ]
    assert rig.sink.sent == 3
    (closed,) = rig.logged("slack_sink_closed")
    assert (closed["sent"], closed["failed"], closed["dropped"]) == (3, 0, 0)


async def test_429_waits_for_retry_after_capped() -> None:
    rig = make_rig(
        PostResult(status=429, retry_after_s=2.0),
        PostResult(status=429, retry_after_s=90.0),
        PostResult(status=200),
    )
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await rig.sink.close()
    assert rig.sleep.delays == [2.0, slack_module.RETRY_AFTER_CAP_S]
    assert len(rig.poster.calls) == 3
    assert (rig.sink.sent, rig.sink.failed) == (1, 0)
    retries = rig.logged("slack_post_retry")
    assert [(line["attempt"], line["status"], line["delay_s"]) for line in retries] == [
        (1, 429, 2.0),
        (2, 429, 30.0),
    ]


async def test_429_without_retry_after_uses_the_backoff() -> None:
    rig = make_rig(PostResult(status=429), PostResult(status=200))
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await rig.sink.close()
    assert rig.sleep.delays == [1.0]
    assert rig.sink.sent == 1


async def test_server_and_network_errors_back_off_then_give_up() -> None:
    rig = make_rig(
        PostResult(status=500, error="HTTP Error 500: Internal Server Error"),
        PostResult(status=None, error="ConnectionRefusedError: refused"),
        PostResult(status=503, error="HTTP Error 503: Service Unavailable"),
    )
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await rig.sink.close()
    assert rig.sleep.delays == list(slack_module.RETRY_DELAYS_S)
    assert len(rig.poster.calls) == 3
    assert (rig.sink.sent, rig.sink.failed) == (0, 1)
    assert rig.recorder.kinds == ["blocked"]
    (failed,) = rig.logged("slack_notification_failed")
    assert (failed["level"], failed["attempts"], failed["status"]) == ("warning", 3, 503)
    assert failed["error"] == "HTTP Error 503: Service Unavailable"


async def test_other_4xx_is_permanent() -> None:
    rig = make_rig(PostResult(status=400, error="HTTP Error 400: Bad Request"))
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await rig.sink.close()
    assert rig.sleep.delays == []
    assert len(rig.poster.calls) == 1
    assert (rig.sink.sent, rig.sink.failed) == (0, 1)
    (failed,) = rig.logged("slack_notification_failed")
    assert (failed["attempts"], failed["status"]) == (1, 400)


async def test_full_queue_drops_and_keeps_delivering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_module, "QUEUE_LIMIT", 2)
    rig = make_rig()
    for number in (1, 2, 3):
        rig.bus.publish(claim(number))
    assert rig.sink.dropped == 1
    (line,) = rig.logged("slack_queue_full")
    assert (line["level"], line["issue_number"], line["limit"]) == ("warning", 3, 2)
    rig.sink.start(rig.bus)
    await rig.sink.close()
    assert len(rig.poster.calls) == 2
    assert (rig.sink.sent, rig.sink.dropped) == (2, 1)


async def test_close_drains_then_drops_later_events() -> None:
    rig = make_rig()
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked())
    await rig.sink.close()
    assert rig.sink._task is not None and rig.sink._task.done()
    assert len(rig.poster.calls) == 1
    rig.bus.publish(blocked())
    await settle()
    assert len(rig.poster.calls) == 1
    (dropped,) = rig.logged("slack_sink_closed_drop")
    assert dropped["level"] == "debug"


async def test_close_times_out_on_a_hanging_poster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_module, "DRAIN_TIMEOUT_S", 0.05)
    rig = make_rig()
    rig.poster.block = asyncio.Event()
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked(1))
    rig.bus.publish(blocked(2))
    await settle()
    await rig.sink.close()
    assert rig.sink._task is not None and rig.sink._task.cancelled()
    (timeout,) = rig.logged("slack_drain_timeout")
    assert (timeout["level"], timeout["left"]) == ("warning", 1)
    assert rig.logged("slack_sink_closed")


async def test_start_twice_raises_and_close_before_start_is_a_noop() -> None:
    rig = make_rig()
    await rig.sink.close()
    assert rig.logged("slack_sink_closed") == []
    rig.sink.start(rig.bus)
    with pytest.raises(RuntimeError, match="already started"):
        rig.sink.start(rig.bus)
    await rig.sink.close()


async def test_poster_exception_is_logged_and_the_loop_continues() -> None:
    rig = make_rig()
    rig.poster.raise_first = True
    rig.sink.start(rig.bus)
    rig.bus.publish(blocked(1))
    rig.bus.publish(blocked(2))
    await rig.sink.close()
    assert len(rig.poster.calls) == 2
    assert (rig.sink.sent, rig.sink.failed) == (1, 1)
    (crashed,) = rig.logged("slack_deliver_crashed")
    assert (crashed["level"], crashed["issue_number"]) == ("error", 1)
    assert "poster bug" in crashed["exception"]


def test_sink_requires_a_webhook() -> None:
    with pytest.raises(ValueError, match="webhook_url"):
        SlackSink(SlackSettings(), repo="example/repo", labels=GitHubLabels())


async def test_sink_posts_to_a_local_server(server: ScriptedServer) -> None:
    settings = SlackSettings(webhook_url=server.url)  # type: ignore[arg-type]
    sink = SlackSink(settings, repo="example/repo", labels=GitHubLabels())
    bus = EventBus([sink])
    sink.start(bus)
    bus.publish(
        RunStarted(
            issue_number=42,
            issue_identifier="repo-42",
            run_id="run-1",
            attempt=1,
            session_id=None,
            workspace_path="/w",
        )
    )
    bus.publish(claim())
    await sink.close()
    assert sink.sent == 1
    (request,) = server.received
    assert json.loads(request.body)["text"].startswith(":hammer_and_wrench: ")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_notifications_slack.py -q`
Expected: collection error, `ImportError: cannot import name 'PostResult' from 'issuebot.notifications'`.

- [ ] **Step 3: Write the module and complete the package re-exports**

Create `src/issuebot/notifications/slack.py`:

```python
"""Slack incoming-webhook sink: a queue drained by one task, bounded retry, redacted errors."""

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from issuebot.config import GitHubLabels, SlackSettings
from issuebot.events import Event, EventBus, IssueEvent, NotificationSent
from issuebot.log import get_logger
from issuebot.notifications.messages import format_event

QUEUE_LIMIT = 100
MAX_ATTEMPTS = 3
POST_TIMEOUT_S = 10.0
RETRY_DELAYS_S: tuple[float, ...] = (1.0, 4.0)
RETRY_AFTER_CAP_S = 30.0
DRAIN_TIMEOUT_S = 10.0
REDACTED = "<webhook url>"
_SCHEMES = ("http", "https")


@dataclass(frozen=True, slots=True)
class PostResult:
    """What one webhook POST came back with. ``error`` never contains the webhook URL."""

    status: int | None
    retry_after_s: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500


class Poster(Protocol):
    async def __call__(self, url: str, payload: bytes, *, timeout_s: float) -> PostResult: ...


def slack_payload(text: str) -> bytes:
    return json.dumps({"text": text}).encode("utf-8")


def redact(text: str, url: str) -> str:
    """Replace the webhook URL, and its path on its own, with a placeholder."""
    redacted = text.replace(url, REDACTED)
    path = urlsplit(url).path
    if path and path != "/":
        redacted = redacted.replace(path, REDACTED)
    return redacted


def subscribed_kinds(slack: SlackSettings) -> frozenset[str]:
    """The allow-list minus ``notification_sent``, which the sink never notifies about."""
    return frozenset(slack.events) - {NotificationSent.kind}


async def urllib_post(url: str, payload: bytes, *, timeout_s: float = POST_TIMEOUT_S) -> PostResult:
    """POST ``payload`` as JSON with urllib in a worker thread; never raises."""
    scheme = urlsplit(url).scheme
    if scheme not in _SCHEMES:
        return PostResult(status=None, error=f"unsupported URL scheme {scheme!r}")
    return await asyncio.to_thread(_post_blocking, url, payload, timeout_s)


def _post_blocking(url: str, payload: bytes, timeout_s: float) -> PostResult:
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return PostResult(status=response.status)
    except urllib.error.HTTPError as exc:
        return PostResult(
            status=exc.code,
            retry_after_s=_retry_after(exc.headers.get("Retry-After")),
            error=redact(str(exc), url),
        )
    except (OSError, ValueError) as exc:
        return PostResult(status=None, error=redact(f"{type(exc).__name__}: {exc}", url))


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class _Pending:
    event: IssueEvent
    text: str


class SlackSink:
    """Posts subscribed events to a Slack incoming webhook from one background task.

    ``handle`` only formats and enqueues; ``start`` creates the drain task; ``close``
    drains what is queued (bounded) and stops it. Failures are logged and counted.
    """

    name = "slack"

    def __init__(
        self,
        slack: SlackSettings,
        *,
        repo: str,
        labels: GitHubLabels,
        post: Poster = urllib_post,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if slack.webhook_url is None:
            raise ValueError("SlackSink needs notifications.slack.webhook_url")
        self._url = slack.webhook_url.get_secret_value()
        self.kinds = subscribed_kinds(slack)
        self._repo = repo
        self._labels = labels
        self._post = post
        self._sleep = sleep
        self._queue: asyncio.Queue[_Pending | None] = asyncio.Queue()
        self._bus: EventBus | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self._log = get_logger(__name__)

    def handle(self, event: Event) -> None:
        if not isinstance(event, IssueEvent) or event.kind not in self.kinds:
            return
        if self._closed:
            self._log.debug(
                "slack_sink_closed_drop", kind=event.kind, issue_number=event.issue_number
            )
            return
        text = format_event(event, repo=self._repo, labels=self._labels)
        if text is None:
            return
        if self._queue.qsize() >= QUEUE_LIMIT:
            self.dropped += 1
            self._log.warning(
                "slack_queue_full",
                kind=event.kind,
                issue_number=event.issue_number,
                limit=QUEUE_LIMIT,
            )
            return
        self._queue.put_nowait(_Pending(event, text))

    def start(self, bus: EventBus) -> None:
        """Create the drain task on the running loop; ``NotificationSent`` goes to ``bus``."""
        if self._task is not None:
            raise RuntimeError("SlackSink is already started")
        self._bus = bus
        self._task = asyncio.create_task(self._drain(), name="issuebot-slack-sink")
        self._log.info("slack_sink_started", kinds=sorted(self.kinds))

    async def close(self) -> None:
        """Deliver what is queued for at most DRAIN_TIMEOUT_S, then stop the drain task."""
        if self._task is None:
            return
        self._closed = True
        self._queue.put_nowait(None)
        try:
            await asyncio.wait_for(self._task, DRAIN_TIMEOUT_S)
        except TimeoutError:
            self._log.warning(
                "slack_drain_timeout",
                left=max(self._queue.qsize() - 1, 0),
                timeout_s=DRAIN_TIMEOUT_S,
            )
        self._log.info(
            "slack_sink_closed", sent=self.sent, failed=self.failed, dropped=self.dropped
        )

    async def _drain(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            try:
                await self._deliver(item)
            except Exception:
                self.failed += 1
                self._log.exception(
                    "slack_deliver_crashed",
                    kind=item.event.kind,
                    issue_number=item.event.issue_number,
                )

    async def _deliver(self, item: _Pending) -> None:
        event = item.event
        payload = slack_payload(item.text)
        attempt = 0
        result = PostResult(status=None)
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            result = await self._post(self._url, payload, timeout_s=POST_TIMEOUT_S)
            if result.ok:
                self.sent += 1
                self._log.info(
                    "slack_notification_sent",
                    kind=event.kind,
                    issue_number=event.issue_number,
                    attempt=attempt,
                )
                self._publish_sent(event)
                return
            if not result.retryable or attempt == MAX_ATTEMPTS:
                break
            delay = self._retry_delay(result, attempt)
            self._log.warning(
                "slack_post_retry",
                kind=event.kind,
                issue_number=event.issue_number,
                attempt=attempt,
                status=result.status,
                error=result.error,
                delay_s=delay,
            )
            await self._sleep(delay)
        self.failed += 1
        self._log.warning(
            "slack_notification_failed",
            kind=event.kind,
            issue_number=event.issue_number,
            attempts=attempt,
            status=result.status,
            error=result.error,
        )

    @staticmethod
    def _retry_delay(result: PostResult, attempt: int) -> float:
        delay = RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S)) - 1]
        if result.status == 429 and result.retry_after_s is not None:
            delay = result.retry_after_s
        return min(delay, RETRY_AFTER_CAP_S)

    def _publish_sent(self, event: IssueEvent) -> None:
        if self._bus is None:
            return
        self._bus.publish(
            NotificationSent(
                issue_number=event.issue_number,
                issue_identifier=event.issue_identifier,
                channel=self.name,
                about_kind=event.kind,
            )
        )
```

Replace `src/issuebot/notifications/__init__.py` with:

```python
"""Notification sinks: Slack incoming webhook (Phase 5)."""

from issuebot.notifications.messages import format_duration, format_event, issue_link, pr_link
from issuebot.notifications.slack import (
    DRAIN_TIMEOUT_S,
    MAX_ATTEMPTS,
    POST_TIMEOUT_S,
    QUEUE_LIMIT,
    REDACTED,
    RETRY_AFTER_CAP_S,
    RETRY_DELAYS_S,
    Poster,
    PostResult,
    SlackSink,
    redact,
    slack_payload,
    subscribed_kinds,
    urllib_post,
)

__all__ = [
    "DRAIN_TIMEOUT_S",
    "MAX_ATTEMPTS",
    "POST_TIMEOUT_S",
    "QUEUE_LIMIT",
    "REDACTED",
    "RETRY_AFTER_CAP_S",
    "RETRY_DELAYS_S",
    "PostResult",
    "Poster",
    "SlackSink",
    "format_duration",
    "format_event",
    "issue_link",
    "pr_link",
    "redact",
    "slack_payload",
    "subscribed_kinds",
    "urllib_post",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `timeout 120 uv run pytest tests/test_notifications_slack.py tests/test_notifications_messages.py -q`
Expected: 60 passed, in well under a second (the loopback server's `serve_forever` polls every 50 ms; the sink tests never sleep for real; the drain-timeout test waits 50 ms).

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/notifications tests/test_notifications_slack.py
git commit -m "feat: add the Slack webhook transport and the queued SlackSink"
```

Expected before the commit: 562 passed.

---

### Task 4: CLI wiring: the bus, `run-once`, `worker`, `validate` and `--slack-probe`

**Files:**
- Modify: `src/issuebot/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 3's `SlackSink`, `subscribed_kinds`, `slack_payload`, `urllib_post`, `POST_TIMEOUT_S`; `EventSink` from `issuebot.events`.
- Produces: the module seam `_slack_post = urllib_post` (tests substitute it like `_adapter_factory`); `SLACK_WEBHOOK_HOST`, `SLACK_WEBHOOK_PATH`; `run_checks(workflow, *, adapter=None, slack_probe=False)`; `_slack_check(settings, *, probe)`; `_slack_sink(settings) -> SlackSink | None`; `_build_bus(settings) -> tuple[EventBus, SlackSink | None]`; `_claim_and_run(...)` (the tail of `_run_once`, so the sink can be closed in a `finally`); `validate --slack-probe`.

Spec: §6 (the six-row table is asserted line by line).

- [ ] **Step 1: Write the failing tests**

Use the Edit tool on `tests/test_cli.py` with these exact old and new strings.

Imports: replace

```python
import asyncio
import os
```

with

```python
import asyncio
import json
import os
```

and replace

```python
from issuebot.github import FakeGitHub, GitHubError, Issue, LinkedPr, StateLabel
from issuebot.orchestrator import OrchestratorStartupError
```

with

```python
from issuebot.github import FakeGitHub, GitHubError, Issue, LinkedPr, StateLabel
from issuebot.notifications import PostResult
from issuebot.orchestrator import OrchestratorStartupError
```

After `INVALID = FIXTURES / "invalid.md"` add the line

```python
WEBHOOK = "https://hooks.slack.com/services/T000/B000/secret"
```

Insert immediately before `def _write(tmp_path: Path, text: str) -> Path:`:

```python
class FakeSlackPost:
    """Stands in for urllib_post: records each payload; answers from a script, else 200."""

    def __init__(self) -> None:
        self.results: list[PostResult] = []
        self.calls: list[dict[str, object]] = []

    async def __call__(self, url: str, payload: bytes, *, timeout_s: float) -> PostResult:
        self.calls.append({"url": url, "text": json.loads(payload)["text"]})
        return self.results.pop(0) if self.results else PostResult(status=200)


@pytest.fixture
def slack_post(monkeypatch: pytest.MonkeyPatch) -> FakeSlackPost:
    fake = FakeSlackPost()
    monkeypatch.setattr("issuebot.cli._slack_post", fake)
    return fake
```

The Slack line of the default configuration now warns, which moves five summary counts by one. In `test_validate_good_workflow_exits_zero` replace

```python
    assert "[ OK ] database.url: not configured (history and dashboard disabled)" in out
    assert "[ OK ] notifications.slack: not configured" in out
    assert "[ OK ] prompt: 44 characters, renders" in out
```

with

```python
    assert "[ OK ] database.url: not configured (history and dashboard disabled)" in out
    assert (
        "[WARN] notifications.slack: not configured; export SLACK_WEBHOOK_URL to notify on "
        "blocked, state_changed, or set notifications.slack.events: [] to silence this" in out
    )
    assert "[ OK ] prompt: 44 characters, renders" in out
```

and `assert out.rstrip().endswith("12 checks: 0 failed, 0 warnings")` with `assert out.rstrip().endswith("12 checks: 0 failed, 1 warnings")`. Then, one occurrence each:

| Test | Old assertion | New assertion |
|---|---|---|
| `test_validate_literal_token_warns` | `assert "0 failed, 1 warnings" in out` (the line after the `github.token` assertion) | `assert "0 failed, 2 warnings" in out` |
| `test_validate_missing_executables_fail` | `assert "2 failed, 3 warnings" in out` | `assert "2 failed, 4 warnings" in out` |
| `test_validate_unknown_claude_version_warns` | `assert "0 failed, 1 warnings" in out` (after the `claude.command` assertion) | `assert "0 failed, 2 warnings" in out` |
| `test_validate_warns_about_missing_labels` | `assert "0 failed, 1 warnings" in out` (after the `labels ensure` assertion) | `assert "0 failed, 2 warnings" in out` |

Replace the whole of `test_validate_configured_database_and_slack` (from its `def` line up to, not including, `def test_validate_old_claude_fails(`) with this block, which keeps that test (its `hooks.example` host now warns) and adds seven more:

```python
def test_validate_configured_database_and_slack(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/x")
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] database.url: configured" in out
    assert (
        "[WARN] notifications.slack: configured (blocked, state_changed); the URL is not a "
        "hooks.slack.com/services/ webhook (a compatible endpoint is fine)" in out
    )
    assert "hooks.example" not in out


def test_validate_slack_configured_ok(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] notifications.slack: configured (blocked, state_changed)" in out
    assert "12 checks: 0 failed, 0 warnings" in out
    assert "secret" not in out


def test_validate_slack_empty_events_is_ok_without_a_webhook(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    text = "---\ngithub:\n  repo: o/r\nnotifications:\n  slack:\n    events: []\n---\nBody"
    assert main(["validate", "--workflow", str(_write(tmp_path, text))]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] notifications.slack: not configured (events: [])" in out
    assert "0 failed, 0 warnings" in out


def test_validate_slack_empty_events_with_a_webhook_warns(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    text = "---\ngithub:\n  repo: o/r\nnotifications:\n  slack:\n    events: []\n---\nBody"
    assert main(["validate", "--workflow", str(_write(tmp_path, text))]) == 0
    out = capsys.readouterr().out
    assert "[WARN] notifications.slack: configured but events is empty; nothing will be sent" in out


def test_validate_slack_http_url_fails_without_printing_it(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://hooks.slack.com/services/T0/B0/plain")
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] notifications.slack: webhook_url is not an https URL" in out
    assert "plain" not in out
    assert "1 failed" in out


def test_validate_slack_probe_posts_one_test_message(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    slack_post: FakeSlackPost,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--slack-probe", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert (
        "[ OK ] notifications.slack: configured (blocked, state_changed); test message delivered"
        in out
    )
    assert slack_post.calls == [
        {
            "url": WEBHOOK,
            "text": ":wave: issuebot validate: Slack notifications are configured for "
            "blocked, state_changed (o/r)",
        }
    ]


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (PostResult(status=403, error="HTTP Error 403: Forbidden"), "HTTP 403"),
        (
            PostResult(status=None, error="ConnectionRefusedError: refused"),
            "ConnectionRefusedError: refused",
        ),
    ],
)
def test_validate_slack_probe_reports_a_failed_post(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
    slack_post: FakeSlackPost,
    result: PostResult,
    reason: str,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    slack_post.results = [result]
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--slack-probe", "--workflow", str(path)]) == 1
    out = capsys.readouterr().out
    assert f"[FAIL] notifications.slack: test message not delivered: {reason}" in out
    assert "secret" not in out


def test_validate_slack_probe_is_skipped_when_not_configured(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    slack_post: FakeSlackPost,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--slack-probe", "--workflow", str(GOOD)]) == 0
    assert "[WARN] notifications.slack: not configured;" in capsys.readouterr().out
    assert slack_post.calls == []
```

In the run-once section, insert immediately before the `@pytest.mark.skipif(sys.platform == "win32", reason="the fakes are POSIX shebang scripts")` decorator of `test_run_once_end_to_end_with_the_fakes`:

```python
def test_run_once_posts_the_claim_to_slack(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_github: FakeGitHub,
    stub_session: StubSession,
    slack_post: FakeSlackPost,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    fake_github.add_issue("Add retry backoff", labels=("issuebot/todo",), number=42)
    assert main(["run-once", "42", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    assert "issue #42 is now review" in capsys.readouterr().out
    (call,) = slack_post.calls
    assert call["url"] == WEBHOOK
    assert call["text"] == (
        ":hammer_and_wrench: <https://github.com/example/repo/issues/42|repo-42> "
        "`issuebot/todo` → `issuebot/in-progress` by issuebot"
    )
```

In `StubOrchestrator`, after `next_sigterm: ClassVar[bool] = False` add

```python
    next_event: ClassVar[Event | None] = None
```

and in its `run()` replace

```python
        if StubOrchestrator.next_problems is not None:
            raise OrchestratorStartupError(StubOrchestrator.next_problems)
        if StubOrchestrator.next_sigterm:
```

with

```python
        if StubOrchestrator.next_problems is not None:
            raise OrchestratorStartupError(StubOrchestrator.next_problems)
        if StubOrchestrator.next_event is not None:
            self.kwargs["bus"].publish(StubOrchestrator.next_event)  # type: ignore[attr-defined]
        if StubOrchestrator.next_sigterm:
```

In the `stub_orchestrator` fixture add `StubOrchestrator.next_event = None` after `StubOrchestrator.next_sigterm = False`. Insert immediately before `def test_worker_requires_no_arguments(tmp_path: Path) -> None:`:

```python
def test_worker_wires_the_slack_sink_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_orchestrator: type[StubOrchestrator],
    slack_post: FakeSlackPost,
) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    stub_orchestrator.next_event = StateChanged(
        issue_number=7,
        issue_identifier="repo-7",
        from_label="issuebot/in-progress",
        to_label="issuebot/review",
        actor="agent",
        pr_url="https://github.com/example/repo/pull/8",
    )
    assert main(["worker", "--workflow", str(_workflow_with_root(tmp_path))]) == 0
    instance = stub_orchestrator.instances[0]
    assert [sink.name for sink in instance.kwargs["bus"].sinks] == ["log", "slack"]  # type: ignore[attr-defined]
    (call,) = slack_post.calls
    assert call["text"] == (
        ":eyes: <https://github.com/example/repo/issues/7|repo-7> `issuebot/in-progress` → "
        "`issuebot/review` by the agent · <https://github.com/example/repo/pull/8|PR #8>"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `timeout 120 uv run pytest tests/test_cli.py -q 2>&1 | tail -30`
Expected: the two parametrised probe cases error at setup (`monkeypatch.setattr` finds no `issuebot.cli._slack_post`), and the other new and updated tests fail: the `notifications.slack` lines still read `not configured` or `configured`, the five summary counts are one short, and `--slack-probe` is an unknown flag (`SystemExit: 2`).

- [ ] **Step 3: Wire the CLI**

Use the Edit tool on `src/issuebot/cli.py` with these exact old and new strings.

Imports: replace

```python
from pathlib import Path
from typing import Literal
```

with

```python
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
```

and replace

```python
from issuebot.events import EventBus, LogSink, StateChanged
from issuebot.github import GhCliAdapter, GitHubAdapter, GitHubError, Issue, StateLabel
from issuebot.github.normalise import repo_short_name
from issuebot.log import LOG_LEVELS, configure_logging
from issuebot.orchestrator import Orchestrator, OrchestratorStartupError
```

with

```python
from issuebot.events import EventBus, EventSink, LogSink, StateChanged
from issuebot.github import GhCliAdapter, GitHubAdapter, GitHubError, Issue, StateLabel
from issuebot.github.normalise import repo_short_name
from issuebot.log import LOG_LEVELS, configure_logging
from issuebot.notifications import (
    POST_TIMEOUT_S,
    SlackSink,
    slack_payload,
    subscribed_kinds,
    urllib_post,
)
from issuebot.orchestrator import Orchestrator, OrchestratorStartupError
```

Seams and constants: replace

```python
_claude_version = _claude_version_output
_run_session = run_session
_orchestrator_factory = Orchestrator
```

with

```python
_claude_version = _claude_version_output
_run_session = run_session
_orchestrator_factory = Orchestrator
_slack_post = urllib_post

SLACK_WEBHOOK_HOST = "hooks.slack.com"
SLACK_WEBHOOK_PATH = "/services/"
```

The flag: replace

```python
    validate.add_argument(
        "--show-config", action="store_true", help="print the effective configuration as YAML"
    )
    validate.set_defaults(func=cmd_validate)
```

with

```python
    validate.add_argument(
        "--show-config", action="store_true", help="print the effective configuration as YAML"
    )
    validate.add_argument(
        "--slack-probe",
        action="store_true",
        help="post one test message to the configured Slack webhook",
    )
    validate.set_defaults(func=cmd_validate)
```

`cmd_validate`: replace

```python
    adapter = _adapter_factory(workflow.config.github) if _which("gh") else None
    checks = run_checks(workflow, adapter=adapter)
```

with

```python
    adapter = _adapter_factory(workflow.config.github) if _which("gh") else None
    checks = run_checks(workflow, adapter=adapter, slack_probe=args.slack_probe)
```

`run_checks`: replace

```python
def run_checks(workflow: Workflow, *, adapter: GitHubAdapter | None = None) -> list[Check]:
```

with

```python
def run_checks(
    workflow: Workflow, *, adapter: GitHubAdapter | None = None, slack_probe: bool = False
) -> list[Check]:
```

and replace

```python
    checks.append(
        Check(
            "notifications.slack",
            "ok",
            "configured" if cfg.notifications.slack.webhook_url else "not configured",
        )
    )
    checks.append(_prompt_check(workflow))
    return checks
```

with

```python
    checks.append(_slack_check(cfg, probe=slack_probe))
    checks.append(_prompt_check(workflow))
    return checks
```

The check: insert immediately before `def _prompt_check(workflow: Workflow) -> Check:`:

```python
def _slack_check(settings: Settings, *, probe: bool) -> Check:
    """The notifications.slack line: presence, URL shape (never the URL itself), optional probe."""
    subject = "notifications.slack"
    slack = settings.notifications.slack
    kinds = ", ".join(sorted(subscribed_kinds(slack)))
    if slack.webhook_url is None:
        if not kinds:
            return Check(subject, "ok", "not configured (events: [])")
        detail = (
            f"not configured; export SLACK_WEBHOOK_URL to notify on {kinds}, "
            "or set notifications.slack.events: [] to silence this"
        )
        return Check(subject, "warn", detail)
    url = slack.webhook_url.get_secret_value()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return Check(subject, "fail", "webhook_url is not an https URL")
    if not kinds:
        return Check(subject, "warn", "configured but events is empty; nothing will be sent")
    status: CheckStatus = "ok"
    detail = f"configured ({kinds})"
    if parts.hostname != SLACK_WEBHOOK_HOST or not parts.path.startswith(SLACK_WEBHOOK_PATH):
        status = "warn"
        detail += (
            "; the URL is not a hooks.slack.com/services/ webhook (a compatible endpoint is fine)"
        )
    if probe:
        text = (
            f":wave: issuebot validate: Slack notifications are configured for {kinds} "
            f"({settings.github.repo})"
        )
        result = asyncio.run(_slack_post(url, slack_payload(text), timeout_s=POST_TIMEOUT_S))
        if not result.ok:
            reason = f"HTTP {result.status}" if result.status else result.error or "no response"
            return Check(subject, "fail", f"test message not delivered: {reason}")
        detail += "; test message delivered"
    return Check(subject, status, detail)
```

(followed by two blank lines before `def _prompt_check`).

The bus helpers: replace

```python
# --- run-once --------------------------------------------------------------------------


def cmd_run_once(args: argparse.Namespace) -> int:
```

with

```python
# --- the event bus ---------------------------------------------------------------------


def _slack_sink(settings: Settings) -> SlackSink | None:
    """A Slack sink when a webhook is set and at least one kind is subscribed."""
    slack = settings.notifications.slack
    if slack.webhook_url is None or not subscribed_kinds(slack):
        return None
    return SlackSink(
        slack, repo=settings.github.repo, labels=settings.github.labels, post=_slack_post
    )


def _build_bus(settings: Settings) -> tuple[EventBus, SlackSink | None]:
    """The log sink, plus the Slack sink when configured; the caller starts and closes it."""
    slack = _slack_sink(settings)
    sinks: list[EventSink] = [LogSink()]
    if slack is not None:
        sinks.append(slack)
    return EventBus(sinks), slack


# --- run-once --------------------------------------------------------------------------


def cmd_run_once(args: argparse.Namespace) -> int:
```

`_run_once`: the tail after the `--show-prompt` branch becomes `_claim_and_run`, wrapped so the sink is always closed. Replace

```python
        try:
            print(PromptRenderer(workflow.prompt_template).render(context).rstrip("\n"))
        except AgentError as exc:
            print(f"[FAIL] prompt: {exc.message}")
            return 1
        return 0
    bus = EventBus([LogSink()])
    if issue.state is not StateLabel.IN_PROGRESS:
```

with

```python
        try:
            print(PromptRenderer(workflow.prompt_template).render(context).rstrip("\n"))
        except AgentError as exc:
            print(f"[FAIL] prompt: {exc.message}")
            return 1
        return 0
    bus, slack = _build_bus(settings)
    if slack is not None:
        slack.start(bus)
    try:
        return await _claim_and_run(
            workflow, adapter, bus, issue, workspaces=workspaces, attempt=attempt, rework=rework
        )
    finally:
        if slack is not None:
            await slack.close()


async def _claim_and_run(
    workflow: Workflow,
    adapter: GitHubAdapter,
    bus: EventBus,
    issue: Issue,
    *,
    workspaces: WorkspaceManager,
    attempt: int,
    rework: bool,
) -> int:
    settings = workflow.config
    number = issue.number
    if issue.state is not StateLabel.IN_PROGRESS:
```

(The rest of the old `_run_once` body, from `from_label = ...` to `return 0 if result.outcome == "succeeded" else 1`, is now the body of `_claim_and_run` unchanged.)

`_run_worker`: replace

```python
async def _run_worker(workflow: Workflow) -> int:
    """Run the orchestrator until a stop signal; 1 when startup validation fails."""
    orchestrator = _orchestrator_factory(
        workflow,
        bus=EventBus([LogSink()]),
        adapter_factory=_adapter_factory,
        run_session=_run_session,
        which=_which,
    )
    loop = asyncio.get_running_loop()
```

with

```python
async def _run_worker(workflow: Workflow) -> int:
    """Run the orchestrator until a stop signal; 1 when startup validation fails."""
    bus, slack = _build_bus(workflow.config)
    orchestrator = _orchestrator_factory(
        workflow,
        bus=bus,
        adapter_factory=_adapter_factory,
        run_session=_run_session,
        which=_which,
    )
    if slack is not None:
        slack.start(bus)
    loop = asyncio.get_running_loop()
```

and replace

```python
    finally:
        for signum in signals:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signum)
    return 0
```

with

```python
    finally:
        for signum in signals:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signum)
        if slack is not None:
            await slack.close()
    return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `timeout 120 uv run pytest tests/test_cli.py -q`
Expected: 73 passed. `uv run ruff check .` and `uv run ruff format --check .` clean (the new `_slack_check` and `_build_bus` are already at the formatter's shape).

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/cli.py tests/test_cli.py
git commit -m "feat: wire the Slack sink into run-once and worker, check the webhook in validate"
```

Expected before the commit: 572 passed.

---

### Task 5: Documentation and the Phase 4 spec prose pass

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`, `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md`, the dot-env example file at the repository root

Use the Edit tool with the exact old strings below. Every claim was checked against the Task 1 to 4 code.

- [ ] **Step 1: `CLAUDE.md`**

In the Commands block, after

```
uv run issuebot validate             # load ./WORKFLOW.md and check the environment
```

add the line

```
uv run issuebot validate --slack-probe   # same, plus one test message to the Slack webhook
```

In the `issuebot.orchestrator` bullet replace

```
  fires retries (continuation 1 s; failure backoff; `escape`; `slots`) and handles worker exits
  (`max_turns` while `in_progress` or `max_attempts` failures → the blocked escape).
```

with

```
  fires retries (continuation 1 s; failure backoff; `escape`; `slots`) and handles worker exits
  (the session's final transition is published before any release; `max_turns` while
  `in_progress` or `max_attempts` failures → the blocked escape).
```

Replace the `issuebot.cli` bullet (from the line `  with a fake clock and a scripted `run_session`.` that ends the orchestrator bullet, through `_orchestrator_factory`.`) with

```
  with a fake clock and a scripted `run_session`.
- `issuebot.notifications`: the Slack sink, imported by `cli` only. `messages.py` (pure):
  `format_event(event, repo=, labels=)` → one line of mrkdwn per kind (issue link, `from → to`
  by actor, PR link, blocker reason, run cost) or `None`. `slack.py`: `urllib_post` (stdlib
  `urllib` in `asyncio.to_thread`, never raises, errors pass through `redact`), `PostResult`,
  `subscribed_kinds` (the allow-list minus `notification_sent`), `SlackSink` (`handle` formats
  and enqueues, cap 100; one drain task started by `start(bus)` posts with three attempts,
  `Retry-After` on 429 capped at 30 s, backoff 1 s then 4 s on 5xx and network errors, other
  4xx dropped; publishes `NotificationSent` after each delivery; `close()` drains for up to
  10 s). Constants, not settings. A webhook or allow-list change needs a worker restart.
- `issuebot.cli`: argparse; `validate` (twelve checks: three network probes through the
  adapter, a `claude --version` floor of 2.1.259, a `notifications.slack` check that warns when
  `SLACK_WEBHOOK_URL` is unset, requires `https`, and with `--slack-probe` posts one test
  message, and a prompt render against a sample issue), `labels ensure`, `issues list`,
  `run-once <number> [--show-prompt]` (claims `in-progress`, runs one session, never sets
  `review`), `worker [--workflow PATH]` (the orchestrator until SIGTERM/SIGINT; `[FAIL]
  startup:` lines and exit 1 when the startup probes fail); `run-once` and `worker` start the
  Slack sink before and close it after; exit codes 0/1/2 (ok / failed / workflow unloadable).
  Tests substitute `_which`, `_claude_version`, `_adapter_factory`, `_run_session`,
  `_orchestrator_factory` and `_slack_post`.
```

- [ ] **Step 2: `README.md`**

After the line

```
uv run issuebot validate          # checks ./WORKFLOW.md and the environment
```

add

```
uv run issuebot validate --slack-probe   # same, plus one test message to the Slack webhook
```

and before the paragraph that begins `The design lives in` insert

```
Slack notifications are optional: export `SLACK_WEBHOOK_URL` (an incoming webhook,
`https://hooks.slack.com/services/...`) and choose the event kinds in `WORKFLOW.md` under
`notifications.slack.events` (default `state_changed` and `blocked`; add `run_ended` for a
line per run with its cost). The worker reads both at start, so changing either needs a
restart; `validate` warns while the variable is unset.

```

- [ ] **Step 3: Roadmap**

In `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`, Phase 5, after the "Done when" paragraph (`` `todo → in-progress → review → complete` on a scratch issue. ``) add a paragraph:

```
Decided 2026-09-03 (Phase 5 spec): the sink lives in `issuebot.notifications` (a
settings-taking sink inside `issuebot.events` would close an import cycle with
`config`); the transport is stdlib `urllib` in a worker thread, no new dependency;
the allow-list is by kind, `run_ended` covers every outcome and stays opt-in, so a
failed run reaches the channel through `blocked` by default; `NotificationSent` is
published after each delivery and never re-notified; `validate` warns when the
webhook is unset, requires `https`, and gains `--slack-probe`; `run-once` posts too;
a webhook or allow-list change needs a restart. Deferred: Block Kit layouts and
attachments, per-channel routing, a reload hook for `notifications.*`.
```

- [ ] **Step 4: Phase 4 spec prose pass (the code is the reference)**

In `docs/superpowers/specs/2026-09-03-phase-4-orchestrator-design.md`:

§5, `blocked_escape` step 1: replace `` IN_PROGRESS`: return `True` (the world moved on; nothing to do). `` with `` IN_PROGRESS`: return `"skipped"` (the world moved on; nothing to do). ``. Step 4: replace `` `Blocked(reason=context.reason)`. Return `True`. `` with `` `Blocked(reason=context.reason)`. Return `"applied"`. ``.

§6.4 step 2: replace

```
   both cases. Any other record (`succeeded`, `failed`, `timed_out`), no
   record, or a `path_for` failure (logged `dispatch_skipped`) means a
   fresh session at attempt 1. `todo` and `rework` candidates are always
   fresh; retries (§6.7) never resume.
```

with

```
   both cases. Any other record (`succeeded`, `failed`, `timed_out`) or no
   record means a fresh session at attempt 1; a `path_for` failure skips the
   candidate for this tick (logged `dispatch_skipped`; Phase 4 ruling R2).
   `todo` and `rework` candidates are always fresh; retries (§6.7) never
   resume.
```

§6.5 Part B, the `closed` row: append to the cell, after `never while `claude` may still be writing to the workspace`, the sentence `. A later refresh that returns the issue open again clears `terminal_issue`, so the exit releases the issue instead of finishing it (Phase 4 ruling R13)` (the row keeps its closing ` |`).

§6.7 steps 1 to 4: replace

```
1. Pop it. `fetch_issues_by_ids([issue_id])`; a `GitHubError` re-schedules
   the same kind and attempt after `polling.interval_ms` with error `retry
   refresh failed: <message>` (never counted as an attempt).
2. Missing: release (log `retry_released`, reason `missing`).
3. `kind == "escape"`: `blocked_escape(...)` with the stored context; `False`
   re-schedules kind `escape` with `attempt + 1`. Done either way.
4. Closed: `finish_terminal`; release.
```

with

```
1. Pop it. `kind == "escape"`: `blocked_escape(...)` with the stored context
   (it refreshes the issue itself); `"failed"` re-schedules kind `escape`
   with `attempt + 1`. Done either way, before any refresh.
2. `fetch_issues_by_ids([issue_id])`; a `GitHubError` re-schedules the same
   kind and attempt after `polling.interval_ms` with error `retry refresh
   failed: <message>` (never counted as an attempt).
3. Missing: release (log `retry_released`, reason `missing`).
4. Closed: `finish_terminal`; release.
```

§6.8, the table: replace its nine body rows with

```
| `entry.terminal_issue` is set | `finish_terminal(entry.terminal_issue)`; release |
| `outcome == "succeeded"` and `final_issue` is set and open, whatever the stop cause | publish `observe_transition(entry.issue, final_issue)`, then continue with the rows below (amended by Phase 5, see below) |
| task cancelled by `task.cancel()` (shutdown last resort), or `stop_cause in ("moved", "missing", "shutdown", "closed")` | release (`closed` reaches here only when a later refresh cleared the terminal snapshot: the reopen edge of §6.5) |
| `stop_cause == "stalled"` | as failed, error `stalled: <detail>` |
| task raised (not cancelled) | log `worker_crashed` with the traceback; treat as failed with error `worker crashed: <exc>` |
| `outcome == "succeeded"`, `stop_reason == "max_turns"`, `final_state is IN_PROGRESS` | blocked escape with reason `Turn budget exhausted ...`; on `"failed"`, schedule kind `escape`, attempt 1 |
| `outcome == "succeeded"` otherwise | schedule `continuation`, attempt 1 |
| failed, `entry.attempt < max_attempts` | schedule `failure`, `attempt + 1`, error `<category>: <message>` |
| failed, `entry.attempt >= max_attempts` | blocked escape with reason `<n> consecutive worker sessions failed; last error: ...`; on `"failed"`, schedule kind `escape`, attempt 1 |
```

§11, the `test_orchestrator_actions.py` row: replace `` `GitHubError` on the comment write → `False` and no `set_state`; `` with `` `GitHubError` on the comment write → `"failed"` and no `set_state`; `` and `` already complete (no `set_state`, removal still attempted), `GitHubError` → `None` and removal still attempted; `` with `` already complete (no `set_state`, removal still attempted), `GitHubError` → `"failed"` and removal still attempted; ``.

- [ ] **Step 5: The dot-env example (Edit tool only; never name this file in a shell command)**

In `.env.example` replace

```
# Optional: Slack incoming webhook for notifications (Phase 5).
SLACK_WEBHOOK_URL=
```

with

```
# Optional: Slack incoming webhook (https://hooks.slack.com/services/...) for notifications.
# WORKFLOW.md's notifications.slack.events picks the kinds (default: state_changed, blocked);
# changing either needs a worker restart.
SLACK_WEBHOOK_URL=
```

- [ ] **Step 6: Verify and commit**

Run: `uv run pre-commit run --all-files && uv run pytest -q`
Expected: hooks pass (no document in this task carries a Python fence, so nothing is reflowed); 572 passed. `git status --short` lists exactly the five files.

```bash
git add --all
git commit -m "docs: describe issuebot.notifications, the Slack validate check and the Phase 4 amendments"
```

---

### Task 6: Live check against `jleavers/issuebot-scratch`

**Files:** none in this repository. Everything here happens against GitHub, a Slack test channel, and directories outside every checkout. This task spends real Claude budget under the operator's subscription login (about $0.60; no `ANTHROPIC_API_KEY` exported) and creates a real issue, branch and pull request; that is intended. Never print `GH_TOKEN` or `SLACK_WEBHOOK_URL`. The executor never merges a pull request; Step 5 asks the operator to. The executor cannot see the Slack channel: the evidence is the worker log plus the operator's confirmation of what the channel shows.

Starting state (from the Phase 4 live check): issue #1 closed `issuebot/complete`; issue #3 (`Add a multiply function`) in `issuebot/review` with PR #4 open (`issuebot/3-multiply-function`, `Closes #3`); workspace `~/issuebot-workspaces/issuebot-scratch-3`; `~/issuebot-scratch/worker.log` from Phase 4.

- [ ] **Step 1: The webhook file and the scratch workflow**

Ask the operator to create a Slack incoming webhook for a test channel and write its URL, on one line, to `~/issuebot-scratch/slack-webhook` with mode 600 (they do this in their own shell; the executor never sees the value). Confirm it exists without reading it:

```bash
test -s ~/issuebot-scratch/slack-webhook && stat -c '%a %n' ~/issuebot-scratch/slack-webhook
```

Expected: `600 /home/jleavers/issuebot-scratch/slack-webhook`.

Recreate `~/issuebot-scratch/WORKFLOW.md` from the repository's `WORKFLOW.md`: `cp WORKFLOW.md ~/issuebot-scratch/WORKFLOW.md`, then with the Edit tool change `repo: jleavers/issuebot` to `repo: jleavers/issuebot-scratch`, `root: /workspaces` to `root: /home/jleavers/issuebot-workspaces`, add `  stall_timeout_ms: 1800000` as a new line directly under `  setting_sources: [project]` (the `claude:` section; the 5-minute default kills any tool call silent for longer and costs an attempt), and change `    events: [state_changed, blocked]` to `    events: [state_changed, blocked, run_ended]` (so the channel also shows the run's cost line). Then:

```bash
export GH_TOKEN=$(gh auth token) && export SLACK_WEBHOOK_URL=$(cat ~/issuebot-scratch/slack-webhook) && unset ANTHROPIC_API_KEY && claude --version && uv run issuebot validate --slack-probe --workflow ~/issuebot-scratch/WORKFLOW.md && uv run issuebot issues list --workflow ~/issuebot-scratch/WORKFLOW.md
```

Expected: `[ OK ] notifications.slack: configured (blocked, run_ended, state_changed); test message delivered` and `12 checks: 0 failed, 0 warnings`; the issue table shows `#3` in `review` with `#4 open`. Ask the operator to confirm the `:wave: issuebot validate: ...` message arrived in the channel. If the line reads `test message not delivered: HTTP 403` or `404`, the webhook is invalid or revoked: stop and ask for a new one.

- [ ] **Step 2: Start the worker detached**

```bash
export GH_TOKEN=$(gh auth token) && export SLACK_WEBHOOK_URL=$(cat ~/issuebot-scratch/slack-webhook) && unset ANTHROPIC_API_KEY && cd /home/jleavers/_dev/issuebot && setsid nohup uv run issuebot --log-format console worker --workflow ~/issuebot-scratch/WORKFLOW.md >> ~/issuebot-scratch/worker.log 2>&1 < /dev/null & sleep 5 && pgrep -f '\.venv/bin/issuebot --log-format console worker' > ~/issuebot-scratch/worker.pid && cat ~/issuebot-scratch/worker.pid && tail -6 ~/issuebot-scratch/worker.log
```

Expected: one pid; the log ends with `slack_sink_started kinds=['blocked', 'run_ended', 'state_changed']`, `orchestrator_started` with `repo=jleavers/issuebot-scratch`, and no `dispatched` line (issue #3 is in `review`). Nothing is posted to Slack at startup.

- [ ] **Step 3: File a trivial issue**

Write `~/issuebot-scratch/issue-divide.md` with the Write tool:

```markdown
Add a `divide(a: int, b: int) -> float` function to `src/scratch/__init__.py` next to `add` and `subtract`, returning `a / b`.

## Acceptance criteria

- `divide(6, 3) == 2.0` and `divide(7, 2) == 3.5`.
- A test in `tests/test_scratch.py` covers both cases.
- `uv run pytest -q` passes.
```

```bash
gh issue create -R jleavers/issuebot-scratch --title "Add a divide function" --body-file ~/issuebot-scratch/issue-divide.md --label issuebot/todo
```

Note the number (`N` below).

- [ ] **Step 4: Watch the run reach `review` and Slack**

Wait with a bounded loop under `run_in_background` (up to fifteen minutes):

```bash
timeout 900 bash -c 'until grep -q "to_label=issuebot/review" ~/issuebot-scratch/worker.log; do sleep 10; done'; grep -E "state_changed|slack_|notification_sent|run_ended|dispatched|blocked" ~/issuebot-scratch/worker.log | tail -24
```

Expected sequence for issue N: `state_changed` with `actor=issuebot to_label=issuebot/in-progress`, `slack_notification_sent kind=state_changed attempt=1`, `notification_sent about_kind=state_changed channel=slack`, `dispatched`, `run_started`, ..., `state_changed` with `actor=agent to_label=issuebot/review pr_url=...`, `slack_notification_sent kind=state_changed`, `run_ended outcome=succeeded`, `slack_notification_sent kind=run_ended`, and one `notification_sent` per delivery; no `slack_post_retry`, `slack_notification_failed` or `slack_queue_full`. Ask the operator to confirm the channel shows three messages: the claim (`:hammer_and_wrench:`), the review move by the agent with a `PR #<n>` link (`:eyes:`), and the run line with its cost (`:white_check_mark:`); also whether the GitHub links unfurl into previews (record it; if they do and are noisy, the payload gains `unfurl_links: false` in `slack_payload` as a follow-up). Then:

```bash
gh issue view N -R jleavers/issuebot-scratch --json labels,state --jq '{labels: [.labels[].name], state}' && gh pr list -R jleavers/issuebot-scratch --json number,headRefName,body --jq '.[] | {number, headRefName, closes: (.body | test("Closes #N"))}'
```

Expected: labels `["issuebot/review"]`; an open PR from `issuebot/N-...` whose body closes N. If the run ends `max_turns` instead, the worker applies the blocked escape and the channel shows the `:no_entry:` blocked line as well; record that and read `turn-*.jsonl` under `~/issuebot-workspaces/issuebot-scratch-N/.issuebot/runs/`.

- [ ] **Step 5: Merged pull request → `complete` in Slack (operator action)**

Ask the operator to merge PR #4 on GitHub (the executor never merges). Then wait for the next terminal sweep (every tenth tick, five minutes at the 30 s interval):

```bash
timeout 420 bash -c 'until grep -q "issue_finished.*outcome=complete" ~/issuebot-scratch/worker.log; do sleep 10; done'; grep -E "issue_finished|issue_completed|to_label=issuebot/complete|slack_notification_sent|workspace_removed" ~/issuebot-scratch/worker.log | tail -6 && gh issue view 3 -R jleavers/issuebot-scratch --json labels,state --jq '{labels: [.labels[].name], state}' && ls ~/issuebot-workspaces
```

Expected: `issue_finished outcome=complete` for issue 3, `state_changed actor=issuebot to_label=issuebot/complete`, `slack_notification_sent kind=state_changed`, `issue_completed` (logged, not posted: not in the allow-list), `workspace_removed` for `issuebot-scratch-3`; GitHub shows `{"labels": ["issuebot/complete"], "state": "CLOSED"}` and no `issuebot-scratch-3` directory. Ask the operator to confirm the channel shows `review → complete` by issuebot (`:white_check_mark:`) with the PR link.

- [ ] **Step 6: SIGTERM while idle**

```bash
kill -TERM $(cat ~/issuebot-scratch/worker.pid) && sleep 3 && tail -4 ~/issuebot-scratch/worker.log && (pgrep -f '\.venv/bin/issuebot --log-format console worker' || echo "no worker left")
```

Expected: `stop_requested`, `orchestrator_stopped` within a second, then `slack_sink_closed sent=4 failed=0 dropped=0` (claim, review, run_ended, complete) and `no worker left`.

- [ ] **Step 7: Report**

Paste the relevant log lines from Steps 2, 4, 5 and 6, the `gh` outputs, the operator's confirmations and the unfurl observation into the report for the PR body. Do not merge N's pull request. Leave `~/issuebot-scratch/slack-webhook` to the operator.

---

### Task 7: Push the branch and open the pull request

**Files:** none.

- [ ] **Step 1: Final full check**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files && uv run pytest -q`
Expected: everything passes; 572 passed; `git status --short` is empty; `git diff main -- pyproject.toml uv.lock` is empty.

- [ ] **Step 2: Push the branch**

Run: `git push -u origin phase-5-slack`

- [ ] **Step 3: Write the PR body to a file under the session scratchpad directory (a separate call from Step 4; use the Write tool)**

`<scratchpad>/issuebot-phase-5-pr.md`:

```markdown
## Phase 5: Slack notifications

Implements `docs/superpowers/specs/2026-09-03-phase-5-slack-notifications-design.md`.

- `issuebot.notifications`: `messages.py` renders one line of Slack mrkdwn per event kind (issue link, `from → to` by actor with an emoji per target role, PR link, blocker reason, run outcome and cost); `slack.py` holds the stdlib `urllib` transport (worker thread, never raises, errors redacted) and `SlackSink`
- `SlackSink.handle` only formats and enqueues (cap 100, drop and log when full); one drain task posts with three attempts, honours `Retry-After` on 429 (capped at 30 s), backs off 1 s then 4 s on 5xx and network errors, drops other 4xx at once, publishes `NotificationSent` after each delivery and never notifies about its own kind; `close()` drains for up to 10 s
- `run-once` and `worker` start the sink before and close it after the session or the orchestrator, so shutdown events are delivered too
- `validate`: the `notifications.slack` check warns when `SLACK_WEBHOOK_URL` is unset (`events: []` silences it), requires `https`, warns on a non-`hooks.slack.com` host, and `--slack-probe` posts one test message
- No new dependency, no new setting; the allow-list is by kind (`run_ended` renders every outcome and stays opt-in); a webhook or allow-list change needs a restart (documented)
- Phase 4 follow-up: a worker that succeeds while being stopped now publishes its final transition before the release rows; the Phase 4 spec carries the amendment note and the prose fixes for the outcome strings, the `path_for` skip and the two orderings
- Live check against `jleavers/issuebot-scratch`: `validate --slack-probe` delivered a test message, a `todo` issue reached `review` with the claim, the agent's move and the run's cost posted to the channel, merging PR #4 posted `review → complete`, and SIGTERM while idle closed the sink with every message sent (output below)

Database and dashboard are Phases 6 and 7.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Append the live-check output from Task 6 under a `## Live check` heading before the generated-with line, and the session link the executing harness requires after it.

- [ ] **Step 4: Open the PR via the REST API (the CLI's `pr create` is blocked in this repo)**

```bash
gh api repos/jleavers/issuebot/pulls -X POST \
  -f title='Phase 5: Slack notifications' \
  -f head='phase-5-slack' -f base='main' \
  -F body=@<scratchpad>/issuebot-phase-5-pr.md
```

Then confirm with `gh pr view --json title,body --jq '.title'` and watch CI with `gh pr checks --watch`. CI must be green before handing over for human review. Do not merge.

---

## Acceptance criteria

Spec §12, restated for the executor:

- `uv run pytest -q` passes with no network (572 tests after Task 4); ruff and pre-commit clean; CI green; `docker compose build` succeeds; `pyproject.toml` and `uv.lock` unchanged.
- `uv run issuebot validate` on the committed `WORKFLOW.md` prints twelve checks, with the `notifications.slack` line warning while `SLACK_WEBHOOK_URL` is unset.
- The live check (Task 6) shows: the probe's test message delivered; the claim, the agent's move to `review` with the PR link and the run's cost line in the channel; `review → complete` after the operator merged PR #4; `slack_sink_closed sent=4 failed=0 dropped=0` after an idle SIGTERM.
- `CLAUDE.md`, `README.md`, the roadmap, the Phase 4 spec and the dot-env example carry the Task 5 edits.
