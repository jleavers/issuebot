"""Tests for the Database facade: hermetic where possible, otherwise against DATABASE_URL."""

from typing import Any

import psycopg
import pytest

from issuebot.config import GitHubLabels
from issuebot.db import DatabaseError, StoreUnavailableError
from issuebot.db.database import Database, Probe
from issuebot.db.listen import REFRESH_CHANNEL, RefreshListener, refresh_channel
from issuebot.db.store import PostgresStore

URL = "postgresql://issuebot:s3cret@db.example:5433/issuebot"


async def refuse(url: str) -> Any:
    raise psycopg.OperationalError(f"connection to {url} failed: refused")


def test_description_hides_the_password() -> None:
    assert Database(URL).description == "postgresql://issuebot@db.example:5433/issuebot"


@pytest.mark.parametrize(
    "url",
    [
        "host=db.example port=5433 user=issuebot password=s3cret dbname=issuebot",
        "postgresql:host=db.example password=s3cret",
        "mysql://issuebot:s3cret@db.example/issuebot",
    ],
)
def test_anything_but_a_postgres_url_is_refused_before_connecting(url: str) -> None:
    """#105: psycopg would take libpq's keyword/value form, but ``describe`` cannot take it
    apart without the password, so the facade -- the path every command takes -- refuses it
    with a message that names the rule and never the value."""
    with pytest.raises(DatabaseError) as info:
        Database(url, connect=refuse)
    assert "postgresql:// URL" in info.value.message
    assert "s3cret" not in info.value.message
    assert "db.example" not in info.value.message


def test_store_and_listener_are_built_with_the_url() -> None:
    database = Database(URL, connect=refuse)
    store = database.store(GitHubLabels(), "example/repo")
    assert isinstance(store, PostgresStore)
    assert store._url == URL
    assert store.repo == "example/repo"
    listener = database.listener(lambda: None, repo="example/repo")
    assert isinstance(listener, RefreshListener)
    assert listener._url == URL
    assert listener._repo == "example/repo"


async def test_probe_reports_an_unreachable_server_without_the_url() -> None:
    with pytest.raises(StoreUnavailableError, match="cannot connect") as exc:
        await Database(URL, connect=refuse).probe()
    assert "s3cret" not in exc.value.message
    assert "<database url>" in exc.value.message


class _RecordingConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    async def execute(self, query: str, params: Any = None) -> None:
        self.executed.append((query, params))

    async def close(self) -> None:
        pass


async def test_notify_refresh_targets_the_repositorys_channel() -> None:
    """One channel per repository (#110); the bare channel only without one."""
    connections: list[_RecordingConnection] = []

    async def record(url: str) -> Any:
        conn = _RecordingConnection()
        connections.append(conn)
        return conn

    database = Database(URL, connect=record)
    await database.notify_refresh("example/repo")
    await database.notify_refresh()
    assert [conn.executed for conn in connections] == [
        [("SELECT pg_notify(%s, %s)", (refresh_channel("example/repo"), "example/repo"))],
        [("SELECT pg_notify(%s, %s)", (REFRESH_CHANNEL, ""))],
    ]


async def test_queries_and_notify_report_an_unreachable_server() -> None:
    database = Database(URL, connect=refuse)
    with pytest.raises(StoreUnavailableError, match="cannot connect"):
        async with database.queries():
            pass
    with pytest.raises(StoreUnavailableError, match="cannot connect"):
        await database.notify_refresh()


def test_probe_flags() -> None:
    assert Probe(server_version="PostgreSQL 18.1", schema_version=0, latest_version=1).behind
    assert Probe(server_version="PostgreSQL 18.1", schema_version=2, latest_version=1).ahead
    current = Probe(server_version="PostgreSQL 18.1", schema_version=1, latest_version=1)
    assert not current.behind and not current.ahead


# --- against a real server -------------------------------------------------------------------


async def test_probe_before_and_after_migrate(db_url: str) -> None:
    database = Database(db_url)
    before = await database.probe()
    assert before.server_version.startswith("PostgreSQL ")
    assert (before.schema_version, before.latest_version, before.behind) == (0, 4, True)
    result = await database.migrate()
    assert result.applied == (
        "0001_initial",
        "0002_run_turns",
        "0003_repos",
        "0004_run_turns_repo",
    )
    after = await database.probe()
    assert (after.schema_version, after.behind, after.ahead) == (4, False, False)
