"""Tests for the orchestrator's GitHub-writing actions against FakeGitHub."""

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from issuebot.agent import AgentError, WorkspaceManager
from issuebot.config import Settings
from issuebot.events import (
    Blocked,
    Event,
    EventBus,
    IssueCancelled,
    IssueCompleted,
    StateChanged,
)
from issuebot.github import WORKPAD_MARKER, FakeGitHub, GitHubError, StateLabel
from issuebot.orchestrator.actions import (
    BUDGET_HEADING,
    BUDGET_REASON_PREFIX,
    CANCEL_REASON,
    CONFLICT_HEADING,
    CONFLICT_LIMIT_HEADING,
    _has_budget_block,
    blocked_block,
    blocked_escape,
    budget_block,
    budget_escape,
    claim,
    conflict_block,
    conflict_limit_block,
    conflict_rework,
    finish_terminal,
    remove_workspace,
)
from issuebot.orchestrator.state import BlockedContext

NOW = datetime(2026, 9, 3, 14, 2, 11, tzinfo=UTC)
CONTEXT = BlockedContext(
    reason="Turn budget exhausted: 5 turns in attempt 2 without reaching `issuebot/review`.",
    run_id="20260903T135501Z-a1b2c3",
    attempt=2,
    turns=5,
    log_dir="/workspaces/example-42/.issuebot/runs/20260903T135501Z-a1b2c3",
)


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings.model_validate(
            {"github": {"repo": "example/repo"}, "workspace": {"root": str(tmp_path / "ws")}}
        )
        self.github = FakeGitHub(self.settings.github)
        self.recorder = Recorder()
        self.bus = EventBus([self.recorder])
        environ = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
        self.workspaces = WorkspaceManager(
            self.settings, environ=environ, hook_shell=("bash", "-c")
        )

    def workspace_dir(self, identifier: str) -> Path:
        path = self.workspaces.root / identifier
        (path / ".git").mkdir(parents=True)
        (path / ".issuebot").mkdir()
        return path

    def calls(self, name: str) -> list[tuple[object, ...]]:
        return [args for called, args in self.github.calls if called == name]


# --- conflict rework --------------------------------------------------------------------

LABELS = Settings.model_validate({"github": {"repo": "a/b"}}).github.labels


def test_conflict_block_names_the_pr_the_bounce_and_the_next_session() -> None:
    block = conflict_block(51, 1, 3, NOW, LABELS)
    assert block.startswith("### Issuebot merge conflict (2026-09-03T14:02:11Z)\n\n")
    assert "Pull request #51 conflicts with the default branch (bounce 1 of 3)." in block
    assert "Moved to `issuebot/rework`:" in block
    assert block.endswith("returns the issue to `issuebot/review`.")


def test_conflict_limit_block_says_it_stopped() -> None:
    block = conflict_limit_block(51, 3, NOW, LABELS)
    assert block.startswith("### Issuebot merge conflict limit (2026-09-03T14:02:11Z)\n\n")
    assert "moved this issue to `issuebot/rework` 3 times" in block
    assert block.endswith("A human resolves the conflict on the branch, or moves the issue.")


def test_the_two_conflict_headings_stay_distinguishable() -> None:
    assert not CONFLICT_LIMIT_HEADING.startswith(CONFLICT_HEADING)


async def seed(h: Harness, *blocks: str, bounces: int = 0) -> None:
    """A review issue whose PR #51 conflicts, bounced ``bounces`` times already by issuebot's
    own account (its label history, which is what the count reads), with a workpad holding
    ``blocks`` if given."""
    h.github.add_issue("Task", labels=("issuebot/review",), number=42)
    h.github.open_pr(42, pr_number=51)
    h.github.set_pr_mergeable(51, "conflicting")
    for _ in range(bounces):
        await h.github.set_state(42, StateLabel.REWORK)
        h.github.human_set_state(42, StateLabel.REVIEW)  # the session brought it back
    if blocks:
        body = "\n\n".join([WORKPAD_MARKER, "### Plan\n\n- [ ] 1. Do it", *blocks]) + "\n"
        await h.github.comment(42, body)
    h.github.calls.clear()


