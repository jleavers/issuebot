"""Tests for the light/dark theme: the toggle, the stamp, the tokens and their contrast.

The two duplicated dark blocks in ``app.css`` (one for the OS preference, one for the
explicit choice) are the maintenance risk in this design, so they are checked against each
other here, and every dark mark is measured against the surface it sits on.

There is no JavaScript engine in the test environment, so the scripts are only held to the
contracts they share with the stylesheet and with each other - the token names and the
repaint event. Their behaviour (the click, the persistence, the inheritance across pages,
the absence of a flash) is verified in a browser and recorded on the pull request.
"""

import re
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
        # a control, not navigation: beside the nav, still inside the header
        assert text.index("</nav>") < text.index('class="theme-toggle"'), response.url
        assert text.index('class="theme-toggle"') < text.index("</header>"), response.url
        # both icons ship; CSS picks the one for the theme the click would move to
        assert 'class="moon"' in text and 'class="sun"' in text, response.url


def test_the_script_runs_before_the_first_paint(h: Harness) -> None:
    """In <head>, and synchronous: a deferred script would flash the light theme first."""
    text = html(h.client.get("/"))
    tag = '<script src="/static/theme.js"></script>'
    assert text.index(tag) < text.index("</head>") < text.index("<body>")
    assert "defer" not in tag and "async" not in tag


def test_the_toggle_is_reachable_and_labelled(h: Harness) -> None:
    text = html(h.client.get("/"))
    # a real <button>, so it carries the role, the keyboard behaviour and the focus ring
    assert '<button class="theme-toggle" type="button"' in text
    # the name describes the action, so it must not also claim a pressed state
    assert 'aria-label="Switch the colour theme"' in text
    assert "aria-pressed" not in text and "aria-pressed" not in THEME_JS
    # the icons are decoration; the label carries the meaning
    assert text.count('aria-hidden="true"') >= 2
    assert ".theme-toggle:focus-visible" in CSS


def test_the_toggle_is_not_offered_without_javascript() -> None:
    """It cannot do anything without the script, so app.css only reveals it once it ran."""
    assert "html:not(.js) .theme-toggle" in CSS and "display: none" in CSS
    assert 'classList.add("js")' in THEME_JS


def test_storage_failures_cannot_break_the_page() -> None:
    """localStorage throws when it is disabled or full; both accesses must be guarded."""
    reads_and_writes = THEME_JS.count("localStorage.")
    assert reads_and_writes == 2, THEME_JS.count("localStorage.")
    assert THEME_JS.count("catch (error)") >= reads_and_writes


def test_the_applied_theme_outranks_storage_when_deciding_what_is_showing() -> None:
    """The toggle must keep toggling when the write fails, not stick after one click.

    Deriving the current theme from localStorage alone means an unwritable store makes
    every click compute the same "next" theme, and the button goes dead after the first.
    """
    body = THEME_JS[THEME_JS.index("function showing()") :]
    body = body[: body.index("\n  }")]
    assert body.index("root.dataset.theme") < body.index("stored()")


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


def test_the_issue_reference_and_the_card_name_no_colour_of_their_own() -> None:
    """The restyle is token-only, which is the whole reason it follows into dark.

    A literal (a hex, an rgb(), a named colour) or a token that only one theme declares
    would light up in light and go wrong, or invisible, in dark - and neither the drift
    check above nor the contrast checks below would see it. The slice starts at the shared
    .chip rule so that the tables' half of the restyle is covered too, not just the card's.
    """
    rules = CSS[CSS.index(".chip {") : CSS.index("/* banners */")]
    used = set(re.findall(r"var\((--[a-z_-]+)\)", rules))
    assert used <= set(LIGHT), used - set(LIGHT)
    assert used <= set(OS_DARK), used - set(OS_DARK)
    # every colour value in the block is a var(); nothing is written out longhand
    for declaration in re.findall(r"(?:color|background|box-shadow|outline):[^;]+;", rules):
        assert "var(--" in declaration, declaration
        assert not re.search(r"#[0-9a-f]{3}|rgb|hsl", declaration), declaration


@pytest.mark.parametrize("theme", [LIGHT, OS_DARK])
def test_the_chip_hover_border_clears_the_mark_floor_on_both_surfaces(
    theme: dict[str, str],
) -> None:
    """`a.chip:hover` lights the border to --accent wherever the chip is drawn.

    On a card that is --bg, and in the tables --panel; the ink stays --muted either way,
    which is why the hover is a mark measurement and not a text one.
    """
    for surface in ("--bg", "--panel"):
        ratio = contrast(theme["--accent"], theme[surface])
        assert ratio >= MARK_FLOOR, f"--accent on {surface} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", [LIGHT, OS_DARK])
