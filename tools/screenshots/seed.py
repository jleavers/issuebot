"""Fabricated data for the README's screenshots.

Nothing here is real. The repository is ``acme/frontend``, the placeholder the README and
``docs/toolchains.md`` already use, and every issue, run, cost and token figure is invented --
which is the point: the images in a public README must not carry anyone's private repository
names or issue titles.

Two things are seeded, and they are separate because they run a different number of times:

``history``  thirty days of closed issues and finished runs, so the dashboard's two charts and
             its 1-day and 7-day tiles carry figures rather than zeroes. Run once; running it
             again would multiply every count behind the tiles.
``stage N``  the board with issue #42 in the Nth column of its journey (1 todo, 2 in-progress,
             3 review, 4 complete). Run once per frame of the journey animation; it upserts by
             issue number, so a later stage replaces the earlier one rather than adding to it.

``DATABASE_URL`` must point at a throwaway. See ``README.md`` beside this file.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from issuebot.config.settings import GitHubLabels
from issuebot.db.database import Database
from issuebot.db.store import IssueSnapshot
from issuebot.events.types import RunEnded, RunStarted
from issuebot.github import Issue, LinkedPr, StateLabel
from issuebot.orchestrator.state import (
    ClaudeTotals,
    Counters,
    RunningRow,
    RuntimeSnapshot,
)

REPO = "acme/frontend"
LABELS = GitHubLabels()
NOW = datetime.now(UTC)

# Fixed, so that regenerating the images does not reshuffle the history behind them.
rng = random.Random(20260922)

ARCHIVE_TITLES = (
    "Virtualise the long order list",
    "Collapse duplicate network retries",
    "Trim the icon sprite to what the app uses",
    "Guard the clipboard call behind a feature test",
    "Preserve scroll position across a route change",
    "Stop the modal trapping focus after close",
    "Cache the currency table for a session",
    "Redraw the sparkline on container resize",
    "Escape the filename in the download header",
    "Announce the toast to screen readers",
    "Debounce the resize observer",
    "Fix the off-by-one in the pagination label",
    "Use a stable key for the reordered rows",
    "Reset the form's dirty flag after a save",
)

JOURNEY_TITLE = "Focus ring is invisible on the dark theme's primary button"


def _issue(
    number: int,
    title: str,
    role: str | None,
    *,
    closed: bool = False,
    pr: int | None = None,
    pr_state: str = "open",
    merged: bool = False,
    age_h: float = 6.0,
    markers: tuple[str, ...] = (),
) -> Issue:
    label = getattr(LABELS, role) if role else None
    at = NOW - timedelta(hours=age_h)
    linked = (
        LinkedPr(
            number=pr,
            url=f"https://github.com/{REPO}/pull/{pr}",
            state=pr_state,
            merged_at=at if merged else None,
            mergeable="mergeable",
        )
        if pr
        else None
    )
    return Issue(
        id=f"I_{number}",
        identifier=f"{REPO}#{number}",
        number=number,
        title=title,
        body=None,
        author="dana",
        github_state="closed" if closed else "open",
        state=StateLabel(role) if role else None,
        state_labels=(label,) if label else (),
        labels=((label,) if label else ()) + markers,
        url=f"https://github.com/{REPO}/issues/{number}",
        assignees=(),
        created_at=at - timedelta(days=1),
        updated_at=NOW - timedelta(minutes=12),
        closed_at=at if closed else None,
        linked_pr=linked,
        dispatchable=True,
    )


# The standing board: every column but the one issue #42 moves through.
BOARD = (
    _issue(57, "Debounce the search box so typing does not fire a request a keystroke", "todo"),
    _issue(56, "Add a skip-to-content link for keyboard users", "todo"),
    _issue(55, "Locale-aware date formatting in the activity feed", "todo"),
    _issue(51, "Cart total drops the currency symbol on the summary step", "in_progress"),
    _issue(49, "Retry the avatar upload once on a 502", "review", pr=118),
    _issue(
        47,
        "Tooltip stays open after the trigger unmounts",
        "review",
        pr=116,
        markers=(LABELS.no_fault,),
    ),
    _issue(
        44,
        "Bundle splits the vendor chunk twice",
        "complete",
        closed=True,
        pr=113,
        pr_state="merged",
        merged=True,
        age_h=0.2,
    ),
    _issue(
        41,
        "Empty state flashes before the first fetch resolves",
        "complete",
        closed=True,
        pr=110,
        pr_state="merged",
        merged=True,
        age_h=0.3,
    ),
    _issue(
        38,
        "Sticky header overlaps the anchor it scrolls to",
        "complete",
        closed=True,
        pr=107,
        pr_state="merged",
        merged=True,
        age_h=0.4,
    ),
)

STAGES = {
    1: _issue(42, JOURNEY_TITLE, "todo", age_h=4),
    2: _issue(42, JOURNEY_TITLE, "in_progress", age_h=4),
    3: _issue(42, JOURNEY_TITLE, "review", pr=121, age_h=4),
    4: _issue(
        42,
        JOURNEY_TITLE,
        "complete",
        closed=True,
        pr=121,
        pr_state="merged",
        merged=True,
        age_h=0.1,
    ),
}


def _snapshot(running: int) -> RuntimeSnapshot:
    """A worker mid-tick: what the dashboard's worker line and hero tiles read."""
    from issuebot.agent.runner import RateLimits, RateLimitWindow

    candidates = (
        (51, 7, 2, "Cart total drops the currency symbol on the summary step"),
        (42, 3, 1, JOURNEY_TITLE),
    )
    rows = tuple(
        RunningRow(
            issue_number=number,
            identifier=f"{REPO}#{number}",
            title=title,
            url=f"https://github.com/{REPO}/issues/{number}",
            state="in_progress",
            attempt=1,
            rework=False,
            resumed=False,
            run_id=f"run-{number}",
            session_id=f"sess-{number}",
            started_at=NOW - timedelta(minutes=minutes),
            last_activity_at=NOW - timedelta(seconds=20),
            last_event="assistant",
            turns=turns,
            stop_cause=None,
        )
        for number, minutes, turns, title in candidates[:running]
    )
    return RuntimeSnapshot(
        at=NOW,
        workflow_path="/configs/WORKFLOW.md",
        workflow_mtime_ns=0,
        workflow_overlay_path="/configs/WORKFLOW.local.md",
        config_valid=True,
        config_error=None,
        dispatch_hold=None,
        poll_interval_ms=30000,
        max_concurrent_agents=3,
        tick_count=1284,
        last_tick_at=NOW,
        running=rows,
        retrying=(),
        totals=ClaudeTotals(
            input_tokens=2_940_000,
            output_tokens=511_000,
            cost_usd=41.87,
            seconds_running=18_400.0,
        ),
        counters=Counters(),
        credential="subscription",
        rate_limits=RateLimits(
            five_hour=RateLimitWindow(
                utilization=0.38, resets_at=NOW + timedelta(hours=2, minutes=40)
            ),
            seven_day=RateLimitWindow(utilization=0.61, resets_at=NOW + timedelta(days=3, hours=5)),
            observed_at=NOW - timedelta(minutes=3),
        ),
    )


