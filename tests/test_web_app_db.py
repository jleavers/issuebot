"""The API contract and the pages against a seeded database (skipped without DATABASE_URL)."""

from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from issuebot.agent.turnlog import capture_turns
from issuebot.config import GitHubLabels
from issuebot.db import Database, migrate
from issuebot.db.store import IssueSnapshot, PostgresStore
from issuebot.events import RunEnded, RunStarted, StateChanged
from issuebot.github import Issue, LinkedPr, StateLabel
from issuebot.orchestrator.state import ClaudeTotals, Counters, RuntimeSnapshot
from issuebot.web import create_app

SAMPLE = Path(__file__).parent / "fixtures" / "runs" / "20260904T202535Z-0964cd"
RUN_ID = "20260904T202535Z-0964cd"
NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
REPO = "example/repo"
BASE = "/r/example/repo"
API = "/api/v1/repos/example/repo"


@pytest.fixture
async def seeded(db_url: str, make_issue: Callable[..., Issue]) -> AsyncIterator[Database]:
    """Three issues, one finished run on #7 with the sample turn captured twice, a snapshot."""
    await migrate(db_url)
    store = PostgresStore(db_url, repo=REPO, labels=GitHubLabels())
    await store.connect()

    def issue(number: int, state: StateLabel, **overrides: Any) -> Issue:
        fields: dict[str, Any] = {
            "number": number,
            "identifier": f"repo-{number}",
            "title": f"Issue {number} <b>title</b>",
            "state": state,
            "state_labels": (f"issuebot/{state.value.replace('_', '-')}",),
            "labels": (f"issuebot/{state.value.replace('_', '-')}",),
            "updated_at": NOW - number * HOUR,
        }
        fields.update(overrides)
        return make_issue(**fields)

    pr = LinkedPr(
        number=8, url="https://github.com/example/repo/pull/8", state="open", merged_at=None
    )
    issues = [
        issue(1, StateLabel.TODO),
        issue(7, StateLabel.REVIEW, linked_pr=pr),
        issue(10, StateLabel.COMPLETE, github_state="closed", closed_at=NOW - HOUR),
    ]
    await store.upsert_issues([IssueSnapshot(issue=i, seen_at=NOW - 2 * HOUR) for i in issues])
    await store.apply_event(
        RunStarted(
            issue_number=7,
            issue_identifier="repo-7",
            run_id=RUN_ID,
            attempt=1,
            session_id="sess-7",
            workspace_path="/workspaces/repo-7",
            at=NOW - 30 * timedelta(minutes=1),
        )
    )
    (capture,) = capture_turns(SAMPLE)
    second = replace(capture, turn_number=2)
    await store.apply_event(
        RunEnded(
            issue_number=7,
            issue_identifier="repo-7",
            run_id=RUN_ID,
            outcome="succeeded",
            error=None,
            turns=2,
            input_tokens=1026676,
            output_tokens=16850,
            cost_usd=1.7952,
            duration_s=410.0,
            log_dir=str(SAMPLE),
            at=NOW - 23 * timedelta(minutes=1),
        ),
        turns=[capture, second],
    )
    await store.apply_event(
        StateChanged(
            issue_number=7,
            issue_identifier="repo-7",
            from_label="issuebot/in-progress",
            to_label="issuebot/review",
            actor="agent",
            pr_url=pr.url,
            at=NOW - 22 * timedelta(minutes=1),
        )
    )
    snapshot = RuntimeSnapshot(
        at=NOW,
        workflow_path="/configs/WORKFLOW.md",
        workflow_mtime_ns=1,
        config_valid=True,
        config_error=None,
        dispatch_hold=None,
        poll_interval_ms=30_000,
        max_concurrent_agents=2,
        tick_count=12,
        last_tick_at=NOW,
        running=(),
        retrying=(),
        totals=ClaudeTotals(input_tokens=1026676, output_tokens=16850, cost_usd=1.7952),
        counters=Counters(runs_started=1, runs_ended=1),
    )
    await store.write_snapshot(snapshot.at, snapshot.to_dict())
    await store.close()
    database = Database(db_url)
    await database.register_repo(REPO, GitHubLabels(), "/configs/WORKFLOW.md")
    yield database


