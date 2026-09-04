"""Numbered SQL migrations applied in one transaction under an advisory lock."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

import psycopg
from psycopg import AsyncConnection

from issuebot.db.connection import Connector, connect, error_text, redact
from issuebot.db.errors import MigrationError

MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
ADVISORY_LOCK_KEY = 0x49535355  # "ISSU": the same key in every process that migrates
MIGRATIONS_ROOT = files("issuebot.db") / "migrations"
SCHEMA_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    integer PRIMARY KEY,
    name       text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)"""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied: tuple[str, ...]
    version: int


def discover_migrations(root: Traversable | Path = MIGRATIONS_ROOT) -> tuple[Migration, ...]:
    """Every ``NNNN_name.sql`` under ``root``, sorted; versions must run 1, 2, 3 ... with no gap."""
    found: dict[int, Migration] = {}
    for entry in root.iterdir():
        match = MIGRATION_NAME.match(entry.name)
        if match is None:
            raise MigrationError(f"unexpected file in migrations: {entry.name}")
        version = int(match.group(1))
        if version in found:
            raise MigrationError(f"duplicate migration version {version:04d}")
        found[version] = Migration(
            version=version, name=match.group(2), sql=entry.read_text(encoding="utf-8")
        )
    ordered = tuple(found[version] for version in sorted(found))
    for expected, migration in enumerate(ordered, start=1):
        if migration.version != expected:
            raise MigrationError(
                "migration versions must run 1, 2, 3 ... without gaps; "
                f"found {migration.label} where {expected:04d} was expected"
            )
    return ordered


async def schema_version(conn: AsyncConnection) -> int:
    """``max(version)`` in ``schema_migrations``; 0 when the table does not exist."""
    exists = await (await conn.execute("SELECT to_regclass('schema_migrations')")).fetchone()
    if exists is None or exists[0] is None:
        return 0
    row = await (
        await conn.execute("SELECT coalesce(max(version), 0) FROM schema_migrations")
    ).fetchone()
    return int(row[0]) if row is not None else 0


async def apply_migrations(
    conn: AsyncConnection, migrations: Sequence[Migration] | None = None
) -> MigrationResult:
    """Apply every pending migration in one transaction under an advisory lock."""
    known = tuple(migrations) if migrations is not None else discover_migrations()
    latest = known[-1].version if known else 0
    applied: list[str] = []
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
        await conn.execute(SCHEMA_TABLE_SQL)
        cursor = await conn.execute("SELECT version, name FROM schema_migrations ORDER BY version")
        recorded = {int(version): str(name) for version, name in await cursor.fetchall()}
        newest = max(recorded, default=0)
        if newest > latest:
            raise MigrationError(
                f"schema version {newest} is newer than this issuebot knows ({latest})"
            )
        for migration in known:
            name = recorded.get(migration.version)
            if name is not None:
                if name != migration.name:
                    raise MigrationError(
                        f"migration {migration.version:04d} is recorded as {name!r}, "
                        f"not {migration.name!r}"
                    )
                continue
            await conn.execute(migration.sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (migration.version, migration.name),
            )
            applied.append(migration.label)
    return MigrationResult(applied=tuple(applied), version=latest)


async def migrate(url: str, *, connect: Connector = connect) -> MigrationResult:
    """Connect, apply the pending migrations, close; failures are MigrationError, redacted."""
    try:
        conn = await connect(url)
    except psycopg.Error as exc:
        raise MigrationError(redact(f"cannot connect: {error_text(exc)}", url)) from exc
    try:
        return await apply_migrations(conn)
    except psycopg.Error as exc:
        raise MigrationError(redact(f"{type(exc).__name__}: {error_text(exc)}", url)) from exc
    finally:
        await conn.close()
