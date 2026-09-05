"""Tests for PostgresStore against a real database (skipped without DATABASE_URL)."""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from issuebot.agent.turnlog import TurnCapture
from issuebot.config import GitHubLabels
from issuebot.db import StoreError, StoreUnavailableError, connect, migrate
from issuebot.db.store import IssueSnapshot, PostgresStore
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
from issuebot.github import Issue, LinkedPr, StateLabel

T0 = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
async def store(db_url: str) -> AsyncIterator[PostgresStore]:
    await migrate(db_url)
    store = PostgresStore(db_url, labels=GitHubLabels())
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


async def rows(db_url: str, query: str, *params: Any) -> list[dict[str, Any]]:
    conn = await connect(db_url)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(query, params or None)
            names = [column.name for column in cursor.description or []]
            return [dict(zip(names, row, strict=True)) for row in await cursor.fetchall()]
    finally:
        await conn.close()


def started(**overrides: Any) -> RunStarted:
    fields: dict[str, Any] = {
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "run_id": "run-1",
        "attempt": 2,
        "session_id": "sess-1",
        "workspace_path": "/workspaces/repo-42",
        "at": at(0),
    }
    fields.update(overrides)
    return RunStarted(**fields)


def ended(**overrides: Any) -> RunEnded:
    fields: dict[str, Any] = {
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "run_id": "run-1",
        "outcome": "succeeded",
        "error": None,
        "turns": 3,
        "input_tokens": 1000,
        "output_tokens": 50,
        "cost_usd": 0.75,
        "duration_s": 90.0,
        "log_dir": "/workspaces/repo-42/.issuebot/runs/run-1",
        "at": at(90),
    }
    fields.update(overrides)
    return RunEnded(**fields)


def capture(turn_number: int, **overrides: Any) -> TurnCapture:
    fields: dict[str, Any] = {
        "turn_number": turn_number,
        "model": "claude-opus-5",
        "subtype": "success",
        "is_error": False,
        "num_turns": 19,
        "input_tokens": 38,
        "cache_creation_input_tokens": 23100,
        "cache_read_input_tokens": 490200,
        "output_tokens": 8425,
        "cost_usd": 0.8976,
        "duration_ms": 201719,
        "result_text": "Done.",
        "prompt": "You are working on issue #42.",
        "prompt_bytes": 29,
        "stream": '{"type":"result"}\n',
        "stream_bytes": 18,
        "stream_lines": 1,
        "omitted_lines": 0,
        "stderr": "",
        "stderr_bytes": 0,
        "truncated": False,
    }
    fields.update(overrides)
    return TurnCapture(**fields)


def moved(**overrides: Any) -> StateChanged:
    fields: dict[str, Any] = {
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "from_label": "issuebot/todo",
        "to_label": "issuebot/in-progress",
        "actor": "issuebot",
        "at": at(10),
    }
    fields.update(overrides)
    return StateChanged(**fields)


# --- runs -------------------------------------------------------------------------------------


async def test_run_started_then_ended_fills_one_row(store: PostgresStore, db_url: str) -> None:
    await store.apply_event(started())
    await store.apply_event(ended())
    (row,) = await rows(db_url, "SELECT * FROM runs")
    assert row["run_id"] == "run-1"
    assert (row["issue_number"], row["issue_identifier"], row["attempt"]) == (42, "repo-42", 2)
    assert (row["session_id"], row["workspace_path"]) == ("sess-1", "/workspaces/repo-42")
    assert (row["started_at"], row["ended_at"]) == (at(0), at(90))
    assert (row["outcome"], row["error"], row["turns"]) == ("succeeded", None, 3)
    assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (1000, 50, 0.75)
    assert row["duration_s"] == 90.0
    assert row["log_dir"] == "/workspaces/repo-42/.issuebot/runs/run-1"


async def test_run_ended_before_started_gives_the_same_row(
    store: PostgresStore, db_url: str
) -> None:
    await store.apply_event(ended())
    await store.apply_event(started(at=at(2)))
    (row,) = await rows(db_url, "SELECT * FROM runs")
    assert (row["attempt"], row["session_id"], row["started_at"]) == (2, "sess-1", at(2))
    assert (row["outcome"], row["ended_at"], row["cost_usd"]) == ("succeeded", at(90), 0.75)


