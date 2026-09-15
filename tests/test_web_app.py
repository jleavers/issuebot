"""Tests for the web app's JSON API and health check against a FakeDatabase (hermetic)."""

import sys
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient

from fakes.web import (
    API,
    BASE,
    NOW,
    PASSWORD,
    REFRESH_HEADERS,
    REPO,
    RUN_ID,
    Harness,
    basic_auth,
    event_row,
    issue_row,
    limits,
    repo_row,
    retry_row,
    running_row,
    snapshot,
)
from issuebot.config import GitHubLabels
from issuebot.db import MAX_WINDOW_DAYS, StoreUnavailableError
from issuebot.db.queries import DailyPoint
from issuebot.orchestrator.state import DispatchHold
from issuebot.web import REFRESH_MIN_INTERVAL_S, SECURITY_HEADERS, STALE_FACTOR
from issuebot.web.views import (
    api_base,
    cost_label,
    describe_event,
    dispatch_hold,
    limits_unavailable,
    rate_limit_windows,
    repo_base,
    repo_labels,
    safe_href,
    switch_target,
    window_days,
    worker_status,
    worst_status,
)

HELD_SINCE = NOW - timedelta(minutes=4)
HOLD = DispatchHold(
    kind="auth",
    reason="claude authentication unavailable: not logged in",
    since=HELD_SINCE,
)


@pytest.fixture
def h() -> Iterator[Harness]:
    harness = Harness()
    with harness.client:
        yield harness


# --- /api/v1/state ---------------------------------------------------------------------------


