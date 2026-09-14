"""Tests for migration discovery (hermetic) and application (needs DATABASE_URL)."""

from pathlib import Path

import psycopg
import pytest

from issuebot.db import (
    Migration,
    MigrationError,
    apply_migrations,
    connect,
    discover_migrations,
    migrate,
    schema_version,
)

TABLES = {
    "issues",
    "runs",
    "events",
    "runtime_snapshot",
    "run_turns",
    "repos",
    "schema_migrations",
}


# --- discovery (no database) -------------------------------------------------------------


def test_the_package_ships_the_four_migrations() -> None:
    migrations = discover_migrations()
    assert [m.label for m in migrations] == [
        "0001_initial",
        "0002_run_turns",
        "0003_repos",
        "0004_run_turns_repo",
    ]
    assert [m.version for m in migrations] == [1, 2, 3, 4]
    assert "CREATE TABLE issues" in migrations[0].sql
    assert "CREATE TABLE runtime_snapshot" in migrations[0].sql
    assert "CREATE TABLE run_turns" in migrations[1].sql
    assert "CREATE TABLE repos" in migrations[2].sql


def test_discovery_sorts_by_version_and_reads_the_sql(tmp_path: Path) -> None:
    (tmp_path / "0002_second.sql").write_text("SELECT 2", encoding="utf-8")
    (tmp_path / "0001_first.sql").write_text("SELECT 1", encoding="utf-8")
    found = discover_migrations(tmp_path)
    assert [(m.version, m.name, m.sql) for m in found] == [
        (1, "first", "SELECT 1"),
        (2, "second", "SELECT 2"),
    ]


def test_discovery_rejects_a_gap(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "0003_third.sql").write_text("SELECT 3", encoding="utf-8")
    with pytest.raises(MigrationError, match="without gaps; found 0003_third where 0002"):
        discover_migrations(tmp_path)


def test_discovery_rejects_a_duplicate_version(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "0001_again.sql").write_text("SELECT 1", encoding="utf-8")
    with pytest.raises(MigrationError, match="duplicate migration version 0001"):
        discover_migrations(tmp_path)


def test_discovery_rejects_a_stray_file(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(MigrationError, match=r"unexpected file in migrations: notes\.txt"):
        discover_migrations(tmp_path)


def test_an_empty_directory_has_no_migrations(tmp_path: Path) -> None:
    assert discover_migrations(tmp_path) == ()


# --- application (database) --------------------------------------------------------------


async def _tables(conn: psycopg.AsyncConnection) -> set[str]:
    cursor = await conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    )
    return {row[0] for row in await cursor.fetchall()}


async def test_migrate_applies_every_migration_once(db_url: str) -> None:
    first = await migrate(db_url)
    assert (first.applied, first.version) == (
        ("0001_initial", "0002_run_turns", "0003_repos", "0004_run_turns_repo"),
        4,
    )
    second = await migrate(db_url)
    assert (second.applied, second.version) == ((), 4)
    conn = await connect(db_url)
    try:
        assert await _tables(conn) == TABLES
        assert await schema_version(conn) == 4
    finally:
        await conn.close()


