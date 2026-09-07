"""Tests for the light/dark theme: the toggle, the stamp, the tokens and their contrast.

The two duplicated dark blocks in ``app.css`` (one for the OS preference, one for the
explicit choice) are the maintenance risk in this design, so they are checked against each
other here, and every dark mark is measured against the surface it sits on.
"""

from collections.abc import Iterator
from importlib.resources import files
from typing import Any

import pytest

from fakes.web import RUN_ID, Harness

CSS = (files("issuebot.web") / "static" / "app.css").read_text(encoding="utf-8")
THEME_JS = (files("issuebot.web") / "static" / "theme.js").read_text(encoding="utf-8")
APP_JS = (files("issuebot.web") / "static" / "app.js").read_text(encoding="utf-8")

# a graphical mark (a bar, a border, an icon) needs 3:1 against its background; body text
# needs 4.5:1. WCAG 2.2, 1.4.11 and 1.4.3.
MARK_FLOOR = 3.0
TEXT_FLOOR = 4.5


@pytest.fixture
def h() -> Iterator[Harness]:
    harness = Harness()
    with harness.client:
        yield harness


def tokens(selector: str) -> dict[str, str]:
    """The custom properties declared in the one block that starts with ``selector``."""
    start = CSS.index(selector)
    block = CSS[CSS.index("{", start) + 1 : CSS.index("}", start)]
    found = {}
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("--"):
            name, _, value = line.partition(":")
            found[name.strip()] = value.strip().rstrip(";")
    return found


LIGHT = tokens(":root {")
OS_DARK = tokens(':root:where(:not([data-theme="light"]))')
PICKED_DARK = tokens(':root[data-theme="dark"] {')