async def test_conflict_rework_moves_the_issue_and_records_the_bounce(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await seed(h)
    issue = h.github.issue(42)
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "reworked"
    assert h.github.issue(42).state is StateLabel.REWORK
    comments = h.github.comments_for(42)
    assert len(comments) == 1
    assert comments[0].body.startswith(f"{WORKPAD_MARKER}\n\n{CONFLICT_HEADING}")
    assert "(bounce 1 of 3)" in comments[0].body
    # The label history is read for the count and the workpad for the note's home, then the
    # label moves, then the note lands.
    assert [name for name, _ in h.github.calls] == [
        "count_own_label_additions",
        "find_workpad_comment",
        "set_state",
        "comment",
    ]
    assert h.calls("count_own_label_additions") == [(42, "issuebot/rework")]
    assert h.calls("set_state") == [(42, StateLabel.REWORK)]
    assert h.recorder.kinds == ["state_changed"]
    changed = h.recorder.events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label, changed.actor) == (
        "issuebot/review",
        "issuebot/rework",
        "issuebot",
    )
    assert changed.pr_url == "https://github.com/example/repo/pull/51"


async def test_conflict_rework_appends_to_an_existing_workpad_and_counts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    blocks = (conflict_block(51, 1, 3, NOW, LABELS), conflict_block(51, 2, 3, NOW, LABELS))
    await seed(h, *blocks, bounces=2)
    issue = h.github.issue(42)
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "reworked"
    body = h.github.comments_for(42)[0].body
    assert body.startswith(f"{WORKPAD_MARKER}\n\n### Plan\n\n- [ ] 1. Do it\n\n")
    assert body.count(CONFLICT_HEADING) == 3
    assert "(bounce 3 of 3)" in body
    assert body.endswith("`issuebot/review`.\n")
    assert h.calls("comment") == []
    assert len(h.calls("update_comment")) == 1
    assert h.github.issue(42).state is StateLabel.REWORK