async def test_run_ended_alone_computes_the_start(store: PostgresStore, db_url: str) -> None:
    await store.apply_event(ended(outcome="failed", error="turn_failed: boom"))
    (row,) = await rows(db_url, "SELECT * FROM runs")
    assert row["started_at"] == at(0)
    assert (row["attempt"], row["session_id"], row["workspace_path"]) == (0, None, None)
    assert (row["outcome"], row["error"]) == ("failed", "turn_failed: boom")


async def test_run_ended_with_captures_writes_run_turns(store: PostgresStore, db_url: str) -> None:
    turns = [capture(1), capture(2, subtype=None, num_turns=None, cost_usd=None, truncated=True)]
    await store.apply_event(ended(), turns=turns)
    first, second = await rows(db_url, "SELECT * FROM run_turns ORDER BY turn_number")
    assert (first["run_id"], first["turn_number"], first["model"]) == ("run-1", 1, "claude-opus-5")
    assert (first["subtype"], first["is_error"], first["num_turns"]) == ("success", False, 19)
    assert (first["input_tokens"], first["cache_creation_input_tokens"]) == (38, 23100)
    assert (first["cache_read_input_tokens"], first["output_tokens"]) == (490200, 8425)
    assert (first["cost_usd"], first["duration_ms"], first["result_text"]) == (
        0.8976,
        201719,
        "Done.",
    )
    assert (first["prompt"], first["prompt_bytes"]) == ("You are working on issue #42.", 29)
    assert (first["stream"], first["stream_bytes"], first["stream_lines"]) == (
        '{"type":"result"}\n',
        18,
        1,
    )
    assert (first["omitted_lines"], first["stderr"], first["stderr_bytes"]) == (0, "", 0)
    assert first["truncated"] is False
    assert first["captured_at"] is not None
    assert (second["turn_number"], second["subtype"], second["num_turns"]) == (2, None, None)
    assert (second["cost_usd"], second["truncated"]) == (None, True)


async def test_run_turns_are_idempotent_on_a_retried_event(
    store: PostgresStore, db_url: str
) -> None:
    await store.apply_event(ended(), turns=[capture(1)])
    await store.apply_event(ended(), turns=[capture(1, result_text="Done again.")])
    (row,) = await rows(db_url, "SELECT result_text FROM run_turns")
    assert row["result_text"] == "Done again."
    assert len(await rows(db_url, "SELECT id FROM events")) == 2


async def test_run_ended_without_captures_writes_no_turns(
    store: PostgresStore, db_url: str
) -> None:
    await store.apply_event(ended())
    assert await rows(db_url, "SELECT * FROM run_turns") == []


async def test_captures_are_ignored_for_other_kinds(store: PostgresStore, db_url: str) -> None:
    await store.apply_event(started(), turns=[capture(1)])
    assert await rows(db_url, "SELECT * FROM run_turns") == []
    assert len(await rows(db_url, "SELECT * FROM runs")) == 1


async def test_an_empty_workspace_path_is_stored_as_null(store: PostgresStore, db_url: str) -> None:
    await store.apply_event(started(workspace_path=""))
    (row,) = await rows(db_url, "SELECT workspace_path FROM runs")
    assert row["workspace_path"] is None


# --- events -----------------------------------------------------------------------------------


EVERY_KIND: list[Event] = [
    moved(),
    started(),
    ended(),
    PrOpened(issue_number=42, issue_identifier="repo-42", pr_number=7, pr_url="u", at=at(1)),
    Blocked(issue_number=42, issue_identifier="repo-42", reason="stuck", at=at(2)),
    IssueCompleted(issue_number=42, issue_identifier="repo-42", pr_url="u", at=at(3)),
    IssueCancelled(issue_number=42, issue_identifier="repo-42", reason="closed", at=at(4)),
    NotificationSent(
        issue_number=42, issue_identifier="repo-42", channel="slack", about_kind="blocked", at=at(5)
    ),
]


