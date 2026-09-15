"""The Database facade: everything the CLI does with the database, behind one object."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from issuebot.config import GitHubLabels
from issuebot.db.connection import (
    NOT_A_URL,
    Connector,
    classify,
    connect,
    describe,
    error_text,
    is_postgres_url,
    redact,
)
from issuebot.db.errors import DatabaseError, StoreUnavailableError
from issuebot.db.listen import REFRESH_CHANNEL, RefreshListener
from issuebot.db.migrate import MigrationResult, discover_migrations, migrate, schema_version
from issuebot.db.queries import Queries
from issuebot.db.store import REGISTER_REPO, PostgresStore


@dataclass(frozen=True, slots=True)
class Probe:
    server_version: str  # "PostgreSQL 18.1"
    schema_version: int  # applied
    latest_version: int  # known to this issuebot

    @property
    def behind(self) -> bool:
        return self.schema_version < self.latest_version

    @property
    def ahead(self) -> bool:
        return self.schema_version > self.latest_version


class Database:
    """One URL; migrate, probe, read, and build the sink's store and the refresh listener.

    The URL is checked here, on the path every command takes (#105): psycopg would also accept
    libpq's keyword/value conninfo, but ``describe`` can only take the URL spelling apart
    without its password, so a value in any other spelling is refused before a connection is
    attempted, with a ``DatabaseError`` that names the rule and never the value.
    """

    def __init__(self, url: str, *, connect: Connector = connect) -> None:
        if not is_postgres_url(url):
            raise DatabaseError(NOT_A_URL)
        self._url = url
        self._connect = connect

    @property
    def description(self) -> str:
        return describe(self._url)

    async def migrate(self) -> MigrationResult:
        return await migrate(self._url, connect=self._connect)

    async def probe(self) -> Probe:
        """Server version and schema version; raises DatabaseError when unreachable."""
        latest = len(discover_migrations())
        async with self._open() as conn:
            row = await (await conn.execute("SELECT version()")).fetchone()
            version = " ".join(str(row[0]).split()[:2]) if row else "PostgreSQL ?"
            applied = await schema_version(conn)
        return Probe(server_version=version, schema_version=applied, latest_version=latest)

    @asynccontextmanager
    async def queries(self) -> AsyncIterator[Queries]:
        """A Queries object on one connection; psycopg errors inside become DatabaseError."""
        async with self._open() as conn:
            yield Queries(conn)

    async def register_repo(
        self, repo: str, labels: GitHubLabels, workflow_path: str | None
    ) -> None:
        """Upsert the worker's row in ``repos``: what the dashboard lists and lays out by.

        A one-shot write before the sinks start, so a failure is a startup failure, not a
        queued item that could be dropped (spec §5).
        """
        async with self._open() as conn:
            await conn.execute(
                REGISTER_REPO,
                {
                    "repo": repo,
                    "labels": Jsonb(labels.model_dump()),
                    "workflow_path": workflow_path,
                },
            )

    def store(self, labels: GitHubLabels, repo: str) -> PostgresStore:
        return PostgresStore(self._url, repo=repo, labels=labels, connect=self._connect)

    def listener(
        self, on_notify: Callable[[], None], *, repo: str | None = None
    ) -> RefreshListener:
        return RefreshListener(self._url, on_notify, repo=repo, connect=self._connect)

    async def notify_refresh(self, repo: str | None = None) -> None:
        """NOTIFY the refresh channel: with a repository, that worker ticks at once; without
        one, every listening worker does."""
        async with self._open() as conn:
            await conn.execute("SELECT pg_notify(%s, %s)", (REFRESH_CHANNEL, repo or ""))

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[AsyncConnection]:
        try:
            conn = await self._connect(self._url)
        except psycopg.Error as exc:
            message = redact(f"cannot connect: {error_text(exc)}", self._url)
            raise StoreUnavailableError(message) from exc
        try:
            yield conn
        except psycopg.Error as exc:
            raise classify(exc, self._url) from exc
        finally:
            await conn.close()
