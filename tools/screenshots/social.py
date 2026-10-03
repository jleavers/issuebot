"""Render the repository's social preview from the committed dashboard image.

``social-preview.png``   1280x640, what GitHub asks for under Settings -> General -> Social
                         preview: the name, the tagline and the label flow beside a window
                         cut from ``dashboard.png``.

``capture.py`` runs this after it writes ``dashboard.png``, so the card never shows a board
that has since been redrawn. It also runs on its own, without a store or a dashboard, for a
change to the card alone. GitHub does not read the file from the tree: after it changes,
someone with admin on the repository uploads it again.

The words are Inter and JetBrains Mono from Google Fonts, so this needs the network, and it
refuses to write the file if either did not load rather than leave a fallback face in it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
IMAGES = HERE.parent.parent / "docs" / "images"
SIZE = {"width": 1280, "height": 640}
FACES = ("Inter", "JetBrains Mono")

LOADED_FACES = """async () => {
  await document.fonts.ready;
  return [...document.fonts]
    .filter((face) => face.status === "loaded")
    .map((face) => face.family.replace(/^["']|["']$/g, ""));
}"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--theme",
        choices=("dark", "light"),
        default="dark",
        help="the dashboard theme the card takes its colours from; dark is what is committed",
    )
    args = parser.parse_args()

    out = IMAGES / "social-preview.png"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport=SIZE, device_scale_factor=1)
        page.goto((HERE / "social.html").as_uri(), wait_until="networkidle")
        page.evaluate(f"document.documentElement.dataset.theme = {args.theme!r}")
        missing = set(FACES) - set(page.evaluate(LOADED_FACES))
        if missing:
            print(f"fonts did not load: {', '.join(sorted(missing))}", file=sys.stderr)
            browser.close()
            return 1
        page.screenshot(path=out, clip={"x": 0, "y": 0, **SIZE})
        browser.close()

    kib = out.stat().st_size / 1024
    print(f"{out.name}: {kib:.0f} KB")
    if kib > 500:
        print(f"over the 500 KB hook limit: {out.name}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