async def test_every_kind_lands_in_events_with_its_payload(
    store: PostgresStore, db_url: str
) -> None:
    for event in EVERY_KIND:
        await store.apply_event(event)
    found = await rows(db_url, "SELECT * FROM events ORDER BY id")
    assert [row["kind"] for row in found] == [event.kind for event in EVERY_KIND]
    assert {row["issue_number"] for row in found} == {42}
    assert [row["run_id"] for row in found[:3]] == [None, "run-1", "run-1"]
    assert found[0]["at"] == at(10)
    assert found[0]["payload"] == moved().to_dict()
    assert found[2]["payload"]["log_dir"] == "/workspaces/repo-42/.issuebot/runs/run-1"
    assert found[4]["payload"] == {
        "kind": "blocked",
        "at": at(2).isoformat(),
        "issue_number": 42,
        "issue_identifier": "repo-42",
        "reason": "stuck",
    }


# --- issues -----------------------------------------------------------------------------------


def snapshot(issue: Issue, seconds: float) -> IssueSnapshot:
    return IssueSnapshot(issue=issue, seen_at=at(seconds))


async def test_upsert_issues_inserts_and_keeps_the_newer_snapshot(
    store: PostgresStore, db_url: str, make_issue: Callable[..., Issue]
) -> None:
    pr = LinkedPr(number=7, url="https://x/pull/7", state="merged", merged_at=at(50))
    first = make_issue(title="First", labels=("issuebot/todo", "bug"))
    newer = make_issue(
        title="Renamed",
        state=StateLabel.REVIEW,
        state_labels=("issuebot/review",),
        labels=("issuebot/review", "bug"),
        linked_pr=pr,
        updated_at=at(40),
    )
    await store.upsert_issues([snapshot(first, 0)])
    await store.upsert_issues([snapshot(newer, 20), snapshot(first, 5)])
    (row,) = await rows(db_url, "SELECT * FROM issues")
    assert (row["number"], row["identifier"], row["title"]) == (42, "repo-42", "Renamed")
    assert (row["state"], row["state_label"]) == ("review", "issuebot/review")
    assert (row["github_state"], row["labels"]) == ("open", ["issuebot/review", "bug"])
    assert (row["pr_number"], row["pr_url"], row["pr_state"]) == (7, "https://x/pull/7", "merged")
    assert row["pr_merged_at"] == at(50)
    assert (row["updated_at"], row["closed_at"], row["seen_at"]) == (at(40), None, at(20))


async def test_upsert_issues_handles_conflict_and_unlabelled_issues(
    store: PostgresStore, db_url: str, make_issue: Callable[..., Issue]
) -> None:
    conflict = make_issue(
        number=1,
        identifier="repo-1",
        state=None,
        state_labels=("issuebot/todo", "issuebot/rework"),
        labels=("issuebot/todo", "issuebot/rework"),
    )
    bare = make_issue(number=2, identifier="repo-2", state=None, state_labels=(), labels=())
    await store.upsert_issues([snapshot(conflict, 0), snapshot(bare, 0)])
    found = await rows(db_url, "SELECT number, state, state_label FROM issues ORDER BY number")
    assert found == [
        {"number": 1, "state": None, "state_label": None},
        {"number": 2, "state": None, "state_label": None},
    ]


async def test_upsert_issues_with_nothing_is_a_noop(store: PostgresStore) -> None:
    await store.upsert_issues([])


