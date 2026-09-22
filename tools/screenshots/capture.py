"""Capture the README's two images from a dashboard serving fabricated data.

``dashboard.png``        the whole page, once.
``issue-journey.gif``    the Kanban board once per stage, captioned and assembled.

Playwright and Pillow are ephemeral here -- ``uv run --with playwright --with pillow`` -- so
neither joins the project's dependencies for the sake of a picture. Pillow rather than
ImageMagick or ffmpeg for the same reason: it is a wheel, so the host installs nothing.

Both files must stay under 500 KB, which is what ``check-added-large-files`` allows by
default; ``--width`` and ``--colours`` are the two dials if a redesign pushes the GIF over.
See ``README.md`` beside this file for the whole procedure.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
IMAGES = HERE.parent.parent / "docs" / "images"
FONTS = Path("/usr/share/fonts/truetype/dejavu")

CAPTIONS = (
    "1.  someone labels the issue  issuebot/todo",
    "2.  the worker claims it, clones, and runs  claude -p",
    "3.  it pushes a branch, opens a PR, and asks for review",
    "4.  you merge; the issue closes and lands in  issuebot/complete",
)
HOLD_MS = (2300, 2300, 2300, 3000)
CAPTION_BAR = 52
PAGE = (247, 248, 250)  # the dashboard's own page background
INK = (32, 38, 48)
MUTED = (104, 114, 130)
RULE = (226, 230, 236)


def _page(browser, url: str, password: str, width: int):
    page = browser.new_page(
        viewport={"width": width, "height": 900},
        http_credentials={"username": "screenshot", "password": password},
        color_scheme="light",
        device_scale_factor=1,
    )
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(1500)  # the charts draw after the stats request settles
    return page


def _caption(image: Image.Image, text: str) -> Image.Image:
    regular = ImageFont.truetype(str(FONTS / "DejaVuSans.ttf"), 21)
    bold = ImageFont.truetype(str(FONTS / "DejaVuSans-Bold.ttf"), 21)
    canvas = Image.new("RGB", (image.width, image.height + CAPTION_BAR), PAGE)
    canvas.paste(image, (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.line([(0, image.height + 6), (image.width, image.height + 6)], fill=RULE, width=1)
    number, _, rest = text.partition(" ")
    draw.text((14, image.height + 18), number, font=bold, fill=MUTED)
    draw.text(
        (14 + draw.textlength(number, font=bold), image.height + 18),
        rest,
        font=regular,
        fill=INK,
    )
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8099/r/acme/frontend/")
    parser.add_argument("--password", required=True, help="ISSUEBOT_WEB_PASSWORD of the server")
    parser.add_argument("--dsn", required=True, help="DATABASE_URL of the throwaway store")
    parser.add_argument("--width", type=int, default=900, help="GIF width in pixels")
    parser.add_argument("--colours", type=int, default=64, help="GIF palette size")
    args = parser.parse_args()

    IMAGES.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()

        # The hero, with issue #42 mid-flight so the board is not half empty.
        subprocess.run(
            [sys.executable, str(HERE / "seed.py"), "2"],
            check=True,
            env={"DATABASE_URL": args.dsn, "PATH": "/usr/bin:/bin"},
            capture_output=True,
        )
        page = _page(browser, args.url, args.password, 1280)
        page.screenshot(path=IMAGES / "dashboard.png", full_page=True)
        page.close()

        boards: list[Image.Image] = []
        for stage in (1, 2, 3, 4):
            subprocess.run(
                [sys.executable, str(HERE / "seed.py"), str(stage)],
                check=True,
                env={"DATABASE_URL": args.dsn, "PATH": "/usr/bin:/bin"},
                capture_output=True,
            )
            page = _page(browser, args.url, args.password, 1180)
            shot = HERE / f".frame-{stage}.png"
            page.locator("section.kanban").screenshot(path=shot)
            boards.append(Image.open(shot).convert("RGB"))
            page.close()
        browser.close()

    # One size for every frame: a column gains a card between stages, so the tallest wins.
    width = max(board.width for board in boards)
    height = max(board.height for board in boards)
    frames = []
    for board, caption in zip(boards, CAPTIONS, strict=True):
        canvas = Image.new("RGB", (width, height), PAGE)
        canvas.paste(board, (0, 0))
        canvas = _caption(canvas, caption)
        scale = args.width / width
        canvas = canvas.resize((args.width, round(canvas.height * scale)), Image.LANCZOS)
        frames.append(
            canvas.quantize(colors=args.colours, method=Image.MEDIANCUT, dither=Image.NONE)
        )

    gif = IMAGES / "issue-journey.gif"
    frames[0].save(
        gif,
        save_all=True,
        append_images=frames[1:],
        duration=list(HOLD_MS),
        loop=0,
        optimize=True,
        disposal=2,
    )
    for stage in (1, 2, 3, 4):
        (HERE / f".frame-{stage}.png").unlink(missing_ok=True)

    over = []
    for path in (IMAGES / "dashboard.png", gif):
        kib = path.stat().st_size / 1024
        print(f"{path.name}: {kib:.0f} KB")
        if kib > 500:
            over.append(path.name)
    if over:
        print(f"over the 500 KB hook limit: {', '.join(over)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
