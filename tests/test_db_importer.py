"""The one-off import of a version-2 database into the hub (needs DATABASE_URL)."""

from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from issuebot.config import GitHubLabels
from issuebot.db import Database, apply_migrations, connect, discover_migrations, migrate
from issuebot.db.errors import ImportRefused
from issuebot.db.importer import import_repo

REPO = "example/repo"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
RUN_ID = "20260904T120000Z-abc123"


async def seed_version_two(url: str) -> None:
    """A source at schema version 2 with one row in every table."""
    conn = await connect(url)
    try:
        await apply_migrations(conn, discover_migrations()[:2])
        await conn.execute(
            "INSERT INTO issues (number, identifier, title, state, state_label, github_state, url,"
            " labels, created_at, updated_at, closed_at, seen_at) VALUES (7, 'repo-7', 'Old', "
            "'complete', 'issuebot/complete', 'closed', 'https://github.com/x/y/issues/7', "
            "%s, %s, %s, %s, %s)",
            (["issuebot/complete"], NOW, NOW, NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO runs (run_id, issue_number, issue_identifier, attempt, started_at,"
            " ended_at, outcome, turns, input_tokens, output_tokens, cost_usd, duration_s,"
            " log_dir) VALUES "
            "(%s, 7, 'repo-7', 1, %s, %s, 'succeeded', 1, 100, 10, 0.5, 60.0, '/w/runs/x')",
            (RUN_ID, NOW - timedelta(minutes=1), NOW),
        )
        await conn.execute(
            "INSERT INTO run_turns (run_id, turn_number, captured_at, model, prompt, prompt_bytes,"
            " stream, stream_bytes, stream_lines, omitted_lines, stderr, stderr_bytes, truncated)"
            " VALUES (%s, 1, %s, 'opus', 'p', 1, 's', 1, 1, 0, '', 0, false)",
            (RUN_ID, NOW),
        )
        await conn.execute(
            "INSERT INTO events (at, kind, issue_number, run_id, payload) VALUES (%s, 'run_ended',"
            " 7, %s, %s)",
            (NOW, RUN_ID, Jsonb({"kind": "run_ended", "run_id": RUN_ID})),
        )
        await conn.execute(
            "INSERT INTO runtime_snapshot (id, at, written_at, data) VALUES (true, %s, %s, %s)",
            (NOW, NOW, Jsonb({"tick_count": 3})),
        )
    finally:
        await conn.close()


async def count(url: str, table: str, repo: str | None = REPO) -> int:
    conn = await connect(url)
    try:
        where = "" if repo is None else f" WHERE repo = '{repo}'"
        row = await (await conn.execute(f"SELECT count(*) FROM {table}{where}")).fetchone()
        return int(row[0]) if row else 0
    finally:
        await conn.close()


async def test_import_copies_every_table_stamped_with_the_repository(
    db_url: str, source_db_url: str
) -> None:
    await seed_version_two(source_db_url)
    result = await import_repo(
        source_db_url, db_url, repo=REPO, labels=GitHubLabels(), workflow_path="/configs/W.md"
    )
    assert result.counts == {
        "issues": 1,
        "runs": 1,
        "run_turns": 1,
        "events": 1,
        "runtime_snapshot": 1,
    }
    for table in ("issues", "runs", "events", "runtime_snapshot"):
        assert await count(db_url, table) == 1
    assert await count(db_url, "run_turns", repo=None) == 1
    database = Database(db_url)
    async with database.queries() as queries:
        (row,) = await queries.repos()
        assert (row.repo, row.workflow_path) == (REPO, "/configs/W.md")
        scoped = queries.scoped(REPO)
        turn = await scoped.turn(RUN_ID, 1)
        assert turn is not None and turn.model == "opus"
        (event,) = await scoped.recent_events(10)
        assert event.payload["run_id"] == RUN_ID
        assert (await scoped.snapshot()).data == {"tick_count": 3}  # type: ignore[union-attr]
        assert await scoped.closed_count(timedelta(days=3650)) == 1


async def test_import_refuses_a_repository_that_is_already_registered(
    db_url: str, source_db_url: str
) -> None:
    await seed_version_two(source_db_url)
    await import_repo(source_db_url, db_url, repo=REPO, labels=GitHubLabels(), workflow_path=None)
    with pytest.raises(ImportRefused, match="example/repo is already registered in the target"):
        await import_repo(
            source_db_url, db_url, repo=REPO, labels=GitHubLabels(), workflow_path=None
        )
    assert await count(db_url, "events") == 1  # nothing doubled


async def test_import_refuses_a_source_at_the_wrong_version(
    db_url: str, source_db_url: str
) -> None:
    await migrate(source_db_url)  # version 3: not what the import reads
    with pytest.raises(ImportRefused, match="source is at schema version 3, expected 2"):
        await import_repo(
            source_db_url, db_url, repo=REPO, labels=GitHubLabels(), workflow_path=None
        )


async def test_import_migrates_an_empty_target_first(db_url: str, source_db_url: str) -> None:
    await seed_version_two(source_db_url)
    conn = await connect(source_db_url)
    try:
        await conn.execute("DELETE FROM runtime_snapshot")
    finally:
        await conn.close()
    result = await import_repo(
        source_db_url, db_url, repo=REPO, labels=GitHubLabels(), workflow_path=None
    )
    assert result.counts["runtime_snapshot"] == 0
    assert await count(db_url, "issues") == 1