async def test_state_changed_updates_the_row_when_newer(
    store: PostgresStore, db_url: str, make_issue: Callable[..., Issue]
) -> None:
    await store.upsert_issues([snapshot(make_issue(), 0)])
    await store.apply_event(moved(at=at(10)))
    (row,) = await rows(db_url, "SELECT state, state_label, seen_at FROM issues")
    assert row == {"state": "in_progress", "state_label": "issuebot/in-progress", "seen_at": at(10)}
    await store.apply_event(moved(to_label="issuebot/review", at=at(5)))  # older: ignored
    (row,) = await rows(db_url, "SELECT state, seen_at FROM issues")
    assert row == {"state": "in_progress", "seen_at": at(10)}
    await store.apply_event(moved(to_label="Issuebot/Review", actor="agent", at=at(20)))
    (row,) = await rows(db_url, "SELECT state, state_label FROM issues")
    assert row == {"state": "review", "state_label": "Issuebot/Review"}
    await store.apply_event(moved(to_label="unrelated", actor="human", at=at(30)))
    (row,) = await rows(db_url, "SELECT state, state_label FROM issues")
    assert row == {"state": None, "state_label": "unrelated"}
    await store.apply_event(moved(to_label=None, at=at(40)))
    (row,) = await rows(db_url, "SELECT state, state_label FROM issues")
    assert row == {"state": None, "state_label": None}


async def test_state_changed_for_an_unknown_issue_is_harmless(
    store: PostgresStore, db_url: str
) -> None:
    await store.apply_event(moved())
    assert await rows(db_url, "SELECT * FROM issues") == []
    assert len(await rows(db_url, "SELECT * FROM events")) == 1


async def test_completed_and_cancelled_close_the_row(
    store: PostgresStore, db_url: str, make_issue: Callable[..., Issue]
) -> None:
    await store.upsert_issues([snapshot(make_issue(), 0)])
    await store.apply_event(
        IssueCompleted(issue_number=42, issue_identifier="repo-42", pr_url=None, at=at(10))
    )
    (row,) = await rows(db_url, "SELECT github_state, closed_at, seen_at FROM issues")
    assert row == {"github_state": "closed", "closed_at": at(10), "seen_at": at(10)}
    await store.upsert_issues([snapshot(make_issue(closed_at=at(8), github_state="closed"), 20)])
    await store.apply_event(
        IssueCancelled(issue_number=42, issue_identifier="repo-42", reason="x", at=at(30))
    )
    (row,) = await rows(db_url, "SELECT github_state, closed_at, seen_at FROM issues")
    assert row == {"github_state": "closed", "closed_at": at(8), "seen_at": at(30)}


# --- snapshot ---------------------------------------------------------------------------------


async def test_write_snapshot_keeps_exactly_one_row(store: PostgresStore, db_url: str) -> None:
    await store.write_snapshot(at(0), {"tick_count": 1, "running": []})
    await store.write_snapshot(at(30), {"tick_count": 2, "running": [{"issue_number": 1}]})
    (row,) = await rows(db_url, "SELECT at, written_at, data FROM runtime_snapshot")
    assert row["at"] == at(30)
    assert row["data"] == {"tick_count": 2, "running": [{"issue_number": 1}]}
    assert row["written_at"].tzinfo is not None
    assert row["written_at"] > at(30)


# --- errors -----------------------------------------------------------------------------------


async def test_writes_need_a_connection(db_url: str) -> None:
    await migrate(db_url)
    store = PostgresStore(db_url, labels=GitHubLabels())
    with pytest.raises(StoreUnavailableError, match="not connected"):
        await store.apply_event(moved())
    await store.connect()
    assert store.connected
    await store.close()
    assert not store.connected
    with pytest.raises(StoreUnavailableError, match="not connected"):
        await store.write_snapshot(at(0), {})


async def test_a_bad_value_is_a_store_error_not_an_outage(store: PostgresStore) -> None:
    with pytest.raises(StoreError, match="TypeError"):
        await store.write_snapshot(at(0), {"bad": {1, 2}})
    assert store.connected
    await store.write_snapshot(at(0), {"ok": True})


async def test_a_missing_table_is_a_store_error(db_url: str) -> None:
    store = PostgresStore(db_url, labels=GitHubLabels())
    await store.connect()
    try:
        with pytest.raises(StoreError, match="UndefinedTable"):
            await store.write_snapshot(at(0), {})
    finally:
        await store.close()


async def test_connect_failure_is_unavailable_and_redacted() -> None:
    store = PostgresStore("postgresql://u:s3cret@127.0.0.1:1/db", labels=GitHubLabels())
    with pytest.raises(StoreUnavailableError) as exc:
        await store.connect()
    assert "s3cret" not in exc.value.message
    assert not store.connected