@pytest.fixture
def client(seeded: Database) -> TestClient:
    return TestClient(create_app(seeded))


async def test_the_issue_api_lists_the_run_and_its_turns(client: TestClient) -> None:
    response = client.get(f"{API}/issues/7")
    assert response.status_code == 200
    body = response.json()
    assert (body["issue"]["state"], body["issue"]["pr_number"]) == ("review", 8)
    (run,) = body["runs"]
    assert (run["run_id"], run["outcome"], run["turns"]) == (RUN_ID, "succeeded", 2)
    captured = run["captured_turns"]
    assert [turn["turn_number"] for turn in captured] == [1, 2]
    assert captured[0]["model"] == "claude-opus-5" and captured[0]["num_turns"] == 19
    assert captured[0]["url"] == f"{BASE}/issues/7/runs/{RUN_ID}/turns/1"
    assert [log["turn_number"] for log in body["logs"]] == [1, 2]
    assert [event["kind"] for event in body["recent_events"]] == [
        "state_changed",
        "run_ended",
        "run_started",
    ]
    assert body["running"] is None and body["retry"] is None


async def test_the_turn_page_and_the_raw_stream_come_from_run_turns(client: TestClient) -> None:
    page = client.get(f"{BASE}/issues/7/runs/{RUN_ID}/turns/2")
    assert page.status_code == 200
    assert "turn 2 of 2" in page.text and "Bash" in page.text and "claude-opus-5" in page.text
    raw = client.get(f"{BASE}/issues/7/runs/{RUN_ID}/turns/1/stream")
    assert raw.status_code == 200
    assert raw.text == (SAMPLE / "turn-1.jsonl").read_text(encoding="utf-8")
    prompt = client.get(f"{BASE}/issues/7/runs/{RUN_ID}/turns/1/prompt")
    assert prompt.text == (SAMPLE / "turn-1.prompt.md").read_text(encoding="utf-8")


async def test_stats_match_the_query_module(client: TestClient, seeded: Database) -> None:
    body = client.get(f"{API}/stats?window=7d").json()
    async with seeded.queries() as queries:
        q = queries.scoped(REPO)
        closed = await q.closed_count(timedelta(days=7))
        runs = await q.runs_count(timedelta(days=7))
        counts = await q.state_counts()
        series = await q.daily_series(7)
    assert (body["closed"], body["runs"]) == (closed, runs) == (1, 1)
    assert body["by_state"] == counts
    assert counts == {"todo": 1, "in_progress": 0, "review": 1, "rework": 0, "complete": 1}
    assert len(body["series"]) == len(series) == 7
    assert body["series"][-1]["runs"] == series[-1].runs


async def test_the_dashboard_renders_the_seeded_rows(client: TestClient) -> None:
    response = client.get(f"{BASE}/")
    assert response.status_code == 200
    for label in GitHubLabels().as_tuple():
        assert label in response.text
    assert (
        "Issue 7 &lt;b&gt;title&lt;/b&gt;" in response.text and "<b>title</b>" not in response.text
    )
    assert f'href="{BASE}/issues/7"' in response.text
    assert f'href="{BASE}/issues/10"' in response.text
    assert "PR#8" in response.text
    assert 'class="panel worker ok"' in response.text and "tick 12" in response.text


async def test_state_and_healthz_read_the_snapshot(client: TestClient) -> None:
    state = client.get(f"{API}/state").json()
    assert state["worker"]["tick_count"] == 12 and state["worker"]["stale"] is False
    assert state["claude_totals"]["total_tokens"] == 1026676 + 16850
    assert state["counters"]["runs_ended"] == 1
    health = client.get("/healthz").json()
    assert (health["status"], health["database"], health["worker"]) == ("ok", "ok", "ok")
    assert client.post(f"{API}/refresh").status_code == 202


async def test_unknown_issue_is_a_404_against_the_real_database(client: TestClient) -> None:
    assert client.get(f"{API}/issues/99").json()["error"]["code"] == "unknown_issue"
    assert client.get(f"{BASE}/issues/99").status_code == 404
