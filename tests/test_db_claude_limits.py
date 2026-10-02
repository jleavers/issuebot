"""The claude_limit_alerts table against a real PostgreSQL (skipped without DATABASE_URL)."""

from datetime import UTC, datetime, timedelta

from issuebot.db import Database

RESETS = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)
WEEK = "seven_day"


async def _database(db_url: str) -> Database:
    database = Database(db_url)
    await database.migrate()
    return database


async def test_a_window_instance_nobody_has_alerted_reads_zero(db_url: str) -> None:
    database = await _database(db_url)
    assert await database.claude_limit_alerted(limit_window=WEEK, resets_at=RESETS) == 0


async def test_a_claim_inserts_then_raises_and_refuses_an_equal_or_lower_target(
    db_url: str,
) -> None:
    database = await _database(db_url)
    claim = database.claim_claude_limit_alert
    assert await claim(limit_window=WEEK, resets_at=RESETS, target=75) is True
    assert await claim(limit_window=WEEK, resets_at=RESETS, target=75) is False
    assert await claim(limit_window=WEEK, resets_at=RESETS, target=90) is True
    assert await claim(limit_window=WEEK, resets_at=RESETS, target=75) is False
    assert await database.claude_limit_alerted(limit_window=WEEK, resets_at=RESETS) == 90


async def test_a_window_instance_is_its_window_and_its_reset(db_url: str) -> None:
    """Review Focus 4's memory: next week, and the other window, start from nothing."""
    database = await _database(db_url)
    await database.claim_claude_limit_alert(limit_window=WEEK, resets_at=RESETS, target=90)
    next_week = RESETS + timedelta(days=7)
    assert await database.claude_limit_alerted(limit_window=WEEK, resets_at=next_week) == 0
    assert await database.claude_limit_alerted(limit_window="five_hour", resets_at=RESETS) == 0
    assert await database.claim_claude_limit_alert(
        limit_window="five_hour", resets_at=RESETS, target=100
    )


async def test_a_release_restores_only_its_own_claim(db_url: str) -> None:
    database = await _database(db_url)
    await database.claim_claude_limit_alert(limit_window=WEEK, resets_at=RESETS, target=75)
    await database.claim_claude_limit_alert(limit_window=WEEK, resets_at=RESETS, target=90)
    # A release for a target no longer held changes nothing...
    await database.release_claude_limit_alert(
        limit_window=WEEK, resets_at=RESETS, target=75, previous=0
    )
    assert await database.claude_limit_alerted(limit_window=WEEK, resets_at=RESETS) == 90
    # ...and its own puts back what was there before it.
    await database.release_claude_limit_alert(
        limit_window=WEEK, resets_at=RESETS, target=90, previous=75
    )
    assert await database.claude_limit_alerted(limit_window=WEEK, resets_at=RESETS) == 75