def test_state_reshapes_the_snapshot(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(running=(running_row(),), retrying=(retry_row(),))
    response = h.client.get(f"{API}/state")
    assert response.status_code == 200
    body = response.json()
    assert body["generated_at"] == (NOW - timedelta(seconds=6)).isoformat()
    assert body["written_at"] == (NOW - timedelta(seconds=5)).isoformat()
    assert body["snapshot_age_s"] == 5.0
    assert body["worker"] == {
        "tick_count": 41,
        "last_tick_at": (NOW - timedelta(seconds=6)).isoformat(),
        "poll_interval_ms": 30000,
        "max_concurrent_agents": 2,
        "workflow_path": "/configs/WORKFLOW.md",
        "workflow_overlay_path": None,
        "config_valid": True,
        "config_error": None,
        "dispatch_hold": None,
        "status": "ok",
        "stale": False,
    }
    assert body["counts"] == {"running": 1, "retrying": 1}
    (running,) = body["running"]
    assert running == {
        "issue_id": "7",
        "issue_identifier": "repo-7",
        "issue_number": 7,
        "issue_url": "https://github.com/example/repo/issues/7",
        "title": "Add a power function",
        "state": "in_progress",
        "run_id": RUN_ID,
        "session_id": "sess-7",
        "attempt": 1,
        "rework": False,
        "resumed": False,
        "turn_count": 1,
        "last_event": "turn_activity",
        "started_at": (NOW - timedelta(minutes=3)).isoformat(),
        "last_event_at": (NOW - timedelta(seconds=20)).isoformat(),
        "stop_cause": None,
    }
    (retrying,) = body["retrying"]
    assert retrying == {
        "issue_id": "9",
        "issue_identifier": "repo-9",
        "issue_number": 9,
        "issue_url": "https://github.com/example/repo/issues/9",
        "title": "Retry the flaky import",
        "attempt": 2,
        "kind": "failure",
        "due_at": (NOW + timedelta(seconds=20)).isoformat(),
        "error": "turn_failed: boom",
    }
    assert body["claude_totals"] == {
        "input_tokens": 1000,
        "output_tokens": 50,
        "total_tokens": 1050,
        "cost_usd": 1.25,
        "seconds_running": 90.0,
    }
    assert body["counters"] == {
        "runs_started": 3,
        "runs_ended": 2,
        "issues_completed": 1,
        "issues_cancelled": 0,
        "blocked": 0,
    }


def test_state_names_the_overlay_in_force(h: Harness) -> None:
    """The API's answer to "is the worker running my overrides?"."""
    h.queries.snapshot_row = snapshot(workflow_overlay_path="/configs/WORKFLOW.local.md")
    worker = h.client.get(f"{API}/state").json()["worker"]
    assert worker["workflow_overlay_path"] == "/configs/WORKFLOW.local.md"


def test_state_without_a_snapshot_is_empty_not_missing(h: Harness) -> None:
    body = h.client.get(f"{API}/state").json()
    assert (body["generated_at"], body["written_at"], body["snapshot_age_s"]) == (None, None, None)
    assert body["worker"] is None
    assert (body["counts"], body["running"], body["retrying"]) == (
        {"running": 0, "retrying": 0},
        [],
        [],
    )
    assert body["claude_totals"]["total_tokens"] == 0 and body["counters"]["blocked"] == 0


def test_state_marks_a_stale_worker(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(age_s=STALE_FACTOR * 30 + 1)
    assert h.client.get(f"{API}/state").json()["worker"]["stale"] is True


# --- /api/v1/issues/{number} -----------------------------------------------------------------


def test_issue_document(h: Harness) -> None:
    h.seed_issue()
    h.queries.snapshot_row = snapshot(running=(running_row(),))
    response = h.client.get(f"{API}/issues/7")
    assert response.status_code == 200
    body = response.json()
    assert body["issue"]["number"] == 7 and body["issue"]["state"] == "review"
    assert body["issue"]["updated_at"] == (NOW - timedelta(hours=1)).isoformat()
    assert body["issue"]["labels"] == ["issuebot/review"]
    assert body["running"]["run_id"] == RUN_ID and body["retry"] is None
    (run,) = body["runs"]
    assert (run["run_id"], run["outcome"], run["cost_usd"]) == (RUN_ID, "succeeded", 0.8976)
    assert run["turns"] == 1  # the run's turn count, a runs column
    (turn,) = run["captured_turns"]
    assert (turn["turn_number"], turn["model"], turn["num_turns"]) == (1, "claude-opus-5", 19)
    assert turn["url"] == f"{BASE}/issues/7/runs/{RUN_ID}/turns/1"
    assert "stream" not in turn and "prompt" not in turn
    assert body["logs"] == [
        {
            "run_id": RUN_ID,
            "turn_number": 1,
            "label": f"run {RUN_ID} turn 1",
            "url": f"{BASE}/issues/7/runs/{RUN_ID}/turns/1",
        }
    ]
    assert [event["kind"] for event in body["recent_events"]] == ["run_ended", "state_changed"]
    assert body["recent_events"][0]["payload"]["outcome"] == "succeeded"
    assert body["recent_events"][0]["at"] == (NOW - timedelta(hours=1)).isoformat()


def test_issue_with_a_retry_entry(h: Harness) -> None:
    h.queries.issue_rows[9] = issue_row(number=9, identifier="repo-9", state="todo")
    h.queries.snapshot_row = snapshot(retrying=(retry_row(),))
    body = h.client.get(f"{API}/issues/9").json()
    assert body["running"] is None and body["retry"]["kind"] == "failure"
    assert body["runs"] == [] and body["logs"] == []


def test_unknown_issue_is_a_404_envelope(h: Harness) -> None:
    response = h.client.get(f"{API}/issues/99")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "unknown_issue", "message": "issue #99 is not known"}
    }


def test_a_non_numeric_issue_is_a_404_envelope(h: Harness) -> None:
    response = h.client.get(f"{API}/issues/abc")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- /api/v1/stats -----------------------------------------------------------------------------


def test_stats_default_window(h: Harness) -> None:
    h.queries.closed = {7: 2, 1: 1}
    h.queries.runs = {7: 3, 1: 1}
    h.queries.counts = {"todo": 1, "in_progress": 0, "review": 1, "rework": 0, "complete": 3}
    h.queries.series = [DailyPoint(day=date(2026, 9, 4), closed=1, runs=1)]
    body = h.client.get(f"{API}/stats").json()
    assert body == {
        "window": "7d",
        "days": 7,
        "closed": 2,
        "runs": 3,
        "by_state": {"todo": 1, "in_progress": 0, "review": 1, "rework": 0, "complete": 3},
        "series": [{"day": "2026-09-04", "closed": 1, "runs": 1}],
    }
    assert h.queries.days_asked == 7


def test_stats_thirty_day_window(h: Harness) -> None:
    h.queries.closed[30] = 5
    h.queries.runs[30] = 9
    body = h.client.get(f"{API}/stats?window=30d").json()
    assert (body["window"], body["days"], body["closed"], body["runs"]) == ("30d", 30, 5, 9)
    assert h.queries.days_asked == 30


