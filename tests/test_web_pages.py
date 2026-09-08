"""Tests for the HTML pages, the live partial, the raw text routes and the static files."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from importlib.resources import files
from typing import Any

import pytest

from fakes.web import (
    NOW,
    RUN_ID,
    Harness,
    issue_row,
    retry_row,
    run_row,
    running_row,
    snapshot,
)
from issuebot.config import GitHubLabels
from issuebot.db import COMPLETE_LIMIT, PROMPT_LIMIT, STDERR_LIMIT, StoreUnavailableError
from issuebot.web import LIVE_POLL_S, SECURITY_HEADERS
from issuebot.web.views import (
    age_text,
    dashboard_context,
    duration_text,
    money,
    stamp_text,
    thousands,
)

CSS = (files("issuebot.web") / "static" / "app.css").read_text(encoding="utf-8")


def css_declarations(selector: str) -> dict[str, str]:
    """The declarations of the one rule that starts with ``selector``, property to value."""
    start = CSS.index(selector)
    block = CSS[CSS.index("{", start) + 1 : CSS.index("}", start)]
    found = {}
    for declaration in block.split(";"):
        name, sep, value = declaration.partition(":")
        if sep:
            found[name.strip()] = value.strip()
    return found


RUN_ID_2 = "20260904T210000Z-abcdef"
HOSTILE = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"


@pytest.fixture
def h() -> Iterator[Harness]:
    harness = Harness()
    with harness.client:
        yield harness


def html(response: Any) -> str:
    assert response.headers["content-type"].startswith("text/html")
    return response.text


# --- the dashboard page -----------------------------------------------------------------------


def test_the_dashboard_renders_and_escapes(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(running=(running_row(title=HOSTILE),))
    h.queries.groups["review"] = [issue_row(title=HOSTILE)]
    h.queries.groups["todo"] = [
        issue_row(number=3, title="Safe", pr_number=4, pr_url="javascript:alert(1)")
    ]
    response = h.client.get("/")
    assert response.status_code == 200
    text = html(response)
    assert HOSTILE not in text and text.count(ESCAPED) == 2
    assert 'hx-get="/partials/dashboard"' in text
    assert f'hx-trigger="every {LIVE_POLL_S}s"' in text
    assert 'hx-post="/api/v1/refresh"' in text and 'id="refresh-status"' in text
    assert (
        'src="/static/vendor/htmx.min.js"' in text and 'src="/static/vendor/chart.umd.js"' in text
    )
    assert 'id="closed-chart"' in text and 'id="runs-chart"' in text
    assert 'data-chart-window="30d"' in text and 'data-chart-poll-s="60"' in text
    assert '<meta name="htmx-config"' in text and '"allowEval": false' in text
    assert '<link rel="icon" href="data:,">' in text
    assert '"code": "503", "swap": true' in text
    for label in GitHubLabels().as_tuple():
        assert label in text
    assert 'href="/issues/7"' in text and 'href="/issues/3"' in text
    assert "javascript:" not in text and "PR #4" in text
    assert "<script>" not in text and ' style="' not in text  # the CSP forbids inline code
    assert "example/repo" in text


def test_the_dashboard_shows_a_running_agent_and_a_retry(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(running=(running_row(),), retrying=(retry_row(),))
    text = html(h.client.get("/"))
    assert 'class="panel worker ok"' in text and "tick 41" in text
    assert "turn_activity" in text and "20 s ago" in text
    assert "Retrying" in text and "turn_failed: boom" in text


def test_the_live_partial_without_a_snapshot(h: Harness) -> None:
    text = html(h.client.get("/partials/dashboard"))
    assert text.startswith('<div id="live"')
    assert 'class="panel worker none"' in text and "no report yet" in text
    assert "no agent is running" in text
    assert "Retrying" not in text


def test_the_live_partial_marks_a_stale_worker(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(age_s=200.0)
    text = html(h.client.get("/partials/dashboard"))
    assert 'class="panel worker stale"' in text and "3 min ago" in text


def test_a_kanban_card_separates_the_number_from_the_title(h: Harness) -> None:
    """The number is metadata and the title is the content, so they are separate elements.

    Both stay inside the one link to the issue page, which is what lets the stylesheet lay
    the chip and the title out as a row without giving up a single click target.
    """
    h.queries.groups["todo"] = [issue_row(number=23, title="Add a status badge to the README")]
    text = html(h.client.get("/partials/dashboard"))
    assert (
        '<a class="title" href="/issues/23">'
        '<span class="number">#23</span>'
        '<span class="text">Add a status badge to the README</span></a>'
    ) in text


def test_the_card_title_is_still_escaped(h: Harness) -> None:
    """The title moved into a new element; it must not have picked up any markup on the way."""
    h.queries.groups["todo"] = [issue_row(title=HOSTILE)]
    text = html(h.client.get("/partials/dashboard"))
    assert HOSTILE not in text
    assert f'<span class="text">{ESCAPED}</span>' in text


def test_the_card_is_laid_out_by_the_stylesheet_alone(h: Harness) -> None:
    """The chip and the title are new elements; the CSP forbids styling them inline."""
    assert ".card .number {" in CSS and ".card .title .text {" in CSS
    assert ' style="' not in html(h.client.get("/partials/dashboard"))


def test_a_long_card_title_cannot_stretch_its_column() -> None:
    """A column is a grid track with a 180px floor; an unbroken title would blow past it.

    The standard `line-clamp` is checked as a whole declaration: as a bare substring it is
    also inside `-webkit-line-clamp`, so it could be deleted with the test still green.
    """
    declarations = css_declarations(".card .title .text {")
    assert declarations["-webkit-line-clamp"] == "3"
    assert declarations["line-clamp"] == "3", "the standard property must ship beside the prefix"
    assert declarations["overflow"] == "hidden"
    assert declarations["overflow-wrap"] == "anywhere"


def test_the_chip_stays_beside_the_first_line_of_a_wrapped_title() -> None:
    """`overflow` makes the title a scroll container, whose baseline is its bottom edge.

    Aligning the two on the baseline would therefore drop the chip to the last line of a
    wrapped title - the case the clamp exists for. They are aligned to the top instead.
    """
    assert css_declarations(".card .title {")["align-items"] == "flex-start"


def test_the_card_link_is_reachable_by_keyboard() -> None:
    """The card is the primary navigation on the dashboard, so its focus must be visible."""
    assert ".card .title:focus-visible { outline: 2px solid var(--accent);" in CSS


def test_only_the_link_lights_the_card_up() -> None:
    """A bare .card:hover would offer a click on the meta row and the padding as well."""
    assert ".card:has(.title:hover) {" in CSS
    assert ".card:hover {" not in CSS


def test_the_live_partial_shows_a_config_error(h: Harness) -> None:
    row = snapshot()
    row.data["config_valid"] = False
    row.data["config_error"] = "polling.interval_ms must be >= 1000"
    h.queries.snapshot_row = row
    text = html(h.client.get("/partials/dashboard"))
    assert "config error: polling.interval_ms must be &gt;= 1000" in text


def test_the_live_partial_notes_the_complete_cap(h: Harness) -> None:
    h.queries.groups["complete"] = [
        issue_row(number=n, state="complete", github_state="closed")
        for n in range(1, COMPLETE_LIMIT + 1)
    ]
    text = html(h.client.get("/partials/dashboard"))
    assert f"{COMPLETE_LIMIT} most recent" in text
    h.queries.groups["complete"].pop()
    assert "most recent" not in html(h.client.get("/partials/dashboard"))


def test_the_live_partial_survives_a_database_error(h: Harness) -> None:
    h.queries.error = StoreUnavailableError("cannot connect: refused")
    response = h.client.get("/partials/dashboard")
    assert response.status_code == 503
    text = html(response)
    assert text.startswith('<div id="live"') and 'hx-get="/partials/dashboard"' in text
    assert "database unavailable: cannot connect: refused" in text


def test_a_database_error_on_a_page_is_an_html_503(h: Harness) -> None:
    h.queries.error = StoreUnavailableError("cannot connect: refused")
    response = h.client.get("/")
    assert response.status_code == 503
    text = html(response)
    assert "<h1>503</h1>" in text and "database_unavailable" in text
    assert "cannot connect: refused" in text


# --- the issue page -----------------------------------------------------------------------------


def test_the_issue_page(h: Harness) -> None:
    h.seed_issue()
    h.queries.runs_by_issue[7].append(
        run_row(run_id=RUN_ID_2, attempt=2, outcome="failed", error="turn_failed: boom")
    )
    h.queries.snapshot_row = snapshot(running=(running_row(),))
    response = h.client.get("/issues/7")
    assert response.status_code == 200
    text = html(response)
    assert "Add a power function" in text and "issuebot/review" in text
    assert 'href="https://github.com/example/repo/issues/7"' in text
    assert 'href="https://github.com/example/repo/pull/8"' in text
    assert "Running now" in text and "turn_activity" in text
    assert RUN_ID in text and RUN_ID_2 in text
    assert "claude-opus-5" in text and "19 agent iterations" in text and "3m21s" in text
    assert f'href="/issues/7/runs/{RUN_ID}/turns/1"' in text
    assert text.count("turn logs were not captured") == 1  # the failed run has none
    assert "agent changed issuebot/in-progress to issuebot/review" in text
    assert "run 20260904T202535Z-0964cd succeeded after 1 turn, $0.90" in text
    assert "513,338" in text
    assert "<th>log dir</th>" in text
    assert text.count(f"/workspaces/repo-7/.issuebot/runs/{RUN_ID}") == 1  # its own row only
    # its own column, plus the "turn logs were not captured" hint (it has no turns)
    assert text.count(f"/workspaces/repo-7/.issuebot/runs/{RUN_ID_2}") == 2


def test_the_issue_page_shows_a_running_run_without_the_not_captured_hint(h: Harness) -> None:
    h.seed_issue()
    h.queries.runs_by_issue[7] = [run_row(run_id=RUN_ID_2, ended_at=None, outcome=None)]
    text = html(h.client.get("/issues/7"))
    assert '<td class="outcome running">running</td>' in text
    assert "turn logs were not captured" not in text


def test_the_issue_page_escapes_the_title(h: Harness) -> None:
    h.queries.issue_rows[7] = issue_row(title=HOSTILE)
    text = html(h.client.get("/issues/7"))
    assert HOSTILE not in text and ESCAPED in text
    assert "no runs recorded" in text and "no events recorded" in text


def test_an_unknown_issue_is_a_404_page(h: Harness) -> None:
    response = h.client.get("/issues/99")
    assert response.status_code == 404
    text = html(response)
    assert "<h1>404</h1>" in text and "issue #99 is not known" in text


def test_a_non_numeric_issue_is_a_404_page(h: Harness) -> None:
    response = h.client.get("/issues/abc")
    assert response.status_code == 404
    assert "<h1>404</h1>" in html(response)


# --- the turn page and the raw files -------------------------------------------------------------


def test_the_turn_page_renders_the_transcript(h: Harness) -> None:
    h.seed_issue()
    response = h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1")
    assert response.status_code == 200
    text = html(response)
    assert "turn 1 of 1" in text and "claude-opus-5" in text
    assert "Bash" in text and "ls &lt;dir&gt;" in text
    assert "Done &lt;script&gt;x&lt;/script&gt;" in text and "<script>x" not in text
    assert "README.md" in text
    assert "1 status message hidden" in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text and "<b>bold</b>" not in text
    assert "warning: something" in text
    for part in ("prompt", "stream", "stderr"):
        assert f'href="/issues/7/runs/{RUN_ID}/turns/1/{part}"' in text
    assert "95 lines" in text and "115,429 bytes" in text


def test_the_turn_page_notes_caps(h: Harness) -> None:
    h.seed_issue()
    row = h.queries.turn_rows[(RUN_ID, 1)]
    h.queries.turn_rows[(RUN_ID, 1)] = replace(row, truncated=True, omitted_lines=2, stream="")
    text = html(h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1"))
    assert "<strong>truncated</strong>" in text and "2 oversized lines replaced" in text
    assert "the stored stream is empty" in text


def test_the_turn_page_notes_cuts_by_bytes_not_characters(h: Harness) -> None:
    h.seed_issue()
    row = h.queries.turn_rows[(RUN_ID, 1)]

    prompt = "café — done"
    h.queries.turn_rows[(RUN_ID, 1)] = replace(
        row, prompt=prompt, prompt_bytes=len(prompt.encode())
    )
    text = html(h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1"))
    assert "showing the first" not in text

    h.queries.turn_rows[(RUN_ID, 1)] = replace(row, prompt_bytes=PROMPT_LIMIT + 1)
    text = html(h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1"))
    assert "showing the first" in text

    h.queries.turn_rows[(RUN_ID, 1)] = replace(row, stderr_bytes=STDERR_LIMIT)
    text = html(h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1"))
    assert "showing the tail" not in text

    h.queries.turn_rows[(RUN_ID, 1)] = replace(row, stderr_bytes=STDERR_LIMIT + 1)
    text = html(h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1"))
    assert "showing the tail" in text


@pytest.mark.parametrize(
    "path",
    [
        f"/issues/9/runs/{RUN_ID}/turns/1",  # another issue's run
        f"/issues/7/runs/{RUN_ID}/turns/2",  # no such turn
        f"/issues/7/runs/{RUN_ID_2}/turns/1",  # no such run
        "/issues/7/runs/bad/turns/1",  # malformed run id
        f"/issues/7/runs/{RUN_ID}/turns/x",  # malformed turn number
        f"/issues/99/runs/{RUN_ID}/turns/1",  # unknown issue
    ],
)
def test_turn_pages_that_do_not_exist_are_404_pages(h: Harness, path: str) -> None:
    h.seed_issue()
    h.queries.issue_rows[9] = issue_row(number=9, identifier="repo-9")
    response = h.client.get(path)
    assert response.status_code == 404
    assert "<h1>404</h1>" in html(response)


@pytest.mark.parametrize(
    ("part", "attribute", "extension"),
    [("prompt", "prompt", "md"), ("stream", "stream", "jsonl"), ("stderr", "stderr", "log")],
)
def test_raw_files_are_plain_text(h: Harness, part: str, attribute: str, extension: str) -> None:
    h.seed_issue()
    response = h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1/{part}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    expected = f'inline; filename="{RUN_ID}-turn-1.{extension}"'
    assert response.headers["content-disposition"] == expected
    assert response.text == getattr(h.queries.turn_rows[(RUN_ID, 1)], attribute)


def test_an_unknown_raw_part_is_a_404(h: Harness) -> None:
    h.seed_issue()
    assert h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1/other").status_code == 404
    assert h.client.get(f"/issues/7/runs/{RUN_ID}/turns/2/prompt").status_code == 404


# --- static files, unknown pages, headers -----------------------------------------------------


def test_static_files_are_served(h: Harness) -> None:
    css = h.client.get("/static/app.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "--accent" in css.text
    htmx = h.client.get("/static/vendor/htmx.min.js")
    assert htmx.status_code == 200 and htmx.text.startswith("var htmx=")
    chart = h.client.get("/static/vendor/chart.umd.js")
    assert chart.status_code == 200 and "Chart.js v4.5.1" in chart.text[:200]
    assert h.client.get("/static/app.js").status_code == 200
    assert h.client.get("/static/nope.css").status_code == 404


def test_an_unknown_page_is_a_404_page(h: Harness) -> None:
    response = h.client.get("/nothing")
    assert response.status_code == 404
    assert "<h1>404</h1>" in html(response)


def test_pages_and_static_files_carry_the_security_headers(h: Harness) -> None:
    h.seed_issue()
    for response in (
        h.client.get("/"),
        h.client.get("/partials/dashboard"),
        h.client.get("/issues/7"),
        h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1"),
        h.client.get("/static/app.css"),
        h.client.get("/nothing"),
    ):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, (response.url, name)


# --- the pure helpers -------------------------------------------------------------------------


def test_age_text() -> None:
    assert age_text(NOW - timedelta(seconds=12), NOW) == "12 s ago"
    assert age_text(NOW - timedelta(minutes=3, seconds=20), NOW) == "3 min ago"
    assert age_text(NOW - timedelta(hours=2, minutes=5), NOW) == "2 h ago"
    assert age_text(NOW - timedelta(days=4, hours=3), NOW) == "4 d ago"
    assert age_text((NOW - timedelta(seconds=45)).isoformat(), NOW) == "45 s ago"
    assert age_text(NOW + timedelta(seconds=30), NOW) == "0 s ago"
    assert age_text(None, NOW) == "-"
    assert age_text("not a date", NOW) == "not a date"


def test_stamp_duration_money_and_thousands() -> None:
    assert stamp_text(NOW) == "2026-09-04T12:00:00Z"
    assert stamp_text(NOW.isoformat()) == "2026-09-04T12:00:00Z"
    assert stamp_text(None) == "-"
    assert stamp_text("garbage") == "garbage"
    assert duration_text(201719) == "3m21s"
    assert duration_text(999) == "0m00s"
    assert duration_text(None) == "-"
    assert money(0.8976) == "$0.90"
    assert money(None) == "$0.00"
    assert thousands(513338) == "513,338"
    assert thousands(None) == "0"


def test_dashboard_context() -> None:
    groups = {role: [] for role in ("todo", "in_progress", "review", "rework", "complete")}
    groups["complete"] = [issue_row(number=n, state="complete") for n in range(COMPLETE_LIMIT)]
    live = dashboard_context(
        snapshot(running=(running_row(),)),
        groups,
        closed_1d=1,
        closed_7d=2,
        runs_1d=3,
        runs_7d=4,
        now=NOW,
        labels=GitHubLabels(),
    )
    assert live["unavailable"] is None
    assert (live["worker"]["status"], live["worker"]["tick_count"]) == ("ok", 41)
    assert live["hero"] == {
        "closed_1d": 1,
        "closed_7d": 2,
        "runs_1d": 3,
        "runs_7d": 4,
        "running": 1,
        "retrying": 0,
        "cost_usd": 1.25,
        "total_tokens": 1050,
    }
    assert [column["role"] for column in live["columns"]] == list(groups)
    assert [column["label"] for column in live["columns"]] == list(GitHubLabels().as_tuple())
    assert [column["capped"] for column in live["columns"]] == [False, False, False, False, True]
    assert live["running"][0]["issue_number"] == 7
    empty = dashboard_context(
        None, groups, closed_1d=0, closed_7d=0, runs_1d=0, runs_7d=0, now=NOW, labels=GitHubLabels()
    )
    assert empty["worker"] == {"status": "none"}
    assert empty["hero"]["cost_usd"] == 0.0 and empty["running"] == []