def relative_luminance(colour: str) -> float:
    channels = [int(colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(one: str, other: str) -> float:
    first, second = relative_luminance(one), relative_luminance(other)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def html(response: Any) -> str:
    assert response.headers["content-type"].startswith("text/html")
    return response.text


# --- the toggle and the stamp -----------------------------------------------------------------


def test_theme_js_is_served(h: Harness) -> None:
    response = h.client.get("/static/theme.js")
    assert response.status_code == 200
    kind = response.headers["content-type"]
    assert kind.startswith(("text/javascript", "application/javascript")), kind
    assert "issuebot-theme" in response.text


def test_every_page_carries_the_toggle_and_the_script(h: Harness) -> None:
    h.seed_issue()
    pages = (
        h.client.get("/"),
        h.client.get("/issues/7"),
        h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1"),
        h.client.get("/nothing"),  # the error page inherits the header too
    )
    for response in pages:
        text = html(response)
        assert '<script src="/static/theme.js"></script>' in text, response.url
        assert 'class="theme-toggle"' in text, response.url
        # the toggle sits last in the header nav, after the three links
        assert text.index('href="/healthz"') < text.index('class="theme-toggle"'), response.url
        assert text.index("</nav>") > text.index('class="theme-toggle"'), response.url
        # both icons ship; CSS picks the one for the theme the click would move to
        assert 'class="moon"' in text and 'class="sun"' in text, response.url


def test_the_script_runs_before_the_first_paint(h: Harness) -> None:
    """In <head>, and synchronous: a deferred script would flash the light theme first."""
    text = html(h.client.get("/"))
    tag = '<script src="/static/theme.js"></script>'
    assert text.index(tag) < text.index("</head>") < text.index("<body>")
    assert "defer" not in tag and "async" not in tag


def test_the_toggle_is_labelled_and_only_shown_when_the_script_ran() -> None:
    assert 'aria-pressed="false"' in (files("issuebot.web") / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    assert "html:not(.js) .theme-toggle { display: none; }" in CSS
    assert 'root.classList.add("js")' in THEME_JS
    assert "aria-label" in THEME_JS and "aria-pressed" in THEME_JS


def test_the_choice_is_persisted_so_every_page_inherits_it() -> None:
    assert 'var KEY = "issuebot-theme"' in THEME_JS
    assert "localStorage.getItem(KEY)" in THEME_JS
    assert "localStorage.setItem(KEY, next)" in THEME_JS
    # storage can throw (disabled, or full); the page must still render
    assert THEME_JS.count("catch (error)") >= 2


def test_no_stored_choice_means_the_os_decides() -> None:
    assert 'window.matchMedia("(prefers-color-scheme: dark)")' in THEME_JS
    # nothing stored leaves data-theme off the element, so the media query stays in charge
    assert "if (initial) {\n    root.dataset.theme = initial;\n  }" in THEME_JS


# --- the tokens -------------------------------------------------------------------------------


def test_the_two_dark_blocks_agree() -> None:
    """The OS-preference block and the explicit-choice block must not drift apart."""
    assert OS_DARK == PICKED_DARK
    assert set(OS_DARK) == set(LIGHT)
    assert OS_DARK != LIGHT


def test_the_explicit_choice_outranks_the_os_preference() -> None:
    # :where() has zero specificity, so :root[data-theme="dark"] wins under a light OS,
    # and the :not() guard lets an explicit "light" win under a dark OS.
    assert ':root:where(:not([data-theme="light"]))' in CSS
    media = CSS.index("@media (prefers-color-scheme: dark)")
    assert media < CSS.index(':root[data-theme="dark"] {')
    assert "color-scheme: dark;" in CSS and "color-scheme: light;" in CSS


@pytest.mark.parametrize("theme,surface", [(LIGHT, "--panel"), (OS_DARK, "--panel")])
def test_marks_clear_the_contrast_floor_on_the_panel(theme: dict[str, str], surface: str) -> None:
    """Every bar, pill background and status colour against the surface it is drawn on."""
    background = theme[surface]
    for name in (
        "--chart-closed",
        "--chart-runs",
        "--accent",
        "--ok",
        "--warn",
        "--bad",
        "--todo",
        "--in_progress",
        "--review",
        "--rework",
        "--complete",
    ):
        ratio = contrast(theme[name], background)
        assert ratio >= MARK_FLOOR, f"{name} {theme[name]} on {background} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", [LIGHT, OS_DARK])
def test_text_clears_the_contrast_floor(theme: dict[str, str]) -> None:
    for background in (theme["--panel"], theme["--bg"]):
        for name in ("--ink", "--muted"):
            ratio = contrast(theme[name], background)
            assert ratio >= TEXT_FLOOR, f"{name} {theme[name]} on {background} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", [LIGHT, OS_DARK])
def test_a_pill_label_is_legible_on_every_solid_colour(theme: dict[str, str]) -> None:
    """--on-solid is why the dark theme can use hues too bright to carry white text."""
    for name in (
        "--muted",
        "--ok",
        "--warn",
        "--todo",
        "--in_progress",
        "--review",
        "--rework",
        "--complete",
    ):
        ratio = contrast(theme["--on-solid"], theme[name])
        assert ratio >= MARK_FLOOR, f"--on-solid on {name} {theme[name]} is {ratio:.2f}:1"


def test_the_dark_theme_fixes_the_bar_that_clashed() -> None:
    """The reported defect: the "issues closed" violet is unreadable on a dark panel."""
    assert contrast(LIGHT["--chart-closed"], OS_DARK["--panel"]) < MARK_FLOOR
    assert contrast(OS_DARK["--chart-closed"], OS_DARK["--panel"]) >= MARK_FLOOR


# --- the charts -------------------------------------------------------------------------------


def test_the_charts_read_their_colours_from_the_stylesheet() -> None:
    for name in ("--chart-closed", "--chart-runs", "--chart-ink", "--chart-grid"):
        assert name in APP_JS, name
        assert name in LIGHT and name in OS_DARK, name
    assert "getComputedStyle(document.documentElement)" in APP_JS
    # the axis furniture too: Chart.js would otherwise paint it near-black on a dark panel
    assert "ticks: { color: colors.ink }" in APP_JS
    assert "grid: { color: colors.grid }" in APP_JS


def test_the_charts_repaint_when_the_theme_changes() -> None:
    assert 'document.addEventListener("issuebot:themechange", render)' in APP_JS
    assert 'CustomEvent("issuebot:themechange"' in THEME_JS
    # repainting must not refetch: the series is kept and redrawn
    assert "series = body.series" in APP_JS


def test_the_theme_introduces_no_inline_code(h: Harness) -> None:
    h.seed_issue()
    for response in (h.client.get("/"), h.client.get("/issues/7"), h.client.get("/nothing")):
        text = html(response)
        assert ' style="' not in text, response.url
        assert "<style" not in text, response.url
        assert "onclick=" not in text, response.url