@pytest.mark.parametrize("window", ["0d", f"{MAX_WINDOW_DAYS + 1}d", "7", "x", "7D", "-3d"])
def test_stats_rejects_a_bad_window(h: Harness, window: str) -> None:
    response = h.client.get(f"{API}/stats", params={"window": window})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_window"
    assert f"1 <= N <= {MAX_WINDOW_DAYS}" in response.json()["error"]["message"]


# --- POST /api/v1/refresh ----------------------------------------------------------------------


def test_refresh_notifies_then_coalesces_then_notifies_again(h: Harness) -> None:
    first = h.client.post(f"{API}/refresh", headers=REFRESH_HEADERS)
    assert first.status_code == 202
    assert first.json() == {
        "queued": True,
        "coalesced": False,
        "requested_at": NOW.isoformat(),
        "operations": ["poll", "reconcile"],
    }
    h.clock.mono += REFRESH_MIN_INTERVAL_S - 0.5
    second = h.client.post(f"{API}/refresh", headers=REFRESH_HEADERS)
    assert second.status_code == 202
    assert (second.json()["queued"], second.json()["coalesced"]) == (False, True)
    assert h.database.notified == 1
    h.clock.mono += 0.5
    third = h.client.post(f"{API}/refresh", headers=REFRESH_HEADERS)
    assert (third.json()["queued"], third.json()["coalesced"]) == (True, False)
    assert h.database.notified == 2


def test_refresh_reports_a_database_failure(h: Harness) -> None:
    message = "cannot connect to postgresql://issuebot:***@db.example:5432/issuebot: refused"
    h.database.notify_error = StoreUnavailableError(message)
    response = h.client.post(f"{API}/refresh", headers=REFRESH_HEADERS)
    assert response.status_code == 503
    assert response.json() == {"error": {"code": "database_unavailable", "message": message}}
    assert "s3cret" not in response.text
    assert "***" in response.text


def test_refresh_only_accepts_post(h: Harness) -> None:
    response = h.client.get(f"{API}/refresh")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


# --- /healthz ------------------------------------------------------------------------------------


def test_healthz_ok_stale_and_none(h: Harness) -> None:
    """The three states, per registered repository: they live in the ``workers`` map now."""
    body = h.client.get("/healthz").json()
    assert (body["status"], body["worker"]) == ("ok", "none")
    assert body["workers"][REPO] == {
        "status": "none",
        "snapshot_at": None,
        "snapshot_age_s": None,
        "dispatch_hold": None,
    }
    h.queries.snapshot_row = snapshot(age_s=5.0)
    worker = h.client.get("/healthz").json()["workers"][REPO]
    assert (worker["status"], worker["snapshot_age_s"]) == ("ok", 5.0)
    assert worker["snapshot_at"] == (NOW - timedelta(seconds=6)).isoformat()
    h.queries.snapshot_row = snapshot(age_s=91.0)
    assert h.client.get("/healthz").json()["workers"][REPO]["status"] == "stale"


def test_healthz_reports_an_unreachable_database(h: Harness) -> None:
    h.queries.error = StoreUnavailableError("cannot connect: refused")
    response = h.client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "database": "unavailable",
        "error": "cannot connect: refused",
    }


# --- repositories ------------------------------------------------------------------------------


