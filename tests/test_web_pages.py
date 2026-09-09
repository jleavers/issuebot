"""Tests for the HTML pages, the live partial, the raw text routes and the static files."""

import re
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
    limits,
    retry_row,
    run_row,
    running_row,
    snapshot,
)
from issuebot.config import GitHubLabels
from issuebot.db import (
    BOARD_LIMIT,
    ISSUE_LIST_LIMIT,
    PROMPT_LIMIT,
    STDERR_LIMIT,
    RunTotals,
    StoreUnavailableError,
)
from issuebot.orchestrator.state import DispatchHold
from issuebot.web import LIVE_POLL_S, SECURITY_HEADERS
from issuebot.web.views import (
    age_text,
    compact,
    dashboard_context,
    duration_text,
    issue_filters,
    money,
    stamp_text,
    thousands,
)

CSS = (files("issuebot.web") / "static" / "app.css").read_text(encoding="utf-8")
HOLD = DispatchHold(
    kind="auth",
    reason="claude authentication unavailable: not logged in",
    since=NOW - timedelta(minutes=4),
)


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


def hero(text: str) -> str:
    """Just the hero section, so a label or a value elsewhere on the page cannot be mistaken."""
    start = text.index('<section class="hero">')
    return text[start : text.index("</section>", start)]


RUN_ID_2 = "20260904T210000Z-abcdef"
HOSTILE = "<script>alert(1)</script>"
PR_URL = "https://github.com/jleavers/issuebot/pull/26"
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
    assert "javascript:" not in text and "PR#4" in text
    assert "<script>" not in text and ' style="' not in text  # the CSP forbids inline code
    assert "example/repo" in text


def test_the_dashboard_costs_the_windows_not_the_worker_process(h: Harness) -> None:
    """The snapshot's totals restart with the worker; the tiles must survive a restart."""
    h.queries.snapshot_row = snapshot()  # ClaudeTotals: $1.25 and 1,050 tokens this process
    h.queries.totals[1] = RunTotals(input_tokens=200_000, output_tokens=3_000, cost_usd=4.5)
    h.queries.totals[7] = RunTotals(input_tokens=1_200_000, output_tokens=9_000, cost_usd=42.66)
    section = hero(html(h.client.get("/partials/dashboard")))
    assert "$4.50" in section and "$42.66" in section
    assert "203,000" in section and "1,209,000" in section
    assert "since start" not in section and "$1.25" not in section and "1,050" not in section


def test_the_hero_is_one_tile_per_metric(h: Harness) -> None:
    """Six tiles, each carrying both windows: ten of them orphaned the last onto its own row."""
    h.queries.closed = {1: 6, 7: 5}
    h.queries.runs = {1: 8, 7: 7}
    h.queries.totals[1] = RunTotals(input_tokens=200_000, output_tokens=3_000, cost_usd=4.5)
    h.queries.totals[7] = RunTotals(input_tokens=1_200_000, output_tokens=9_000, cost_usd=42.66)
    h.queries.snapshot_row = snapshot(rate_limits=limits(0.42, 0.32))
    section = hero(html(h.client.get("/partials/dashboard")))
    assert section.count('<div class="tile">') == 6
    assert re.findall(r'<div class="label">([^<]+)</div>', section) == [
        "closed",
        "agents run",
        "cost (effort)",
        "tokens",
        "limits",
        "activity",
    ]
    assert re.findall(r'<div class="value">([^<]+)</div>', section) == [
        "6",
        "5",
        "8",
        "7",
        "$4.50",
        "$42.66",
        "203K",
        "1.2M",
        "42%",
        "32%",
        "0",
        "0",
    ]
    assert 'title="203,000"' in section and 'title="1,209,000"' in section
    assert section.count('<div class="span">1 day</div>') == 4
    assert section.count('<div class="span">7 days</div>') == 4
    assert section.count('<div class="span">5-hour</div>') == 1
    assert section.count('<div class="span">7-day</div>') == 1
    assert section.count('<div class="span">running</div>') == 1
    assert section.count('<div class="span">retrying</div>') == 1
    assert 'class="meter"' in section


