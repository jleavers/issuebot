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

TABLES = {"issues", "runs", "events", "runtime_snapshot", "schema_migrations"}


# --- discovery (no database) -------------------------------------------------------------


def test_the_package_ships_the_initial_migration() -> None:
    migrations = discover_migrations()
    assert [m.label for m in migrations] == ["0001_initial"]
    assert migrations[0].version == 1
    assert "CREATE TABLE issues" in migrations[0].sql
    assert "CREATE TABLE runtime_snapshot" in migrations[0].sql


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


async def test_migrate_applies_the_initial_migration_once(db_url: str) -> None:
    first = await migrate(db_url)
    assert (first.applied, first.version) == (("0001_initial",), 1)
    second = await migrate(db_url)
    assert (second.applied, second.version) == ((), 1)
    conn = await connect(db_url)
    try:
        assert await _tables(conn) == TABLES
        assert await schema_version(conn) == 1
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
        with pytest.raises(MigrationError, match=r"schema version 7 is newer .* knows \(1\)"):
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