def test_root_redirects_to_the_cookie_then_the_first_repository(h: Harness) -> None:
    h.register("acme/frontend")
    response = h.client.get("/", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (302, "/r/acme/frontend/")
    h.client.cookies.set("issuebot-repo", "example/repo")
    response = h.client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/r/example/repo/"
    h.client.cookies.set("issuebot-repo", "nobody/here")
    response = h.client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/r/acme/frontend/"
    # what app.js actually stores: encodeURIComponent turns the name's "/" into %2F, and
    # nothing between the browser and here decodes a cookie value.
    h.client.cookies.set("issuebot-repo", "example%2Frepo")
    response = h.client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/r/example/repo/"


def test_root_without_a_registry_is_a_plain_page(h: Harness) -> None:
    h.queries.repo_rows.clear()
    response = h.client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "no worker has registered yet" in response.text
    assert "<select" not in response.text


def test_an_unregistered_prefix_is_a_404_before_any_scoped_read(h: Harness) -> None:
    response = h.client.get("/api/v1/repos/nobody/here/state")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert h.queries.scoped_repos == []
    page = h.client.get("/r/nobody/here/")
    assert page.status_code == 404 and "<h1>404</h1>" in page.text


def test_the_repos_api_lists_every_registration_with_its_worker(h: Harness) -> None:
    h.register("acme/frontend")
    h.queries.snapshot_rows["acme/frontend"] = snapshot(age_s=5.0)
    body = h.client.get("/api/v1/repos").json()
    assert body == {
        "repos": [
            {
                "repo": "acme/frontend",
                "url": "/r/acme/frontend/",
                "worker": "ok",
                "snapshot_at": (NOW - timedelta(seconds=6)).isoformat(),
            },
            {
                "repo": "example/repo",
                "url": "/r/example/repo/",
                "worker": "none",
                "snapshot_at": None,
            },
        ]
    }


def test_healthz_and_state_report_a_worker_that_cannot_read_github(h: Harness) -> None:
    """Nothing else in the snapshot distinguishes an outage from a quiet board (#88)."""
    hold = DispatchHold(
        kind="github",
        reason=(
            "GitHub is not answering this worker: transport: http 502: Bad Gateway "
            "\u2014 githubstatus.com: Pull Requests, major outage"
        ),
        since=HELD_SINCE,
    )
    h.queries.snapshot_rows["example/repo"] = snapshot(age_s=5.0, dispatch_hold=hold)
    h.queries.snapshot_row = snapshot(age_s=5.0, dispatch_hold=hold)
    body = h.client.get("/healthz").json()
    assert body["worker"] == "held"
    assert body["workers"]["example/repo"]["status"] == "held"
    assert body["workers"]["example/repo"]["dispatch_hold"]["kind"] == "github"
    state = h.client.get(f"{API}/state").json()
    assert state["worker"]["dispatch_hold"]["kind"] == "github"
    assert (
        "githubstatus.com: Pull Requests, major outage"
        in (state["worker"]["dispatch_hold"]["reason"])
    )


def test_healthz_maps_every_worker_and_reports_the_worst(h: Harness) -> None:
    h.register("acme/frontend")
    h.queries.snapshot_rows["example/repo"] = snapshot(age_s=5.0)
    h.queries.snapshot_rows["acme/frontend"] = snapshot(age_s=5.0, dispatch_hold=HOLD)
    body = h.client.get("/healthz").json()
    assert body["status"] == "ok" and body["database"] == "ok"
    assert body["worker"] == "held"
    assert body["workers"]["example/repo"] == {
        "status": "ok",
        "snapshot_at": (NOW - timedelta(seconds=6)).isoformat(),
        "snapshot_age_s": 5.0,
        "dispatch_hold": None,
    }
    assert body["workers"]["acme/frontend"]["status"] == "held"
    assert body["workers"]["acme/frontend"]["dispatch_hold"]["kind"] == "auth"
    h.queries.snapshot_rows["acme/frontend"] = snapshot(age_s=91.0)
    assert h.client.get("/healthz").json()["worker"] == "stale"
    h.queries.repo_rows.clear()
    assert h.client.get("/healthz").json() == {
        "status": "ok",
        "database": "ok",
        "worker": "none",
        "workers": {},
    }


def test_refresh_is_throttled_per_repository(h: Harness) -> None:
    h.register("acme/frontend")
    assert h.client.post(f"{API}/refresh", headers=REFRESH_HEADERS).json()["queued"] is True
    assert h.client.post(f"{API}/refresh", headers=REFRESH_HEADERS).json()["coalesced"] is True
    assert (
        h.client.post("/api/v1/repos/acme/frontend/refresh", headers=REFRESH_HEADERS).json()[
            "queued"
        ]
        is True
    )
    assert h.database.notified_repos == ["example/repo", "acme/frontend"]


def test_invalid_stored_labels_are_a_503_under_that_prefix_only(h: Harness) -> None:
    h.register("acme/frontend", labels={"todo": ""})
    response = h.client.get("/r/acme/frontend/")
    assert response.status_code == 503 and "do not validate" in response.text
    assert h.client.get(f"{BASE}/").status_code == 200


# --- errors and headers -------------------------------------------------------------------------


def test_a_database_error_in_the_api_is_a_503_envelope(h: Harness) -> None:
    h.queries.error = StoreUnavailableError("cannot connect: refused")
    response = h.client.get(f"{API}/state")
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "database_unavailable",
        "message": "cannot connect: refused",
    }