async def test_conflict_rework_at_the_limit_notes_it_once_and_stays_in_review(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    await seed(h, *(conflict_block(51, n, 3, NOW, LABELS) for n in (1, 2, 3)), bounces=3)
    issue = h.github.issue(42)
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "limit_reached"
    body = h.github.comments_for(42)[0].body
    assert body.count(CONFLICT_LIMIT_HEADING) == 1
    assert body.endswith("or moves the issue.\n")
    assert h.github.issue(42).state is StateLabel.REVIEW
    assert h.calls("set_state") == []
    assert h.recorder.events == []
    h.github.calls.clear()
    # The block's presence is the idempotence: the next tick changes nothing.
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "limit_noted"
    assert h.github.comments_for(42)[0].body == body
    assert [name for name, _ in h.github.calls] == [
        "count_own_label_additions",
        "find_workpad_comment",
    ]


async def test_conflict_rework_with_the_limit_lowered_below_the_count(tmp_path: Path) -> None:
    """An operator dropping the setting to 1 after two bounces gets the limit note, not a third."""
    h = Harness(tmp_path)
    await seed(
        h, conflict_block(51, 1, 3, NOW, LABELS), conflict_block(51, 2, 3, NOW, LABELS), bounces=2
    )
    issue = h.github.issue(42)
    assert await conflict_rework(h.github, h.bus, issue, limit=1, now=NOW) == "limit_reached"
    assert h.github.issue(42).state is StateLabel.REVIEW


async def test_the_cap_holds_when_the_session_rewrites_the_workpad(tmp_path: Path) -> None:
    """The count is the label history, so a workpad stripped of every block changes nothing
    (#104): three bounces, then the limit note, however the body reads."""
    h = Harness(tmp_path)
    await seed(h)
    outcomes = []
    for _ in range(5):
        outcomes.append(
            await conflict_rework(h.github, h.bus, h.github.issue(42), limit=3, now=NOW)
        )
        h.github.human_set_state(42, StateLabel.REVIEW)  # the session returns it to review
        pad = await h.github.find_workpad_comment(42)
        assert pad is not None
        await h.github.update_comment(pad.id, f"{WORKPAD_MARKER}\n\n### Plan\n\n- [x] done\n")
    assert outcomes == ["reworked", "reworked", "reworked", "limit_reached", "limit_reached"]
    assert h.github.issue(42).state is StateLabel.REVIEW
    assert len([e for e in h.recorder.events if isinstance(e, StateChanged)]) == 3


async def test_a_humans_rework_label_is_not_a_bounce(tmp_path: Path) -> None:
    """Only the account's own additions count; a reviewer asking for changes is not the cap."""
    h = Harness(tmp_path)
    await seed(h)
    for _ in range(3):
        h.github.human_set_state(42, StateLabel.REWORK, actor="reviewer")
        h.github.human_set_state(42, StateLabel.REVIEW)
    assert (
        await conflict_rework(h.github, h.bus, h.github.issue(42), limit=1, now=NOW) == "reworked"
    )


async def test_conflict_rework_failure_on_the_count_writes_nothing(tmp_path: Path) -> None:
    """The count is read first; without it there is no bound to act under."""
    h = Harness(tmp_path)
    await seed(h)
    h.github.fail_next("transport")
    assert await conflict_rework(h.github, h.bus, h.github.issue(42), limit=3, now=NOW) == "failed"
    assert h.github.issue(42).state is StateLabel.REVIEW
    assert h.github.comments_for(42) == []
    assert h.recorder.events == []


async def test_conflict_rework_gives_up_on_a_non_retryable_failure(tmp_path: Path) -> None:
    """A read past its cap (#110) is `gave_up`, not `failed`: the orchestrator holds off until
    the issue changes rather than repeating the same bounded read on every poll. Only a
    `response` error is: a 5xx or a transport error is the next tick's to retry."""
    h = Harness(tmp_path)
    await seed(h)
    h.github.fail_next("response")
    with capture_logs() as logs:
        outcome = await conflict_rework(h.github, h.bus, h.github.issue(42), limit=3, now=NOW)
    assert outcome == "gave_up"
    assert h.github.issue(42).state is StateLabel.REVIEW
    assert h.github.comments_for(42) == []
    assert h.recorder.events == []
    failed = [entry for entry in logs if entry["event"] == "conflict_rework_failed"]
    assert failed and failed[0]["retryable"] is False
    h.github.fail_next("transport")
    assert await conflict_rework(h.github, h.bus, h.github.issue(42), limit=3, now=NOW) == "failed"


async def test_conflict_rework_failure_on_set_state_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    await seed(h)

    async def refuse(*args: object, **kwargs: object) -> None:
        raise GitHubError("rate_limited", "slow down")

    monkeypatch.setattr(h.github, "set_state", refuse)
    issue = h.github.issue(42)
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "failed"
    assert h.github.comments_for(42) == []
    assert h.github.issue(42).state is StateLabel.REVIEW
    assert h.recorder.events == []


async def test_conflict_rework_failure_on_the_note_still_counts_as_reworked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The label moved, so the rework session resolves the conflict either way."""
    h = Harness(tmp_path)
    await seed(h)
    original = h.github.set_state

    async def then_fail_the_note(*args: object, **kwargs: object) -> None:
        await original(*args, **kwargs)  # type: ignore[arg-type]
        h.github.fail_next("server_error")  # armed for the very next call: the note

    monkeypatch.setattr(h.github, "set_state", then_fail_the_note)
    issue = h.github.issue(42)
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "reworked"
    assert h.github.issue(42).state is StateLabel.REWORK
    assert h.github.comments_for(42) == []
    assert h.recorder.kinds == ["state_changed"]


# --- claim ----------------------------------------------------------------------------


async def test_claim_sets_in_progress_and_publishes(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    issue = h.github.add_issue("Task", labels=("bug", "issuebot/todo"), number=42)
    claimed = await claim(h.github, h.bus, issue)
    assert claimed is not None
    assert claimed.state is StateLabel.IN_PROGRESS
    assert claimed.labels == ("bug", "issuebot/in-progress")
    assert h.github.issue(42).state is StateLabel.IN_PROGRESS
    assert h.calls("set_state") == [(42, StateLabel.IN_PROGRESS)]
    assert h.recorder.kinds == ["state_changed"]
    event = h.recorder.events[0]
    assert isinstance(event, StateChanged)
    assert (event.from_label, event.to_label, event.actor) == (
        "issuebot/todo",
        "issuebot/in-progress",
        "issuebot",
    )


async def test_claim_clears_the_no_fault_marker(tmp_path: Path) -> None:
    """#34: the marker records what the *last* session concluded, so a new claim drops it.

    A reworked issue that an earlier session handed over as "no fault" must not stay labelled
    that way while this session works on it — otherwise a pull request this session merges
    would close a no-fault-labelled issue, and `classify_closed` could not trust the marker.
    """
    h = Harness(tmp_path)
    issue = h.github.add_issue(
        "Reported bug", labels=("bug", "issuebot/no-fault", "issuebot/rework"), number=42
    )
    claimed = await claim(h.github, h.bus, issue)
    assert claimed is not None
    assert claimed.labels == ("bug", "issuebot/in-progress")
    assert h.github.issue(42).labels == ("bug", "issuebot/in-progress")


async def test_claim_failure_returns_none_without_events(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    issue = h.github.add_issue("Task", labels=("issuebot/rework",), number=42)
    h.github.fail_next("transport")
    assert await claim(h.github, h.bus, issue) is None
    assert h.github.issue(42).state is StateLabel.REWORK
    assert h.recorder.events == []


# --- blocked escape -------------------------------------------------------------------


def test_blocked_block_content() -> None:
    labels = Settings.model_validate({"github": {"repo": "a/b"}}).github.labels
    text = blocked_block(CONTEXT, NOW, labels)
    assert text.splitlines() == [
        "### Issuebot blocked (2026-09-03T14:02:11Z)",
        "",
        "Turn budget exhausted: 5 turns in attempt 2 without reaching `issuebot/review`.",
        "Run `20260903T135501Z-a1b2c3` (attempt 2, 5 turns); "
        "logs: `/workspaces/example-42/.issuebot/runs/20260903T135501Z-a1b2c3`.",
        "Moved to `issuebot/review` for a human to look at.",
    ]


def test_blocked_block_singular_turn_without_logs() -> None:
    context = BlockedContext(reason="r.", run_id="run-1", attempt=1, turns=1, log_dir=None)
    labels = Settings.model_validate({"github": {"repo": "a/b"}}).github.labels
    assert "Run `run-1` (attempt 1, 1 turn)." in blocked_block(context, NOW, labels)


async def test_escape_appends_to_the_workpad_and_sets_review(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=42)
    h.github.open_pr(42, pr_number=43)
    workpad = await h.github.comment(42, f"{WORKPAD_MARKER}\n\n### Plan\n\n- [ ] 1. Do it\n")
    h.github.calls.clear()
    assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "applied"
    comments = h.github.comments_for(42)
    assert len(comments) == 1
    body = comments[0].body
    assert body.startswith(
        f"{WORKPAD_MARKER}\n\n### Plan\n\n- [ ] 1. Do it\n\n### Issuebot blocked"
    )
    assert body.endswith("Moved to `issuebot/review` for a human to look at.\n")
    assert h.calls("update_comment")[0][0] == workpad.id
    assert h.calls("comment") == []
    assert h.github.issue(42).state is StateLabel.REVIEW
    assert h.recorder.kinds == ["state_changed", "blocked"]
    changed = h.recorder.events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label, changed.actor) == (
        "issuebot/in-progress",
        "issuebot/review",
        "issuebot",
    )
    assert changed.pr_url == "https://github.com/example/repo/pull/43"
    blocked = h.recorder.events[1]
    assert isinstance(blocked, Blocked)
    assert blocked.reason == CONTEXT.reason


async def test_escape_creates_the_workpad_when_missing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=42)
    assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "applied"
    comments = h.github.comments_for(42)
    assert len(comments) == 1
    assert comments[0].body.startswith(f"{WORKPAD_MARKER}\n\n### Issuebot blocked (")
    assert h.calls("update_comment") == []
    assert h.github.issue(42).state is StateLabel.REVIEW


async def test_escape_writes_past_an_impostor_workpad(tmp_path: Path) -> None:
    """A marker comment by someone else is not where issuebot keeps its record (#77).

    Without the author check the block, and the run marker the escape's idempotence reads,
    would land in a comment its author can edit.
    """
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=42)
    impostor = h.github.add_comment(42, f"{WORKPAD_MARKER}\n\nyours truly", author="mallory")
    assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "applied"
    comments = h.github.comments_for(42)
    assert [c.author for c in comments] == ["mallory", "issuebot"]
    assert comments[0].body == impostor.body
    assert comments[1].body.startswith(f"{WORKPAD_MARKER}\n\n### Issuebot blocked (")
    assert h.calls("update_comment") == []
    # And the impostor cannot pre-empt the record by quoting the run id.
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=43)
    h.github.add_comment(43, f"{WORKPAD_MARKER}\n\nRun `{CONTEXT.run_id}` done", author="mallory")
    assert await blocked_escape(h.github, h.bus, "43", CONTEXT, now=NOW) == "applied"
    assert [c.author for c in h.github.comments_for(43)] == ["mallory", "issuebot"]


async def test_conflict_rework_counts_only_issuebots_own_workpad(tmp_path: Path) -> None:
    """Three bounce headings in someone else's comment do not use up the limit (#77)."""
    h = Harness(tmp_path)
    await seed(h)
    body = "\n\n".join(
        [WORKPAD_MARKER, *(conflict_block(51, n, 3, NOW, LABELS) for n in (1, 2, 3))]
    )
    h.github.add_comment(42, body, author="mallory")
    issue = h.github.issue(42)
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "reworked"
    assert h.github.issue(42).state is StateLabel.REWORK
    assert [c.author for c in h.github.comments_for(42)] == ["mallory", "issuebot"]
    assert "(bounce 1 of 3)" in h.github.comments_for(42)[1].body


async def test_conflict_rework_ignores_a_pull_request_that_is_not_issuebots(tmp_path: Path) -> None:
    """A contributor's conflicting PR that says ``Closes #42`` is not the issue's (#77)."""
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/review",), number=42)
    h.github.open_pr(42, pr_number=51, author="mallory")
    h.github.set_pr_mergeable(51, "conflicting")
    issue = h.github.issue(42)
    assert issue.linked_pr is None
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "failed"
    assert h.github.issue(42).state is StateLabel.REVIEW


@pytest.mark.parametrize("state", [StateLabel.REVIEW, StateLabel.TODO])
async def test_escape_is_a_no_op_when_the_issue_moved(tmp_path: Path, state: StateLabel) -> None:
    h = Harness(tmp_path)
    label = getattr(h.settings.github.labels, state.value)
    h.github.add_issue("Task", labels=(label,), number=42)
    assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "skipped"
    assert h.github.comments_for(42) == []
    assert h.calls("set_state") == []
    assert h.recorder.events == []


async def test_escape_is_a_no_op_for_closed_or_missing_issues(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=42)
    h.github.close_issue(42)
    assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "skipped"
    assert await blocked_escape(h.github, h.bus, "99", CONTEXT, now=NOW) == "skipped"
    assert h.recorder.events == []


def fail_on(h: Harness, method: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the next call of one adapter method raise, leaving the calls before it alone."""
    original = getattr(h.github, method)

    async def failing(*args: object, **kwargs: object) -> object:
        h.github.fail_next("transport")
        return await original(*args, **kwargs)

    monkeypatch.setattr(h.github, method, failing)


async def test_escape_is_idempotent_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=42)
    await h.github.comment(42, f"{WORKPAD_MARKER}\n\nnotes\n")
    # First run: the workpad is updated but the label write fails.
    with monkeypatch.context() as patch:
        fail_on(h, "set_state", patch)
        assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "failed"
    body_after_first = h.github.comments_for(42)[0].body
    assert body_after_first.count("### Issuebot blocked") == 1
    assert h.github.issue(42).state is StateLabel.IN_PROGRESS
    assert h.recorder.events == []
    # Second run: no second block, label set, events published.
    h.github.calls.clear()
    assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "applied"
    assert h.github.comments_for(42)[0].body == body_after_first
    assert h.calls("update_comment") == []
    assert h.github.issue(42).state is StateLabel.REVIEW
    assert h.recorder.kinds == ["state_changed", "blocked"]


async def test_escape_failure_on_the_workpad_write_leaves_the_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=42)
    fail_on(h, "comment", monkeypatch)
    assert await blocked_escape(h.github, h.bus, "42", CONTEXT, now=NOW) == "failed"
    assert h.github.issue(42).state is StateLabel.IN_PROGRESS
    assert h.github.comments_for(42) == []
    assert h.recorder.events == []


# --- finish_terminal ------------------------------------------------------------------


async def test_finish_terminal_completes_a_merged_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/review",), number=42)
    h.github.open_pr(42, pr_number=43)
    h.github.merge_pr(43)
    workspace = h.workspace_dir("repo-42")
    issue = h.github.issue(42)
    assert issue.github_state == "closed"
    assert await finish_terminal(h.github, h.bus, h.workspaces, issue) == "complete"
    assert h.github.issue(42).state is StateLabel.COMPLETE
    assert h.recorder.kinds == ["state_changed", "issue_completed"]
    changed = h.recorder.events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label, changed.actor) == (
        "issuebot/review",
        "issuebot/complete",
        "issuebot",
    )
    completed = h.recorder.events[1]
    assert isinstance(completed, IssueCompleted)
    assert completed.pr_url == "https://github.com/example/repo/pull/43"
    assert not workspace.exists()


async def test_finish_terminal_cancels_an_issue_closed_by_someone_elses_pull_request(
    tmp_path: Path,
) -> None:
    """issuebot's completion is issuebot's pull request (#77): a human's merged PR that closes
    the issue is a human's decision, recorded as a cancellation, not as issuebot's delivery."""
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/review",), number=42)
    h.github.open_pr(42, pr_number=43, author="mallory")
    h.github.merge_pr(43)
    issue = h.github.issue(42)
    assert issue.github_state == "closed" and issue.linked_pr is None
    assert await finish_terminal(h.github, h.bus, h.workspaces, issue) == "cancelled"
    assert h.github.issue(42).state is None
    assert h.recorder.kinds == ["state_changed", "issue_cancelled"]
    cancelled = h.recorder.events[1]
    assert isinstance(cancelled, IssueCancelled)
    assert cancelled.reason == CANCEL_REASON


async def test_finish_terminal_cancels_an_unmerged_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress", "bug"), number=42)
    h.github.close_issue(42)
    workspace = h.workspace_dir("repo-42")
    assert await finish_terminal(h.github, h.bus, h.workspaces, h.github.issue(42)) == "cancelled"
    assert h.github.issue(42).state is None
    assert h.github.issue(42).labels == ("bug",)
    assert h.recorder.kinds == ["state_changed", "issue_cancelled"]
    changed = h.recorder.events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label) == ("issuebot/in-progress", None)
    cancelled = h.recorder.events[1]
    assert isinstance(cancelled, IssueCancelled)
    assert cancelled.reason == CANCEL_REASON
    assert not workspace.exists()


async def test_finish_terminal_completes_a_no_fault_issue_without_a_pull_request(
    tmp_path: Path,
) -> None:
    """#33's other outcome: closing it is a resolution, not the abandonment #34 reported."""
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/review", "issuebot/no-fault"), number=42)
    h.github.close_issue(42)
    workspace = h.workspace_dir("repo-42")
    assert await finish_terminal(h.github, h.bus, h.workspaces, h.github.issue(42)) == "no_change"
    # It rests in `complete`, so it stays on the Kanban and the closed counts see it...
    assert h.github.issue(42).state is StateLabel.COMPLETE
    # ...and it keeps the marker, so the resolution is still legible on GitHub afterwards.
    assert "issuebot/no-fault" in h.github.issue(42).labels
    assert h.calls("clear_state") == []
    assert h.recorder.kinds == ["state_changed", "issue_completed"]
    changed = h.recorder.events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label) == ("issuebot/review", "issuebot/complete")
    completed = h.recorder.events[1]
    assert isinstance(completed, IssueCompleted)
    assert completed.resolution == "no_change"
    assert completed.pr_url is None
    assert not workspace.exists()


async def test_finish_terminal_prefers_a_merged_pr_over_the_no_fault_marker(
    tmp_path: Path,
) -> None:
    """A marker left over from an earlier session must not relabel a real fix as no-change."""
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/review", "issuebot/no-fault"), number=42)
    h.github.open_pr(42, pr_number=43)
    h.github.merge_pr(43)
    assert await finish_terminal(h.github, h.bus, h.workspaces, h.github.issue(42)) == "complete"
    completed = h.recorder.events[1]
    assert isinstance(completed, IssueCompleted)
    assert completed.resolution == "merged_pr"
    assert completed.pr_url == "https://github.com/example/repo/pull/43"


async def test_finish_terminal_counts_no_change_over_a_superseded_pull_request(
    tmp_path: Path,
) -> None:
    """#34: a rework whose next session found no fault, closed with the old PR still linked.

    The first session opened a pull request, a human closed it unmerged and asked for rework,
    and the second session concluded there was nothing to fix. `claim` cleared the marker before
    that session ran, so the marker on the issue is that session\'s verdict — the unmerged pull
    request beneath it is the attempt it superseded, and the close is a resolution.
    """
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/review", "issuebot/no-fault"), number=42)
    h.github.open_pr(42, pr_number=43)
    h.github.close_pr(43)
    h.github.close_issue(42)
    assert await finish_terminal(h.github, h.bus, h.workspaces, h.github.issue(42)) == "no_change"
    assert h.github.issue(42).state is StateLabel.COMPLETE
    assert "issuebot/no-fault" in h.github.issue(42).labels
    completed = h.recorder.events[1]
    assert isinstance(completed, IssueCompleted)
    assert completed.resolution == "no_change"


async def test_finish_terminal_leaves_a_completed_issue_alone(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/complete",), number=42)
    h.github.close_issue(42)
    workspace = h.workspace_dir("repo-42")
    h.github.calls.clear()
    assert await finish_terminal(h.github, h.bus, h.workspaces, h.github.issue(42)) == "unchanged"
    assert h.calls("set_state") == []
    assert h.calls("clear_state") == []
    assert h.recorder.events == []
    assert not workspace.exists()


async def test_finish_terminal_reports_github_failure_and_still_removes(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/in-progress",), number=42)
    h.github.close_issue(42)
    workspace = h.workspace_dir("repo-42")
    issue = h.github.issue(42)
    h.github.fail_next("transport")
    assert await finish_terminal(h.github, h.bus, h.workspaces, issue) == "failed"
    assert h.github.issue(42).state is StateLabel.IN_PROGRESS
    assert h.recorder.events == []
    assert not workspace.exists()


async def test_remove_workspace_contains_agent_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    assert await remove_workspace(h.workspaces, "repo-42") is False
    h.workspace_dir("repo-42")
    assert await remove_workspace(h.workspaces, "repo-42") is True

    async def refuse(identifier: str) -> bool:
        raise AgentError("workspace_error", f"cannot remove {identifier}")

    monkeypatch.setattr(h.workspaces, "remove", refuse)
    assert await remove_workspace(h.workspaces, "repo-42") is False


# --- the budget escape (#112) ---------------------------------------------------------------

BUDGET_REASON = "this issue has cost $12.50 over 4 runs and agent.max_issue_cost_usd is $10.00"


def _fails_once(method: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """Fail this one call and no other. `fail_next` is positional, and the read comes first."""
    calls = 0

    async def wrapper(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GitHubError("transport", "the escape could not move the label")
        await method(*args, **kwargs)

    return wrapper


def test_budget_block_names_the_reason_and_the_stamp() -> None:
    block = budget_block("spend", BUDGET_REASON, NOW, LABELS)
    assert block.startswith("### Issuebot budget limit (2026-09-03T14:02:11Z)\n\n")
    assert f"issuebot has stopped claiming this issue: {BUDGET_REASON}." in block
    assert "Moved to `issuebot/review` for a human to look at." in block


def test_budget_block_tells_each_ceiling_its_own_way_out() -> None:
    """The escape clears the failure chain on its way out, and cannot clear a cost."""
    attempts = budget_block("attempts", "3 sessions failed", NOW, LABELS)
    assert "label the issue `issuebot/rework` or `issuebot/todo`" in attempts
    assert "the next label move starts the run budget again" in attempts
    spend = budget_block("spend", BUDGET_REASON, NOW, LABELS)
    assert "Raise `agent.max_issue_cost_usd`" in spend
    assert "Relabelling on its own only brings the issue back here" in spend


def test_a_budget_block_is_matched_by_its_reason_not_its_heading(tmp_path: Path) -> None:
    """Both ceilings share the heading, so the reason is what makes two blocks the same one.

    A block for a different reason -- the other ceiling, a raised one, a higher spend after
    another run -- is a different escalation, and its way out is different too; a session
    quoting the reason in its own prose is neither, so the check is line-anchored.
    """
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/todo",), number=42)
    block = budget_block("spend", BUDGET_REASON, NOW, h.github.labels)
    assert _has_budget_block(f"{WORKPAD_MARKER}\n\n{block}\n", BUDGET_REASON)
    assert not _has_budget_block(f"{WORKPAD_MARKER}\n\n{block}\n", "some other reason")
    said = f"{BUDGET_REASON_PREFIX}{BUDGET_REASON}."
    quoted = f"{WORKPAD_MARKER}\n\n### Notes\n\n- it said `{said}` last time\n"
    assert not _has_budget_block(quoted, BUDGET_REASON)
    assert BUDGET_HEADING in block  # still the heading a human and the docs look for


@pytest.mark.parametrize("state", ["todo", "rework", "in-progress"])
async def test_budget_escape_hands_over_an_issue_in_any_claimable_state(
    tmp_path: Path, state: str
) -> None:
    """The gate refuses before the claim, so the issue is rarely the one `blocked_escape` takes."""
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=(f"issuebot/{state}",), number=42)
    await h.github.comment(42, f"{WORKPAD_MARKER}\n\n### Plan\n\n- [ ] 1. Do it\n")
    h.github.calls.clear()
    assert await budget_escape(h.github, h.bus, "42", "spend", BUDGET_REASON, now=NOW) == "applied"
    body = h.github.comments_for(42)[0].body
    assert BUDGET_HEADING in body
    assert h.github.issue(42).state is StateLabel.REVIEW
    assert h.recorder.kinds == ["state_changed", "blocked"]
    assert h.recorder.events[1].reason == BUDGET_REASON  # type: ignore[attr-defined]


async def test_budget_escape_creates_the_workpad_when_there_is_none(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/todo",), number=42)
    assert await budget_escape(h.github, h.bus, "42", "spend", BUDGET_REASON, now=NOW) == "applied"
    body = h.github.comments_for(42)[0].body
    assert body.startswith(f"{WORKPAD_MARKER}\n\n{BUDGET_HEADING}")


async def test_an_unannounced_budget_escape_returns_the_issue_without_a_blocked_event(
    tmp_path: Path,
) -> None:
    """`announce=False` is the caller saying it has already told everyone about this one.

    The label move is published either way -- the issue really did come back from `rework`,
    and the board should say so -- but a second `Blocked` would report a second escalation to
    Slack and to the dashboard's blocked tile when only one was ever made.
    """
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/todo",), number=42)
    assert await budget_escape(h.github, h.bus, "42", "spend", BUDGET_REASON, now=NOW) == "applied"
    h.github.human_set_state(42, StateLabel.REWORK)
    outcome = await budget_escape(
        h.github, h.bus, "42", "spend", BUDGET_REASON, now=NOW, announce=False
    )
    assert outcome == "skipped"
    assert h.github.comments_for(42)[0].body.count(BUDGET_HEADING) == 1
    assert h.github.issue(42).state is StateLabel.REVIEW
    assert h.recorder.kinds == ["state_changed", "blocked", "state_changed"]


async def test_a_budget_escape_that_could_not_move_the_issue_still_announces_on_retry(
    tmp_path: Path,
) -> None:
    """The block landing and the event going out are two writes with a failure between them.

    A `set_state` that fails leaves the block behind and publishes nothing, so the escalation
    has not been announced and the tick that retries it must still announce it -- which is why
    the caller cannot read that decision off the workpad.
    """
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/todo",), number=42)
    ok = h.github.set_state
    h.github.set_state = _fails_once(ok)  # type: ignore[method-assign]
    assert await budget_escape(h.github, h.bus, "42", "spend", BUDGET_REASON, now=NOW) == "failed"
    assert h.recorder.kinds == []
    assert h.github.comments_for(42)[0].body.count(BUDGET_HEADING) == 1
    h.github.set_state = ok  # type: ignore[method-assign]
    assert await budget_escape(h.github, h.bus, "42", "spend", BUDGET_REASON, now=NOW) == "applied"
    assert h.recorder.kinds == ["state_changed", "blocked"]
    assert h.github.comments_for(42)[0].body.count(BUDGET_HEADING) == 1  # not written twice


async def test_a_second_ceiling_gets_its_own_block_and_its_own_announcement(
    tmp_path: Path,
) -> None:
    """The two ceilings name different ways out, so the `attempts` block cannot stand in.

    An issue escalated on attempts, recovered by a human and run again until it exceeds
    `agent.max_issue_cost_usd` is a new escalation with a different remedy; a block matched on
    the shared heading would leave it with the wrong one and nothing said anywhere.
    """
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/todo",), number=42)
    assert await budget_escape(h.github, h.bus, "42", "attempts", "3 runs", now=NOW) == "applied"
    h.github.human_set_state(42, StateLabel.REWORK)
    assert await budget_escape(h.github, h.bus, "42", "spend", BUDGET_REASON, now=NOW) == "applied"
    body = h.github.comments_for(42)[0].body
    assert body.count(BUDGET_HEADING) == 2
    assert "Raise `agent.max_issue_cost_usd`" in body  # the spend block's own way out
    assert h.recorder.kinds == ["state_changed", "blocked", "state_changed", "blocked"]


@pytest.mark.parametrize("state", ["review", "complete"])
async def test_budget_escape_skips_an_issue_it_does_not_claim(tmp_path: Path, state: str) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=(f"issuebot/{state}",), number=42)
    h.github.calls.clear()
    assert await budget_escape(h.github, h.bus, "42", "spend", BUDGET_REASON, now=NOW) == "skipped"
    assert h.calls("set_state") == []
    assert h.recorder.kinds == []


async def test_budget_escape_skips_a_closed_or_missing_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/todo",), number=42)
    h.github.close_issue(42)
    assert await budget_escape(h.github, h.bus, "42", "spend", BUDGET_REASON, now=NOW) == "skipped"
    assert await budget_escape(h.github, h.bus, "99", "spend", BUDGET_REASON, now=NOW) == "skipped"


async def test_budget_escape_reports_a_github_failure(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/todo",), number=42)
    h.github.fail_next("transport")
    assert await budget_escape(h.github, h.bus, "42", "spend", BUDGET_REASON, now=NOW) == "failed"
    assert h.github.issue(42).state is StateLabel.TODO
    assert h.recorder.kinds == []