async def _run(
    store, number: int, at: datetime, *, cost: float, tokens: tuple[int, int], turns: int
) -> None:
    common = {
        "issue_number": number,
        "issue_identifier": f"{REPO}#{number}",
        "run_id": f"run-{number}-{at:%m%d%H%M}",
    }
    await store.apply_event(
        RunStarted(
            at=at,
            attempt=1,
            session_id=f"sess-{number}",
            workspace_path=f"/workspaces/acme_frontend-{number}",
            **common,
        )
    )
    await store.apply_event(
        RunEnded(
            at=at + timedelta(minutes=14),
            outcome="succeeded",
            error=None,
            turns=turns,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cost_usd=cost,
            duration_s=840.0,
            **common,
        )
    )


def _titles() -> Iterator[str]:
    """Every title before any repeats, so no two cards on the board read the same.

    ``rng.choice`` would draw with replacement, and the board shows the five most recently
    closed: two of them landing on the same title is the kind of detail a reader notices in a
    screenshot and reads as a bug.
    """
    while True:
        shuffled = list(ARCHIVE_TITLES)
        rng.shuffle(shuffled)
        yield from shuffled


async def seed_history(store) -> int:
    """Thirty days of closed issues and finished runs, today included."""
    archive: list[Issue] = []
    titles = _titles()
    number = 200
    for day in range(30, -1, -1):
        # A working rhythm rather than a flat line: quiet days, and a couple of busy ones.
        closed = rng.choice((0, 0, 1, 1, 1, 2, 2, 3))
        for _ in range(closed):
            at = NOW - timedelta(days=day, hours=rng.uniform(1, 20))
            archive.append(
                _issue(
                    number,
                    next(titles),
                    "complete",
                    closed=True,
                    pr=number + 60,
                    pr_state="merged",
                    merged=True,
                    age_h=(NOW - at).total_seconds() / 3600,
                )
            )
            number += 1
        for _ in range(closed + rng.choice((0, 0, 1))):
            await _run(
                store,
                number,
                NOW - timedelta(days=day, hours=rng.uniform(1, 22)),
                cost=round(rng.uniform(0.6, 3.4), 2),
                tokens=(rng.randrange(40_000, 130_000), rng.randrange(6_000, 21_000)),
                turns=rng.randint(2, 5),
            )

    # The runs behind the board's own issues, so the 1-day tiles are not zero while the
    # dashboard is also showing two agents running.
    for issue_number, hours, cost, tokens, turns in (
        (44, 5, 1.94, (84_000, 12_400), 3),
        (41, 20, 2.61, (96_500, 15_900), 4),
        (38, 16, 1.12, (51_300, 8_050), 2),
        (49, 2, 3.05, (120_400, 19_100), 4),
        (47, 8, 0.74, (33_900, 5_240), 2),
    ):
        await _run(
            store, issue_number, NOW - timedelta(hours=hours), cost=cost, tokens=tokens, turns=turns
        )

    await store.upsert_issues([IssueSnapshot(issue=i, seen_at=NOW) for i in archive])
    return len(archive)


async def main(command: str) -> None:
    database = Database(os.environ["DATABASE_URL"])
    await database.migrate()
    await database.register_repo(REPO, labels=LABELS, workflow_path="/configs/WORKFLOW.md")
    store = database.store(LABELS, REPO)
    await store.connect()
    try:
        if command == "history":
            print(f"history: {await seed_history(store)} closed issues over 30 days")
            return
        stage = int(command)
        board = [*BOARD, STAGES[stage]]
        await store.upsert_issues([IssueSnapshot(issue=i, seen_at=NOW) for i in board])
        snapshot = _snapshot(running=2 if stage == 2 else 1)
        await store.write_snapshot(snapshot.at, snapshot.to_dict())
        print(f"stage {stage}: issue #42 in {STAGES[stage].state}")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "history"))