def test_the_kanban_card_marks_clear_the_mark_floor(theme: dict[str, str]) -> None:
    """A card sits on --bg, not on the --panel its column is painted with.

    The stripe down a card's left edge is the column's own state colour and the hover
    border is --accent, so both are measured against the card, one surface further in.
    Light is the tighter of the two (--bg is darker than --panel there, and the marks are
    dark); --in_progress is the closest.
    """
    background = theme["--bg"]
    for name in ("--todo", "--in_progress", "--review", "--rework", "--complete", "--accent"):
        ratio = contrast(theme[name], background)
        assert ratio >= MARK_FLOOR, f"{name} {theme[name]} on {background} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", [LIGHT, OS_DARK])
@pytest.mark.parametrize("surface", ["--bg", "--panel"])
def test_the_number_chip_is_drawn_by_its_border(theme: dict[str, str], surface: str) -> None:
    """The chip has no fill, so --line has to carry it, as the gridlines carry the charts.

    No token in the set is both a perceptible step off the surface and dark enough to keep
    --muted legible on top (--panel is 1.07:1 off --bg in light, and --line drops --muted
    to 3.58:1), so the chip is drawn with a border and read as monospace instead. That
    border only has to be visible, not to clear the mark floor: it delimits the chip, it
    does not carry the number, and the digits themselves are held to the text floor.

    Both surfaces, because one rule now draws the chip on a card (--bg) and in the
    Running and Retrying tables, which are .panel (--panel).
    """
    background = theme[surface]
    edge = contrast(theme["--line"], background)
    assert 1.2 <= edge < MARK_FLOOR, f"--line on {surface} is {edge:.2f}:1"
    assert contrast(theme["--muted"], background) >= TEXT_FLOOR


@pytest.mark.parametrize("theme", [LIGHT, OS_DARK])
def test_text_clears_the_contrast_floor(theme: dict[str, str]) -> None:
    for background in (theme["--panel"], theme["--bg"]):
        for name in ("--ink", "--muted", "--chart-ink"):
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


def test_the_gridlines_are_decorative_but_still_visible() -> None:
    """The one token deliberately under the mark floor, and why.

    WCAG 1.4.11 covers graphics required to understand the content; gridlines are not one
    (the tick labels are, and they are text held to --chart-ink's floor). A grid at 3:1
    would compete with the bars it exists to measure. It must still be distinguishable.
    """
    for theme in (LIGHT, OS_DARK):
        ratio = contrast(theme["--chart-grid"], theme["--panel"])
        assert 1.2 <= ratio < MARK_FLOOR, ratio
        assert contrast(theme["--chart-ink"], theme["--panel"]) > ratio


def test_the_dark_theme_fixes_the_bar_that_clashed() -> None:
    """The reported defect: the "issues closed" violet is unreadable on a dark panel."""
    assert contrast(LIGHT["--chart-closed"], OS_DARK["--panel"]) < MARK_FLOOR
    assert contrast(OS_DARK["--chart-closed"], OS_DARK["--panel"]) >= MARK_FLOOR


# --- the charts -------------------------------------------------------------------------------


def test_every_chart_token_the_script_asks_for_exists_in_both_themes() -> None:
    """A token renamed in one file and not the other would silently fall back."""
    asked = set(re.findall(r'token\("(--[a-z-]+)"', APP_JS))
    assert asked == {"--chart-closed", "--chart-runs", "--chart-ink", "--chart-grid"}, asked
    for name in asked:
        assert name in LIGHT and name in OS_DARK, name
    # Chart.js paints on a canvas, which CSS cannot reach, so the values are read at runtime
    assert "getComputedStyle(document.documentElement)" in APP_JS


def test_every_axis_line_the_charts_draw_is_themed() -> None:
    """Chart.js paints anything left unset from its own light-theme defaults.

    `border` is a separate option from `grid` and does not inherit from it: unset, it
    routes to Chart.defaults.borderColor, "rgba(0,0,0,0.1)", which is invisible on the
    dark panel. Both scales must set ticks, grid and border, on creation and on update.
    """
    for scale in ("x", "y"):
        for part in ("grid", "border"):
            assert f"scales.{scale}.{part}.color = colors.grid" in APP_JS, (scale, part)
        assert f"scales.{scale}.ticks.color = colors.ink" in APP_JS, scale
    # the creation path states the same three, once per scale
    options = APP_JS[APP_JS.index("scales: {") :]
    options = options[: options.index("\n      }")]
    assert options.count("border: { color: colors.grid }") == 2, options
    assert options.count("grid: { color: colors.grid }") == 2, options
    assert options.count("color: colors.ink") == 2, options


def test_the_two_scripts_agree_on_the_repaint_event() -> None:
    """theme.js fires it and app.js listens; a rename in one file only would go unnoticed."""
    fired = set(re.findall(r'CustomEvent\("([a-z:]+)"', THEME_JS))
    heard = set(re.findall(r'addEventListener\("(issuebot:[a-z]+)"', APP_JS))
    assert fired == heard == {"issuebot:themechange"}, (fired, heard)


def test_the_theme_introduces_no_inline_code(h: Harness) -> None:
    h.seed_issue()
    for response in (h.client.get("/"), h.client.get("/issues/7"), h.client.get("/nothing")):
        text = html(response)
        assert ' style="' not in text, response.url
        assert "<style" not in text, response.url
        assert "onclick=" not in text, response.url