def test_every_hero_tile_carries_two_windows(h: Harness) -> None:
    """The merge of running and retrying is what makes the layout uniform."""
    h.queries.snapshot_row = snapshot(rate_limits=limits())
    section = hero(html(h.client.get("/partials/dashboard")))
    assert section.count('<div class="windows">') == 6
    assert section.count('<div class="window"') == 12


def test_the_limits_tile_is_not_available_on_an_api_key(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(credential="api_key", rate_limits=limits())
    section = hero(html(h.client.get("/partials/dashboard")))
    assert "cost (actual)" in section and "cost (effort)" not in section
    assert section.count(">N/A<") == 2
    assert "has no usage windows" in section
    assert 'class="meter"' not in section
    assert section.count('<div class="tile">') == 6


def test_the_limits_tile_says_not_yet_before_any_reading(h: Harness) -> None:
    """A dash, not N/A: nothing has run, rather than nothing can ever apply."""
    h.queries.snapshot_row = snapshot()
    section = hero(html(h.client.get("/partials/dashboard")))
    assert section.count(">\u2014<") == 2 and "N/A" not in section
    assert "no reading yet; one arrives while a turn is running" in section
    assert 'class="meter"' not in section


def test_a_window_past_its_reset_draws_an_empty_meter(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(
        rate_limits=limits(0.42, 0.32, five_resets_in=-timedelta(minutes=1))
    )
    section = hero(html(h.client.get("/partials/dashboard")))
    assert '<div class="value">0%</div>' in section
    # A <progress>, not a styled div: the CSP has no unsafe-inline, so a width cannot be
    # an inline style, and the element announces itself to a screen reader for free.
    assert 'value="0" max="100"' in section and 'value="32" max="100"' in section


def test_the_hero_columns_always_divide_the_tiles() -> None:
    """Six across, then three, then two: every breakpoint fills its rows exactly."""
    columns = [int(count) for count in re.findall(r"\.hero \{[^}]*repeat\((\d+),", CSS)]
    assert columns and all(6 % count == 0 for count in columns)


def test_the_dashboard_shows_a_running_agent_and_a_retry(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(running=(running_row(),), retrying=(retry_row(),))
    text = html(h.client.get("/"))
    assert 'class="panel worker ok"' in text and "tick 41" in text
    assert "turn_activity" in text and "20 s ago" in text
    assert "Retrying" in text and "turn_failed: boom" in text


def test_the_running_row_draws_its_issue_the_way_a_card_does(h: Harness) -> None:
    """#41: the number was a bare accent link and the title a run of body text.

    It is the same reference a Kanban card carries, so it is the same markup: the number
    as the chip, the title with the weight, both inside the one link to the issue page.
    """
    h.queries.snapshot_row = snapshot(running=(running_row(),))
    text = html(h.client.get("/partials/dashboard"))
    assert (
        '<a class="issue-ref" href="/issues/7">'
        '<span class="chip">#7</span>'
        '<span class="text">Add a power function</span></a>'
    ) in text
    # the old shape: the number linked on its own, with the title loose beside it
    assert '<a href="/issues/7">#7</a>' not in text


def test_the_retrying_row_draws_its_issue_the_same_way(h: Harness) -> None:
    """The identical cell one panel below; a styled row above a plain one is the defect.

    The `.text` slot holds the issue's title, which is what #42 gave the row to put there.
    """
    h.queries.snapshot_row = snapshot(retrying=(retry_row(),))
    text = html(h.client.get("/partials/dashboard"))
    assert (
        '<a class="issue-ref" href="/issues/9">'
        '<span class="chip">#9</span>'
        '<span class="text">Retry the flaky import</span></a>'
    ) in text
    assert '<a href="/issues/9">#9</a>' not in text


def test_the_running_row_keeps_its_markers_beside_the_link(h: Harness) -> None:
    """(rework) and (resumed) sit outside the <a>, so the link must not be a block.

    `inline-flex` shrinks it to its contents, which puts the markers beside a title that
    fits on one line (an inline-flex box is atomic, so a title long enough to wrap pushes
    them below it, which is where they belong). The card overrides the display to a block
    `flex`, being the one surface where the link is the full width and nothing follows it.
    """
    h.queries.snapshot_row = snapshot(running=(running_row(rework=True, resumed=True),))
    text = html(h.client.get("/partials/dashboard"))
    assert '</a> <span class="muted">(rework)</span> <span class="muted">(resumed)</span>' in text
    assert css_declarations(".issue-ref {")["display"] == "inline-flex"
    assert css_declarations(".card .issue-ref {")["display"] == "flex"


def test_the_running_title_is_still_escaped(h: Harness) -> None:
    """The title moved into a new element; it must not have picked up markup on the way."""
    h.queries.snapshot_row = snapshot(running=(running_row(title=HOSTILE),))
    text = html(h.client.get("/partials/dashboard"))
    assert HOSTILE not in text
    assert f'<span class="text">{ESCAPED}</span>' in text


def test_one_rule_gives_the_card_and_the_tables_their_hover_and_focus() -> None:
    """The states are on .issue-ref, not on .card .title, or the tables would not get them."""
    assert ".issue-ref:hover { color: var(--accent); text-decoration: none; }" in CSS
    assert ".issue-ref:hover .text { text-decoration: underline; }" in CSS
    assert ".card .title" not in CSS, "`title` is gone; .issue-ref is the one hook"
    assert ".card:has(.issue-ref:hover) {" in CSS


def test_a_retrying_row_names_the_issue_rather_than_repeating_its_number(h: Harness) -> None:
    """The cell used to read `#9 repo-9`: the identifier is the number again (#42).

    `identifier` is `<repo>-<number>`, so beside a chip that already states the number it
    said nothing about the issue. The `.text` slot #41 gave the row is a title's slot, and
    the row carries a title to put in it now; the identifier is not drawn anywhere.
    """
    h.queries.snapshot_row = snapshot(retrying=(retry_row(),))
    text = html(h.client.get("/partials/dashboard"))
    row = text.split("Retrying", 1)[1].split("</table>", 1)[0]
    assert '<span class="text">Retry the flaky import</span>' in row
    assert "repo-9" not in row


def test_a_retrying_title_is_escaped(h: Harness) -> None:
    """The cell used to hold `identifier`, a sanitised `<repo>-<number>` slug (#42).

    It holds a human-written issue title now, so it is the first arbitrary text to reach
    this row; autoescape covers it, and this is what says so.
    """
    h.queries.snapshot_row = snapshot(retrying=(retry_row(title=HOSTILE),))
    text = html(h.client.get("/partials/dashboard"))
    assert HOSTILE not in text
    assert f'<span class="text">{ESCAPED}</span>' in text


def test_a_retrying_row_written_before_the_title_existed(h: Harness) -> None:
    """A snapshot from an older worker has no title, and the cell must not read `None`.

    The identifier it used to fall back on is gone, and a worker that fails startup never
    overwrites the snapshot it found - so this is not always one poll interval long.
    """
    row = snapshot(retrying=(retry_row(),))
    del row.data["retrying"][0]["title"]
    h.queries.snapshot_row = row
    text = html(h.client.get("/partials/dashboard"))
    retrying = text.split("Retrying", 1)[1].split("</table>", 1)[0]
    assert '<span class="text">-</span>' in retrying and "None" not in retrying


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


def test_the_worker_facts_are_bounded_rather_than_run_together(h: Harness) -> None:
    """The reported defect (#47): the runtime facts and the verdict read as one sentence.

    They were one `.muted` span and another sitting side by side, so nothing but the flex
    gap divided "2 slots" from "config valid" and the pair read as prose with a double
    space in it. Each fact is its own bounded chip now and the verdict is a different shape
    entirely, so no two neighbours on the line share a treatment.
    """
    h.queries.snapshot_row = snapshot()
    text = html(h.client.get("/partials/dashboard"))
    line = text[text.index('<section class="panel worker') :]
    line = line[: line.index("</section>")]
    assert '<span class="fact">tick 41</span>' in line
    assert '<span class="fact">poll 30000 ms</span>' in line
    assert '<span class="fact">2 slots</span>' in line
    # the verdict is not a fourth fact, and no `.muted` run is left to blur into it
    assert '<span class="verdict ok">config valid</span>' in line
    assert "muted" not in line
    # each chip is drawn, not merely spaced: a border delimits it, as it does the card chip
    fact = css_declarations(".worker .fact {")
    assert fact["border"] == "1px solid var(--line)"
    assert fact["color"] == "var(--muted)"
    # and it holds its shape, as the card chip does: a pill broken over two lines is not one
    assert fact["white-space"] == "nowrap"
    # the verdict is a dot and a sentence, a shape the facts do not have
    assert '.worker .verdict::before { content: "";' in CSS


def test_the_worker_line_names_the_overlay_in_force(h: Harness) -> None:
    """A fourth fact, drawn only when there is one: the dashboard's answer to "is the worker
    running my overrides?"."""
    h.queries.snapshot_row = snapshot(workflow_overlay_path="/configs/WORKFLOW.local.md")
    text = html(h.client.get("/partials/dashboard"))
    line = text[text.index('<section class="panel worker') :]
    line = line[: line.index("</section>")]
    assert '<span class="fact overlay">overlay /configs/WORKFLOW.local.md</span>' in line

    h.queries.snapshot_row = snapshot()
    text = html(h.client.get("/partials/dashboard"))
    assert "overlay" not in text[text.index('<section class="panel worker') :]


def test_an_alerting_verdict_is_prose_in_the_bad_token(h: Harness) -> None:
    """A config error and a held dispatch are verdicts, not facts, and they are sentences.

    `config_error` is `str(ConfigError)`, which runs to one line per invalid setting, and a
    hold names its reason; neither would survive being squeezed into a fully rounded pill.
    """
    row = snapshot(dispatch_hold=HOLD)
    row.data["config_valid"] = False
    row.data["config_error"] = "polling.interval_ms must be >= 1000"
    h.queries.snapshot_row = row
    text = html(h.client.get("/partials/dashboard"))
    line = text[text.index('<section class="panel worker') :]
    line = line[: line.index("</section>")]
    assert '<span class="verdict alert config-error">' in line
    assert '<span class="verdict alert dispatch-hold">' in line
    assert "verdict ok" not in line
    assert ".worker .verdict.alert::before { background: var(--bad); }" in CSS
    # a verdict wraps like the prose it is: a config error runs to one line per bad setting
    assert "nowrap" not in css_declarations(".worker .verdict {").values()


def test_a_kanban_card_separates_the_number_from_the_title(h: Harness) -> None:
    """The number is metadata and the title is the content, so they are separate elements.

    Both stay inside the one link to the issue page, which is what lets the stylesheet lay
    the chip and the title out as a row without giving up a single click target.
    """
    h.queries.groups["todo"] = [issue_row(number=23, title="Add a status badge to the README")]
    text = html(h.client.get("/partials/dashboard"))
    assert (
        '<a class="issue-ref" href="/issues/23">'
        '<span class="chip">#23</span>'
        '<span class="text">Add a status badge to the README</span></a>'
    ) in text


def test_a_kanban_card_draws_its_pull_request_as_the_same_chip(h: Harness) -> None:
    """The two numbers on a card are the same kind of reference, so they are one object.

    The prefix stays inside the chip: a card showing a bare `#24` and a bare `#26` would
    not say which of the two is the issue and which is the pull request. It carries no
    space, so the two halves of the reference read as the one token they are.
    """
    h.queries.groups["review"] = [issue_row(number=24, pr_number=26, pr_url=PR_URL)]
    text = html(h.client.get("/partials/dashboard"))
    assert f'<a class="chip" href="{PR_URL}">PR#26</a>' in text
    assert '<span class="chip">#24</span>' in text
    assert "PR #" not in text


def test_an_unsafe_pull_request_url_still_draws_the_chip(h: Harness) -> None:
    """`safe_href` refuses the scheme, so the chip loses its link, not its shape."""
    h.queries.groups["todo"] = [issue_row(number=3, pr_number=4, pr_url="javascript:alert(1)")]
    text = html(h.client.get("/partials/dashboard"))
    assert '<span class="chip">PR#4</span>' in text
    assert "javascript:" not in text


def test_a_card_without_a_pull_request_keeps_the_meta_row_balanced(h: Harness) -> None:
    """The empty cell is what holds the age at the right end of a `space-between` row."""
    h.queries.groups["todo"] = [issue_row(number=9, pr_number=None, pr_url=None, pr_state=None)]
    text = html(h.client.get("/partials/dashboard"))
    assert '<span class="pr"></span>' in text


def test_a_pull_request_with_no_state_renders_the_chip_alone(h: Harness) -> None:
    """The store writes the number and the state together, so this is a guard, not a case.

    It is still worth holding: unguarded, Jinja renders a null state as the word `None`
    beside the chip, and the meta row is the one place on the card with nothing else in it.
    """
    h.queries.groups["todo"] = [issue_row(number=3, pr_number=4, pr_url=PR_URL, pr_state=None)]
    text = html(h.client.get("/partials/dashboard"))
    assert f'<span class="pr"><a class="chip" href="{PR_URL}">PR#4</a></span>' in text
    assert "None" not in text


def test_every_chip_is_drawn_by_one_rule() -> None:
    """Two rules would drift; #28 asked the pull request to match the card's number, and

    #41 asked the Running table to match the card. The selector is unscoped for that
    reason: a `.card`-scoped rule is what would have made the tables plain again.
    """
    assert ".card .number" not in CSS and ".card .chip {" not in CSS
    declarations = css_declarations(".chip {")
    assert declarations["font-family"].startswith("ui-monospace")
    assert declarations["font-variant-numeric"] == "tabular-nums"
    assert declarations["border"] == "1px solid var(--line)"
    assert declarations["white-space"] == "nowrap", "`PR#26` must not break across lines"


def test_the_pull_request_chip_answers_the_pointer_and_the_keyboard() -> None:
    """It is the one chip that is a link, and it leaves the dashboard for GitHub.

    Hover lights the border and leaves the label at --muted: the chip is subordinate to
    the title beside it, which is what the hover is really about, and a number that also
    changed colour would compete with it. (--accent is legible as text on both surfaces
    since #43, so this is a matter of emphasis rather than of contrast.)
    """
    assert "a.chip:hover { border-color: var(--accent); text-decoration: none; }" in CSS
    assert "a.chip:focus-visible { outline: 2px solid var(--accent);" in CSS


def test_the_meta_row_wraps_around_a_chip_that_cannot() -> None:
    """A column is a grid track with a 180px floor; the chip is `nowrap` inside it.

    `PR#26`, the pull request's state and the age do not fit one line of that, so the row
    has to wrap - otherwise the chip overprints the age at every width under about 1100px.
    Neither flex item may be given `min-width: 0`, which would let it shrink back under
    its own chip and hand the overlap straight back.
    """
    assert css_declarations(".card .meta {")["flex-wrap"] == "wrap"
    pr_cell = css_declarations(".card .meta .pr {")
    assert pr_cell["flex-wrap"] == "wrap"
    assert "min-width" not in pr_cell and "min-width" not in css_declarations(".card .meta {")


def test_the_card_title_is_still_escaped(h: Harness) -> None:
    """The title moved into a new element; it must not have picked up any markup on the way."""
    h.queries.groups["todo"] = [issue_row(title=HOSTILE)]
    text = html(h.client.get("/partials/dashboard"))
    assert HOSTILE not in text
    assert f'<span class="text">{ESCAPED}</span>' in text


def test_the_card_is_laid_out_by_the_stylesheet_alone(h: Harness) -> None:
    """The chips and the title are their own elements; the CSP forbids styling them inline."""
    assert ".chip {" in CSS and ".issue-ref .text {" in CSS and ".card .issue-ref .text {" in CSS
    assert ' style="' not in html(h.client.get("/partials/dashboard"))


def test_a_long_card_title_cannot_stretch_its_column() -> None:
    """A column is a grid track with a 180px floor; an unbroken title would blow past it.

    The standard `line-clamp` is checked as a whole declaration: as a bare substring it is
    also inside `-webkit-line-clamp`, so it could be deleted with the test still green.
    """
    declarations = css_declarations(".card .issue-ref .text {")
    assert declarations["-webkit-line-clamp"] == "3"
    assert declarations["line-clamp"] == "3", "the standard property must ship beside the prefix"
    assert declarations["overflow"] == "hidden"
    # shared with the tables, which need it just as much: it is what breaks a branch name
    assert css_declarations(".issue-ref .text {")["overflow-wrap"] == "anywhere"


def test_the_chip_stays_beside_the_first_line_of_a_wrapped_title() -> None:
    """`overflow` makes the title a scroll container, whose baseline is its bottom edge.

    Aligning the two on the baseline would therefore drop the chip to the last line of a
    wrapped title - the case the clamp exists for. They are aligned to the top instead.
    """
    assert css_declarations(".card .issue-ref {")["align-items"] == "flex-start"


def test_an_issue_reference_is_reachable_by_keyboard() -> None:
    """The card and the two tables are the dashboard's navigation; focus must be visible."""
    assert ".issue-ref:focus-visible { outline: 2px solid var(--accent);" in CSS


def test_only_the_link_lights_the_card_up() -> None:
    """A bare .card:hover would offer a click on the meta row and the padding as well.

    The chip inside the reference is not itself an .issue-ref, so the pull request chip -
    a link off the dashboard rather than a click on this card - cannot match either.
    """
    assert ".card:has(.issue-ref:hover) {" in CSS
    assert ".card:hover {" not in CSS


def test_the_live_partial_shows_a_config_error(h: Harness) -> None:
    row = snapshot()
    row.data["config_valid"] = False
    row.data["config_error"] = "polling.interval_ms must be >= 1000"
    h.queries.snapshot_row = row
    text = html(h.client.get("/partials/dashboard"))
    assert "config error: polling.interval_ms must be &gt;= 1000" in text


def test_the_live_partial_names_a_held_dispatch(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(dispatch_hold=HOLD)
    text = html(h.client.get("/partials/dashboard"))
    assert 'class="panel worker held"' in text
    assert "worker held" in text
    assert "not claiming (auth, held since 2026-09-04T11:56:00Z)" in text
    assert "claude authentication unavailable: not logged in" in text
    # Nothing is held by default, so the line appears only when it should.
    h.queries.snapshot_row = snapshot()
    assert "not claiming" not in html(h.client.get("/partials/dashboard"))


def test_a_held_worker_badge_is_marked_up_like_the_other_states() -> None:
    assert ".worker.stale .badge, .worker.held .badge { background: var(--warn); }" in CSS
    assert ".worker .config-error, .worker .dispatch-hold { color: var(--bad); }" in CSS


def test_a_capped_column_counts_them_all_and_links_to_the_rest(h: Harness) -> None:
    """The header counts the whole column; the footer links to what the board left off."""
    h.queries.groups["complete"] = [
        issue_row(number=n, state="complete", github_state="closed")
        for n in range(1, BOARD_LIMIT + 1)
    ]
    h.queries.counts["complete"] = 44
    text = html(h.client.get("/partials/dashboard"))
    assert '<span class="count">44</span>' in text
    assert 'href="/issues?state=complete"' in text
    assert "39 more" in text


def test_a_column_inside_the_cap_has_no_overflow_link(h: Harness) -> None:
    h.queries.groups["complete"] = [
        issue_row(number=n, state="complete", github_state="closed") for n in range(1, 4)
    ]
    h.queries.counts["complete"] = 3
    text = html(h.client.get("/partials/dashboard"))
    assert '<span class="count">3</span>' in text
    assert "more</a>" not in text


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


# --- the issues list page ---------------------------------------------------------------------


def test_the_issues_page_lists_a_row_per_issue(h: Harness) -> None:
    h.queries.issue_list = [
        issue_row(number=7),
        issue_row(
            number=12,
            state="complete",
            state_label="issuebot/complete",
            github_state="closed",
            closed_at=NOW - timedelta(hours=2),
            pr_number=None,
            pr_url=None,
            pr_state=None,
            title="Cache the workflow",
        ),
    ]
    response = h.client.get("/issues")
    assert response.status_code == 200
    text = html(response)
    assert 'href="/issues/7"' in text and 'href="/issues/12"' in text
    assert "Add a power function" in text and "Cache the workflow" in text
    assert 'class="state review"' in text and 'class="state complete"' in text
    assert h.queries.state_asked is None  # no filter: every column


def test_the_issues_page_filters_to_one_state(h: Harness) -> None:
    h.queries.counts["complete"] = 44
    h.queries.issue_list = [issue_row(number=12, state="complete", github_state="closed")]
    text = html(h.client.get("/issues?state=complete"))
    assert h.queries.state_asked == "complete"
    assert 'class="filter current" href="/issues?state=complete"' in text
    assert "44" in text


def test_the_issues_page_offers_a_filter_per_column_and_an_all(h: Harness) -> None:
    text = html(h.client.get("/issues"))
    for role in ("todo", "in_progress", "review", "rework", "complete"):
        assert f'href="/issues?state={role}"' in text
    assert 'class="filter current" href="/issues"' in text


def test_an_unknown_state_filter_is_a_404_page(h: Harness) -> None:
    response = h.client.get("/issues?state=mystery")
    assert response.status_code == 404
    text = html(response)
    assert "<h1>404</h1>" in text and "mystery" in text
    assert "issues_for_state" not in h.queries.calls


def test_the_issues_page_notes_a_truncated_list(h: Harness) -> None:
    h.queries.issue_list = [issue_row(number=n) for n in range(ISSUE_LIST_LIMIT)]
    text = html(h.client.get("/issues"))
    assert f"the {ISSUE_LIST_LIMIT} most recent" in text
    h.queries.issue_list.pop()
    assert "most recent" not in html(h.client.get("/issues"))


def test_the_issues_page_says_when_a_column_is_empty(h: Harness) -> None:
    text = html(h.client.get("/issues?state=rework"))
    assert "no issues" in text


def test_the_issues_page_escapes_the_title(h: Harness) -> None:
    h.queries.issue_list = [issue_row(title=HOSTILE)]
    text = html(h.client.get("/issues"))
    assert HOSTILE not in text and ESCAPED in text


def test_the_issues_page_survives_a_database_error(h: Harness) -> None:
    h.queries.error = StoreUnavailableError("cannot connect: refused")
    response = h.client.get("/issues")
    assert response.status_code == 503


def test_every_page_links_to_the_issues_list(h: Harness) -> None:
    for path in ("/", "/issues"):
        assert '<a href="/issues">issues</a>' in html(h.client.get(path))


def test_issue_filters_mark_the_current_column() -> None:
    filters = issue_filters("review", {"todo": 2, "review": 1, "complete": 44}, GitHubLabels())
    assert [entry["label"] for entry in filters] == [
        "all",
        "issuebot/todo",
        "issuebot/in-progress",
        "issuebot/review",
        "issuebot/rework",
        "issuebot/complete",
    ]
    assert [entry["current"] for entry in filters] == [False, False, False, True, False, False]
    assert [entry["total"] for entry in filters] == [47, 2, 0, 1, 0, 44]
    assert filters[0]["href"] == "/issues"
    assert filters[3]["href"] == "/issues?state=review"
    assert issue_filters(None, {}, GitHubLabels())[0]["current"] is True


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


def test_compact_abbreviates_large_counts() -> None:
    """The hero's token figures run to ten digits; the tile shows the magnitude."""
    assert compact(0) == "0"
    assert compact(950) == "950"
    assert compact(1_000) == "1.0K"
    assert compact(203_000) == "203K"
    assert compact(39_160_357) == "39.2M"
    assert compact(1_209_000_000) == "1.2B"
    assert compact(None) == "0"


def test_dashboard_context() -> None:
    groups = {role: [] for role in ("todo", "in_progress", "review", "rework", "complete")}
    groups["complete"] = [issue_row(number=n, state="complete") for n in range(BOARD_LIMIT)]
    counts = dict.fromkeys(groups, 0) | {"complete": 44}
    live = dashboard_context(
        snapshot(running=(running_row(),)),
        groups,
        counts=counts,
        closed_1d=1,
        closed_7d=2,
        runs_1d=3,
        runs_7d=4,
        totals_1d=RunTotals(input_tokens=200, output_tokens=20, cost_usd=0.25),
        totals_7d=RunTotals(input_tokens=210, output_tokens=21, cost_usd=0.35),
        now=NOW,
        labels=GitHubLabels(),
    )
    assert live["unavailable"] is None
    assert (live["worker"]["status"], live["worker"]["tick_count"]) == ("ok", 41)
    assert live["worker"]["dispatch_hold"] is None
    assert live["hero"] == {
        "closed_1d": 1,
        "closed_7d": 2,
        "runs_1d": 3,
        "runs_7d": 4,
        "running": 1,
        "retrying": 0,
        "cost_1d": 0.25,
        "cost_7d": 0.35,
        "cost_label": "cost (effort)",
        "tokens_1d": 220,
        "tokens_7d": 231,
        "limits": [],
        "limits_unavailable": {
            "value": "\u2014",
            "title": "no reading yet; one arrives while a turn is running",
        },
    }
    assert [column["role"] for column in live["columns"]] == list(groups)
    assert [column["label"] for column in live["columns"]] == list(GitHubLabels().as_tuple())
    assert [column["total"] for column in live["columns"]] == [0, 0, 0, 0, 44]
    assert [column["overflow"] for column in live["columns"]] == [0, 0, 0, 0, 39]
    assert live["running"][0]["issue_number"] == 7
    zero = RunTotals(input_tokens=0, output_tokens=0, cost_usd=0.0)
    empty = dashboard_context(
        None,
        groups,
        counts=counts,
        closed_1d=0,
        closed_7d=0,
        runs_1d=0,
        runs_7d=0,
        totals_1d=zero,
        totals_7d=zero,
        now=NOW,
        labels=GitHubLabels(),
    )
    assert empty["worker"] == {"status": "none"}
    assert empty["hero"]["cost_7d"] == 0.0 and empty["running"] == []
    assert empty["hero"]["cost_label"] == "cost" and empty["hero"]["limits"] == []
    assert empty["hero"]["limits_unavailable"]["value"] == "\u2014"


def test_dashboard_context_carries_a_held_dispatch() -> None:
    live = dashboard_context(
        snapshot(dispatch_hold=HOLD),
        {role: [] for role in ("todo", "in_progress", "review", "rework", "complete")},
        counts={},
        closed_1d=0,
        closed_7d=0,
        runs_1d=0,
        runs_7d=0,
        totals_1d=RunTotals(input_tokens=0, output_tokens=0, cost_usd=0.0),
        totals_7d=RunTotals(input_tokens=0, output_tokens=0, cost_usd=0.0),
        now=NOW,
        labels=GitHubLabels(),
    )
    assert live["worker"]["status"] == "held"
    assert live["worker"]["dispatch_hold"] == {
        "kind": "auth",
        "reason": "claude authentication unavailable: not logged in",
        "since": (NOW - timedelta(minutes=4)).isoformat(),
    }
