"""The actions_minutes table against a real PostgreSQL (skipped without DATABASE_URL)."""

from datetime import UTC, date, datetime, timedelta

from issuebot.db import ActionsMinutesRow, Database

OCT = date(2026, 10, 1)
NOV = date(2026, 11, 1)
AT = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)


async def _database(db_url: str) -> Database:
    database = Database(db_url)
    await database.migrate()
    return database


async def _row(database: Database, repo: str = "jleavers/issuebot") -> ActionsMinutesRow | None:
    async with database.queries() as queries:
        return await queries.scoped(repo).actions_minutes()


async def _read(database: Database, period: date = OCT, used: float = 2306.0) -> int | None:
    return await database.record_actions_reading(
        account="jleavers",
        period=period,
        used_minutes=used,
        included_minutes=3000,
        observed_at=AT,
    )


async def test_a_reading_is_stored_and_read_back_by_the_owner_s_repositories(db_url: str) -> None:
    database = await _database(db_url)
    assert await _read(database) == 0
    assert await _row(database) == ActionsMinutesRow(
        account="jleavers",
        period=OCT,
        used_minutes=2306.0,
        included_minutes=3000,
        observed_at=AT,
        alerted_percent=0,
        error=None,
        error_at=None,
    )


async def test_the_owner_is_matched_case_insensitively_and_nobody_else_s(db_url: str) -> None:
    database = await _database(db_url)
    await _read(database)
    assert await _row(database, "JLeavers/issuebot") is not None
    assert await _row(database, "acme/frontend") is None


async def test_an_error_keeps_the_reading_and_the_next_reading_clears_it(db_url: str) -> None:
    database = await _database(db_url)
    await _read(database)
    later = AT + timedelta(hours=1)
    await database.record_actions_error(account="jleavers", error="token rejected", error_at=later)
    row = await _row(database)
    assert row is not None
    assert (row.used_minutes, row.error, row.error_at) == (2306.0, "token rejected", later)
    await _read(database, used=2400.0)
    row = await _row(database)
    assert row is not None and (row.used_minutes, row.error, row.error_at) == (2400.0, None, None)


async def test_an_error_before_any_reading_makes_a_reading_less_row(db_url: str) -> None:
    database = await _database(db_url)
    await database.record_actions_error(account="jleavers", error="token rejected", error_at=AT)
    row = await _row(database)
    assert row is not None
    assert (row.period, row.used_minutes, row.error) == (None, None, "token rejected")


async def test_a_claim_is_taken_once_and_released_only_by_its_holder(db_url: str) -> None:
    database = await _database(db_url)
    await _read(database)
    assert await database.claim_actions_alert(account="jleavers", period=OCT, target=75) is True
    assert await database.claim_actions_alert(account="jleavers", period=OCT, target=75) is False
    await database.release_actions_alert(account="jleavers", period=OCT, target=90, previous=0)
    row = await _row(database)
    assert row is not None and row.alerted_percent == 75
    await database.release_actions_alert(account="jleavers", period=OCT, target=75, previous=0)
    row = await _row(database)
    assert row is not None and row.alerted_percent == 0


async def test_a_claim_for_another_period_takes_nothing(db_url: str) -> None:
    database = await _database(db_url)
    await _read(database)
    assert await database.claim_actions_alert(account="jleavers", period=NOV, target=75) is False


async def test_the_same_month_keeps_the_alert_memory(db_url: str) -> None:
    database = await _database(db_url)
    await _read(database)
    await database.claim_actions_alert(account="jleavers", period=OCT, target=90)
    assert await _read(database, used=2800.0) == 90


async def test_a_new_month_resets_the_alert_memory(db_url: str) -> None:
    database = await _database(db_url)
    await _read(database)
    await database.claim_actions_alert(account="jleavers", period=OCT, target=90)
    assert await _read(database, period=NOV, used=10.0) == 0
    row = await _row(database)
    assert row is not None and (row.period, row.alerted_percent) == (NOV, 0)


async def test_a_reading_for_an_older_month_is_ignored(db_url: str) -> None:
    """GitHub lagging across a month boundary must not undo the new month or its alerts."""
    database = await _database(db_url)
    await _read(database, period=NOV, used=10.0)
    await database.claim_actions_alert(account="jleavers", period=NOV, target=75)
    assert await _read(database, period=OCT, used=3012.0) is None
    row = await _row(database)
    assert row is not None and (row.period, row.used_minutes, row.alerted_percent) == (
        NOV,
        10.0,
        75,
    )