async def test_schema_version_is_zero_before_any_migration(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        assert await schema_version(conn) == 0
        assert await _tables(conn) == set()
    finally:
        await conn.close()


async def test_a_newer_recorded_version_is_refused(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await apply_migrations(conn)
        await conn.execute("INSERT INTO schema_migrations (version, name) VALUES (7, 'future')")
        with pytest.raises(MigrationError, match=r"schema version 7 is newer .* knows \(4\)"):
            await apply_migrations(conn)
    finally:
        await conn.close()


async def test_a_renamed_recorded_migration_is_refused(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await apply_migrations(conn)
        await conn.execute("UPDATE schema_migrations SET name = 'other' WHERE version = 1")
        with pytest.raises(MigrationError, match="recorded as 'other', not 'initial'"):
            await apply_migrations(conn)
    finally:
        await conn.close()


async def test_a_failing_migration_rolls_back_the_whole_run(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        good = Migration(version=1, name="table", sql="CREATE TABLE t (id int)")
        bad = Migration(version=2, name="broken", sql="CREATE TABLE t (id int)")
        with pytest.raises(psycopg.Error):
            await apply_migrations(conn, [good, bad])
        assert await _tables(conn) == set()
        assert await schema_version(conn) == 0
    finally:
        await conn.close()


async def test_migrate_reports_an_unreachable_server_without_the_url() -> None:
    url = "postgresql://issuebot:s3cret@127.0.0.1:1/issuebot"
    with pytest.raises(MigrationError, match="cannot connect") as exc:
        await migrate(url)
    assert "s3cret" not in exc.value.message


async def _at_version_two(db_url: str) -> psycopg.AsyncConnection:
    """A connection to a schema migrated to version 2 only (the pre-hub shape)."""
    conn = await connect(db_url)
    await apply_migrations(conn, discover_migrations()[:2])
    return conn


async def _at_version_three(db_url: str) -> psycopg.AsyncConnection:
    """A connection to a schema migrated to version 3 only (run_turns without a repo)."""
    conn = await connect(db_url)
    await apply_migrations(conn, discover_migrations()[:3])
    return conn


RUN_V3 = """
INSERT INTO runs (repo, run_id, issue_number, issue_identifier, attempt, started_at)
VALUES (%s, %s, 1, 'x-1', 1, now())
"""

TURN_V3 = """
INSERT INTO run_turns (run_id, turn_number, captured_at, prompt, prompt_bytes, stream,
                       stream_bytes, stream_lines, omitted_lines, stderr, stderr_bytes, truncated)
VALUES (%s, 1, now(), 'p', 1, '', 0, 0, 0, '', 0, false)
"""

CONSTRAINTS = """
SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
WHERE conrelid = %s::regclass AND contype IN ('p', 'f') ORDER BY conname
"""


async def test_0004_gives_every_turn_its_run_s_repository_and_keys_both_by_it(
    db_url: str,
) -> None:
    conn = await _at_version_three(db_url)
    try:
        await conn.execute(RUN_V3, ("alpha/one", "run-a"))
        await conn.execute(RUN_V3, ("beta/two", "run-b"))
        await conn.execute(TURN_V3, ("run-a",))
        await conn.execute(TURN_V3, ("run-b",))
    finally:
        await conn.close()
    result = await migrate(db_url)
    assert result.version == 4 and result.applied == ("0004_run_turns_repo",)
    conn = await connect(db_url)
    try:
        turns = await (
            await conn.execute("SELECT run_id, repo FROM run_turns ORDER BY run_id")
        ).fetchall()
        assert turns == [("run-a", "alpha/one"), ("run-b", "beta/two")]
        runs = dict(await (await conn.execute(CONSTRAINTS, ("runs",))).fetchall())
        assert runs == {"runs_pkey": "PRIMARY KEY (repo, run_id)"}
        turns_c = dict(await (await conn.execute(CONSTRAINTS, ("run_turns",))).fetchall())
        assert turns_c == {
            "run_turns_pkey": "PRIMARY KEY (repo, run_id, turn_number)",
            "run_turns_repo_run_id_fkey": (
                "FOREIGN KEY (repo, run_id) REFERENCES runs(repo, run_id) ON DELETE CASCADE"
            ),
        }
        # The write is where the check lives now: a turn against another repository's run
        # has no run to reference, however its run_id reads.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await conn.execute(
                "INSERT INTO run_turns (repo, run_id, turn_number, captured_at, prompt, "
                "prompt_bytes, stream, stream_bytes, stream_lines, omitted_lines, stderr, "
                "stderr_bytes, truncated) VALUES ('beta/two', 'run-a', 2, now(), 'p', 1, '', "
                "0, 0, 0, '', 0, false)"
            )
    finally:
        await conn.close()


ISSUE_V2 = """
INSERT INTO issues (number, identifier, title, github_state, url, created_at, updated_at, seen_at)
VALUES (7, 'repo-7', 'Old', 'open', 'https://github.com/x/y/issues/7', now(), now(), now())
"""


async def test_0003_refuses_a_database_that_holds_rows(db_url: str) -> None:
    conn = await _at_version_two(db_url)
    try:
        await conn.execute(ISSUE_V2)
    finally:
        await conn.close()
    with pytest.raises(MigrationError, match="copy these in with the import command"):
        await migrate(db_url)
    conn = await connect(db_url)
    try:
        assert await schema_version(conn) == 2  # the transaction rolled back
    finally:
        await conn.close()


async def test_0003_ignores_a_stored_snapshot(db_url: str) -> None:
    conn = await _at_version_two(db_url)
    try:
        await conn.execute(
            "INSERT INTO runtime_snapshot (id, at, written_at, data) "
            "VALUES (true, now(), now(), '{}')"
        )
    finally:
        await conn.close()
    result = await migrate(db_url)
    assert result.version == 4 and result.applied == ("0003_repos", "0004_run_turns_repo")
    conn = await connect(db_url)
    try:
        columns = await (
            await conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'runtime_snapshot' "
                "ORDER BY ordinal_position"
            )
        ).fetchall()
        assert [c[0] for c in columns] == ["repo", "at", "written_at", "data"]
        assert (await (await conn.execute("SELECT count(*) FROM runtime_snapshot")).fetchone())[
            0
        ] == 0
    finally:
        await conn.close()