def test_unknown_api_paths_and_methods_get_envelopes(h: Harness) -> None:
    missing = h.client.get("/api/v1/nothing")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    wrong = h.client.delete(f"{API}/state")
    assert wrong.status_code == 405
    assert wrong.json()["error"]["code"] == "method_not_allowed"


def test_every_response_carries_the_security_headers(h: Harness) -> None:
    h.queries.snapshot_row = snapshot()
    for response in (
        h.client.get(f"{API}/state"),
        h.client.get("/healthz"),
        h.client.get("/api/v1/nothing"),
        h.client.post(f"{API}/refresh", headers=REFRESH_HEADERS),
    ):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, (response.url, name)
    assert "'unsafe-inline'" not in SECURITY_HEADERS["Content-Security-Policy"]
    assert "'unsafe-eval'" not in SECURITY_HEADERS["Content-Security-Policy"]


def _raising_routes(h: Harness) -> None:
    """Two routes no handler covers, one under the API prefix and one page."""

    @h.app.get("/api/v1/boom")
    async def boom_api() -> None:
        raise RuntimeError("detail-only-uvicorn-logs")

    @h.app.get("/r/boom")
    async def boom_page() -> None:
        raise RuntimeError("detail-only-uvicorn-logs")


def test_an_unhandled_exception_leaves_with_the_security_headers(h: Harness) -> None:
    """The headers ride the send channel, not the response ``call_next`` returns (#106): a
    500 Starlette's error middleware would otherwise emit bare gets the same envelope or page,
    the same headers, and a log line naming the type and the path, never the message."""
    _raising_routes(h)
    with structlog.testing.capture_logs() as logs:
        api = h.client.get("/api/v1/boom")
        page = h.client.get("/r/boom")
    for response in (api, page):
        assert response.status_code == 500
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, (response.url, name)
    assert api.json() == {"error": {"code": "internal_error", "message": "internal server error"}}
    assert page.headers["content-type"].startswith("text/html")
    assert "500" in page.text and "internal_error" in page.text
    assert "detail-only-uvicorn-logs" not in api.text + page.text
    errors = [entry for entry in logs if entry["event"] == "web_unhandled_error"]
    assert [(entry["path"], entry["error"]) for entry in errors] == [
        ("/api/v1/boom", "RuntimeError"),
        ("/r/boom", "RuntimeError"),
    ]
    assert "detail-only-uvicorn-logs" not in repr(errors)


def test_an_unhandled_exception_is_still_raised_after_it_is_answered(h: Harness) -> None:
    """Answered, then re-raised: uvicorn logs the traceback and a strict client sees it."""
    _raising_routes(h)
    strict = TestClient(h.app, headers=basic_auth(PASSWORD))
    with pytest.raises(RuntimeError, match="detail-only"):
        strict.get("/api/v1/boom")


