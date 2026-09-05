"""Tests for the Database facade: hermetic where possible, otherwise against DATABASE_URL."""

from typing import Any

import psycopg
import pytest

from issuebot.config import GitHubLabels
from issuebot.db import StoreUnavailableError
from issuebot.db.database import Database, Probe
from issuebot.db.listen import RefreshListener
from issuebot.db.store import PostgresStore

URL = "postgresql://issuebot:s3cret@db.example:5433/issuebot"


async def refuse(url: str) -> Any:
    raise psycopg.OperationalError(f"connection to {url} failed: refused")


def test_description_hides_the_password() -> None:
    assert Database(URL).description == "postgresql://issuebot@db.example:5433/issuebot"


def test_store_and_listener_are_built_with_the_url() -> None:
    database = Database(URL, connect=refuse)
    store = database.store(GitHubLabels())
    assert isinstance(store, PostgresStore)
    assert store._url == URL
    listener = database.listener(lambda: None)
    assert isinstance(listener, RefreshListener)
    assert listener._url == URL


async def test_probe_reports_an_unreachable_server_without_the_url() -> None:
    with pytest.raises(StoreUnavailableError, match="cannot connect") as exc:
        await Database(URL, connect=refuse).probe()
    assert "s3cret" not in exc.value.message
    assert "<database url>" in exc.value.message


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
    assert (before.schema_version, before.latest_version, before.behind) == (0, 2, True)
    result = await database.migrate()
    assert result.applied == ("0001_initial", "0002_run_turns")
    after = await database.probe()
    assert (after.schema_version, after.behind, after.ahead) == (2, False, False)