def test_an_unhandled_exception_in_the_gate_leaves_with_the_headers(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The header layer wraps the gate, so an exception raised before routing is covered too."""

    def broken(_header: str | None) -> str | None:
        raise RuntimeError("detail-only-uvicorn-logs")

    monkeypatch.setattr("issuebot.web.app.presented_password", broken)
    with structlog.testing.capture_logs() as logs:
        response = h.anonymous.get(f"{API}/state")
    assert response.status_code == 500
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert response.json()["error"]["code"] == "internal_error"
    assert [entry["path"] for entry in logs if entry["event"] == "web_unhandled_error"] == [
        f"{API}/state"
    ]


async def test_a_failing_error_renderer_still_leaves_with_the_headers() -> None:
    """Should ``on_error`` raise too, a plain 500 goes out with the headers, ``on_failure``
    hears about the renderer's exception, and the original one is what propagates."""
    from issuebot.web.app import _SecureExit

    async def app(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("original")

    def on_error(request: Any, exc: Exception) -> Any:
        raise ValueError("renderer")

    failures: list[tuple[str, str]] = []
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    layer = _SecureExit(
        app,
        on_error=on_error,
        on_failure=lambda request, exc: failures.append((request.url.path, str(exc))),
    )
    scope = {"type": "http", "method": "GET", "path": "/r/x", "headers": [], "query_string": b""}
    with pytest.raises(RuntimeError, match="original"):
        await layer(scope, receive, send)
    assert failures == [("/r/x", "renderer")]
    start = sent[0]
    assert start["type"] == "http.response.start" and start["status"] == 500
    headers = {name.decode(): value.decode() for name, value in start["headers"]}
    for name, value in SECURITY_HEADERS.items():
        assert headers[name.lower()] == value
    assert b"".join(message.get("body", b"") for message in sent[1:]) == b"internal server error"


def test_an_oversized_window_is_a_400_not_a_500(h: Harness) -> None:
    """``int`` refuses a digit run past ``sys.get_int_max_str_digits()``; the pattern never
    lets one through (#106)."""
    digits = "1" * (sys.get_int_max_str_digits() + 1)
    response = h.client.get(f"{API}/stats", params={"window": f"{digits}d"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_window"
    assert h.database.opened == 0


def test_each_request_uses_one_connection(h: Harness) -> None:
    h.seed_issue()
    h.client.get(f"{API}/issues/7")
    assert h.database.opened == 1
    h.client.get(f"{API}/stats")
    assert h.database.opened == 2


# --- the pure builders --------------------------------------------------------------------------


def test_safe_href() -> None:
    assert safe_href("https://github.com/example/repo/issues/7") == (
        "https://github.com/example/repo/issues/7"
    )
    assert safe_href("http://github.com/x") is None
    assert safe_href("javascript:alert(1)") is None
    assert safe_href(None) is None
    assert safe_href(7) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "days"),
    [
        (None, 7),
        ("", 7),
        ("1d", 1),
        ("7d", 7),
        ("30d", 30),
        (f"{MAX_WINDOW_DAYS}d", MAX_WINDOW_DAYS),
    ],
)
def test_window_days_accepts(text: str | None, days: int) -> None:
    assert window_days(text) == days


@pytest.mark.parametrize(
    "text",
    [
        "0d",
        "366d",
        "7",
        "d",
        "7D",
        "1.5d",
        " 7d",
        "-1d",
        "0007d",
        pytest.param("1" * 4301 + "d", id="past-the-int-digit-limit"),
        pytest.param("0" * 5000 + "d", id="zeros-past-the-limit"),
    ],
)
def test_window_days_rejects(text: str) -> None:
    assert window_days(text) is None


def test_worker_status() -> None:
    assert worker_status(None, NOW) == "none"
    assert worker_status(snapshot(age_s=89.9), NOW) == "ok"
    assert worker_status(snapshot(age_s=90.1), NOW) == "stale"
    assert worker_status(snapshot(age_s=20.0, poll_interval_ms=5_000), NOW) == "stale"
    row = snapshot(age_s=100.0)
    row.data.pop("poll_interval_ms")  # an older worker's snapshot: assume 30 s
    assert worker_status(row, NOW) == "stale"


def test_a_worker_that_is_ticking_but_not_claiming_is_held_not_ok() -> None:
    assert worker_status(snapshot(age_s=5.0, dispatch_hold=HOLD), NOW) == "held"
    # A snapshot too old to trust says stale first: its hold is as old as the rest of it.
    assert worker_status(snapshot(age_s=100.0, dispatch_hold=HOLD), NOW) == "stale"


def test_state_reports_the_credential_and_the_windows(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(rate_limits=limits(0.42, 0.32))
    body = h.client.get(f"{API}/state").json()
    assert body["credential"] == "subscription"
    assert [(w["key"], w["percent"]) for w in body["rate_limits"]] == [
        ("five_hour", 42),
        ("seven_day", 32),
    ]


def test_state_without_a_snapshot_says_the_credential_is_unknown(h: Harness) -> None:
    h.queries.snapshot_row = None
    body = h.client.get(f"{API}/state").json()
    assert body["credential"] == "unknown"
    assert body["rate_limits"] == []


def test_rate_limit_windows_read_the_latest_reading() -> None:
    windows = rate_limit_windows(snapshot(rate_limits=limits(0.42, 0.32)), NOW)
    assert windows == [
        {
            "key": "five_hour",
            "label": "5-hour",
            "percent": 42,
            "resets_at": (NOW + timedelta(hours=2)).isoformat(),
            "observed_at": (NOW - timedelta(minutes=4)).isoformat(),
        },
        {
            "key": "seven_day",
            "label": "7-day",
            "percent": 32,
            "resets_at": (NOW + timedelta(days=3)).isoformat(),
            "observed_at": (NOW - timedelta(minutes=4)).isoformat(),
        },
    ]


def test_a_window_past_its_reset_reads_zero() -> None:
    """Nothing ran since it rolled over, so the reading is not stale -- it is spent."""
    row = snapshot(
        rate_limits=limits(
            0.42, 0.32, five_resets_in=-timedelta(minutes=1), observed_ago=timedelta(hours=6)
        )
    )
    five, seven = rate_limit_windows(row, NOW)
    assert five["percent"] == 0
    assert seven["percent"] == 32, "the seven-day window has not reset, so it keeps its reading"


def test_a_window_resetting_exactly_now_reads_zero() -> None:
    row = snapshot(rate_limits=limits(0.42, five_resets_in=timedelta(0)))
    assert rate_limit_windows(row, NOW)[0]["percent"] == 0


def test_rate_limit_windows_are_empty_without_a_usable_reading() -> None:
    assert rate_limit_windows(None, NOW) == []
    assert rate_limit_windows(snapshot(), NOW) == []
    assert rate_limit_windows(snapshot(credential="api_key", rate_limits=limits()), NOW) == []
    for value in ("nonsense", 5, [], {}, {"five_hour": "x", "seven_day": None}):
        row = snapshot()
        row.data["rate_limits"] = value
        assert rate_limit_windows(row, NOW) == []


def test_an_unknown_credential_still_shows_a_reading_it_has() -> None:
    """N/A is for a definite API key; an unreadable probe must not hide real data."""
    row = snapshot(credential="unknown", rate_limits=limits(0.42))
    assert rate_limit_windows(row, NOW)[0]["percent"] == 42
    assert rate_limit_windows(snapshot(credential="unknown"), NOW) == []


def test_limits_unavailable_tells_the_two_blank_cases_apart() -> None:
    """N/A is "never applicable"; the dash is "not yet", and the tooltip says which."""
    api_key = limits_unavailable(snapshot(credential="api_key"))
    assert api_key["value"] == "N/A"
    assert "API key" in api_key["title"]
    nothing_yet = limits_unavailable(snapshot())
    assert nothing_yet["value"] == "\u2014"
    assert "turn" in nothing_yet["title"]
    assert limits_unavailable(None) == nothing_yet


@pytest.mark.parametrize(
    ("credential", "label"),
    [("subscription", "cost (effort)"), ("api_key", "cost (actual)"), ("unknown", "cost")],
)
def test_cost_label_follows_the_credential(credential: str, label: str) -> None:
    assert cost_label(snapshot(credential=credential)) == label


def test_cost_label_without_a_snapshot_is_bare() -> None:
    assert cost_label(None) == "cost"


def test_dispatch_hold_ignores_a_snapshot_that_names_no_reason() -> None:
    assert dispatch_hold(None) is None
    assert dispatch_hold(snapshot()) is None
    row = snapshot()
    for value in ("not a mapping", {}, {"kind": "auth", "reason": ""}, {"kind": "auth"}):
        row.data["dispatch_hold"] = value
        assert dispatch_hold(row) is None
    row.data["dispatch_hold"] = {"kind": "auth", "reason": "no login", "since": None}
    assert dispatch_hold(row) == {"kind": "auth", "reason": "no login", "since": None}
    # A reason is enough to report; a kind another worker's snapshot does not carry is not.
    row.data["dispatch_hold"] = {"reason": "no login"}
    assert dispatch_hold(row) == {"kind": "unknown", "reason": "no login", "since": None}


def test_state_names_the_reason_dispatch_is_held(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(dispatch_hold=HOLD)
    worker = h.client.get(f"{API}/state").json()["worker"]
    assert worker["status"] == "held"
    assert worker["stale"] is False
    assert worker["dispatch_hold"] == {
        "kind": "auth",
        "reason": "claude authentication unavailable: not logged in",
        "since": HELD_SINCE.isoformat(),
    }


def test_healthz_reports_a_held_worker_and_why(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(dispatch_hold=HOLD)
    body = h.client.get("/healthz").json()
    assert body["status"] == "ok"  # the service is fine; the worker is not claiming
    assert body["worker"] == "held"
    hold = body["workers"][REPO]["dispatch_hold"]
    assert hold["reason"].startswith("claude authentication unavailable")


@pytest.mark.parametrize(
    ("kind", "payload", "text"),
    [
        (
            "state_changed",
            {
                "from_label": "issuebot/todo",
                "to_label": "issuebot/in-progress",
                "actor": "issuebot",
            },
            "issuebot changed issuebot/todo to issuebot/in-progress",
        ),
        (
            "state_changed",
            {"from_label": None, "to_label": "issuebot/todo", "actor": "human"},
            "human changed unlabelled to issuebot/todo",
        ),
        (
            "state_changed",
            {
                "from_label": "issuebot/in-progress",
                "to_label": "issuebot/review",
                "actor": "agent",
                "pr_url": "https://github.com/example/repo/pull/8",
            },
            "agent changed issuebot/in-progress to issuebot/review "
            "(https://github.com/example/repo/pull/8)",
        ),
        (
            "state_changed",
            {"from_label": "issuebot/review", "to_label": None, "actor": "issuebot"},
            "issuebot changed issuebot/review to unlabelled",
        ),
        ("run_started", {"run_id": RUN_ID, "attempt": 2}, f"run {RUN_ID} started (attempt 2)"),
        (
            "run_ended",
            {
                "run_id": RUN_ID,
                "outcome": "succeeded",
                "turns": 1,
                "cost_usd": 0.8976,
                "input_tokens": 513338,
                "output_tokens": 8425,
                "error": None,
            },
            f"run {RUN_ID} succeeded after 1 turn, $0.90, 513338 in / 8425 out",
        ),
        (
            "run_ended",
            {
                "run_id": RUN_ID,
                "outcome": "failed",
                "turns": 2,
                "cost_usd": 0.5,
                "input_tokens": 10,
                "output_tokens": 1,
                "error": "turn_failed: boom",
            },
            f"run {RUN_ID} failed after 2 turns, $0.50, 10 in / 1 out: turn_failed: boom",
        ),
        (
            "pr_opened",
            {"pr_number": 8, "pr_url": "https://github.com/example/repo/pull/8"},
            "pull request #8 opened (https://github.com/example/repo/pull/8)",
        ),
        ("blocked", {"reason": "turn budget exhausted"}, "blocked: turn budget exhausted"),
        (
            "issue_completed",
            {"pr_url": "https://github.com/example/repo/pull/8"},
            "completed (https://github.com/example/repo/pull/8)",
        ),
        ("issue_completed", {"pr_url": None}, "completed"),
        (
            "issue_completed",
            {"pr_url": None, "resolution": "no_change"},
            "completed: no change needed",
        ),
        # Events stored before the resolution field existed were all merged-PR completions.
        (
            "issue_completed",
            {"pr_url": "https://github.com/example/repo/pull/8", "resolution": "merged_pr"},
            "completed (https://github.com/example/repo/pull/8)",
        ),
        (
            "issue_cancelled",
            {"reason": "closed without a merged pull request"},
            "cancelled: closed without a merged pull request",
        ),
        (
            "notification_sent",
            {"channel": "slack", "about_kind": "blocked"},
            "slack notified about blocked",
        ),
        ("something_new", {"x": 1}, "something_new"),
        ("run_ended", {}, "run ? ? after 0 turns, $0.00, 0 in / 0 out"),
    ],
)
def test_describe_event(kind: str, payload: dict[str, Any], text: str) -> None:
    assert describe_event(event_row(kind=kind, payload=payload)) == text


def test_repo_paths() -> None:
    assert repo_base("acme/frontend") == "/r/acme/frontend"
    assert api_base("acme/frontend") == "/api/v1/repos/acme/frontend"


@pytest.mark.parametrize(
    ("kind", "query", "target"),
    [
        ("dashboard", "", "/r/acme/frontend/"),
        ("issues", "", "/r/acme/frontend/issues"),
        ("issues", "state=review", "/r/acme/frontend/issues?state=review"),
        ("issue", "", "/r/acme/frontend/"),  # an issue number means nothing elsewhere
        ("turn", "", "/r/acme/frontend/"),
    ],
)
def test_switch_target(kind: str, query: str, target: str) -> None:
    assert switch_target(kind, "acme/frontend", query) == target


def test_worst_status_orders_none_over_stale_over_held_over_ok() -> None:
    assert worst_status([]) == "none"
    assert worst_status(["ok", "ok"]) == "ok"
    assert worst_status(["ok", "held"]) == "held"
    assert worst_status(["held", "stale"]) == "stale"
    assert worst_status(["stale", "none", "ok"]) == "none"


def test_repo_labels_validates_the_stored_mapping() -> None:
    row = repo_row(labels={**GitHubLabels().model_dump(), "review": "issuebot/check"})
    assert repo_labels(row).review == "issuebot/check"
    with pytest.raises(ValueError):
        repo_labels(repo_row(labels={"todo": ""}))
