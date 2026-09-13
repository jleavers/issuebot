"""Shared pytest fixtures."""

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from issuebot.github.models import Issue, StateLabel

# Read before the clean_env fixture removes it from the environment for every test.
_DATABASE_URL = os.environ.get("DATABASE_URL")

_ENV_VARS = (
    "GH_TOKEN",
    "DATABASE_URL",
    "SLACK_WEBHOOK_URL",
    "ISSUEBOT_WORKSPACE_ROOT",
    "ISSUEBOT_WORKFLOW",
    "ISSUEBOT_LOG_LEVEL",
    "ISSUEBOT_LOG_FORMAT",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test independent of the developer's shell environment."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_ansi_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin argparse's colour off, so an assertion on help text reads a plain string.

    Python 3.14 gave ``ArgumentParser`` ``color=True`` by default, so nothing in the CLI opted
    in: every parser colourises whenever ``_colorize.can_colorize()`` agrees, and then
    ``usage: issuebot`` is two escape sequences apart (#81). Whether it agrees is the
    developer's shell talking -- a tty, ``FORCE_COLOR``, ``NO_COLOR`` -- which is how one
    person's green suite is another's failure. One fixture answers it for the whole suite, so
    the next formatting change is one edit rather than one per assertion.

    ``PYTHON_COLORS`` is the variable that settles it rather than joining the argument: it is
    the first thing ``can_colorize`` looks at, ahead of ``NO_COLOR``, ``FORCE_COLOR`` and the
    tty test, so ``0`` is off whatever else is set, ``pytest -s`` in a terminal included. The
    one interpreter that disagrees is ``python -E``, which ignores the variable and so takes
    the suite back to guessing; nothing runs it that way, ``uv run pytest`` and CI included.

    It is also the one colour variable pytest itself ignores -- pytest reads ``PY_COLORS``,
    ``NO_COLOR`` and ``FORCE_COLOR`` -- so pinning it leaves pytest's own red and green alone.

    Set rather than deleted, and so on ``os.environ``, which is how it reaches the tests that
    spawn ``issuebot`` as a subprocess as well as the ones that call ``main`` in process.
    """
    monkeypatch.setenv("PYTHON_COLORS", "0")


@pytest.fixture
def make_issue() -> Callable[..., Issue]:
    """Build a consistent Issue; pass field overrides as keyword arguments."""

    def factory(**overrides: Any) -> Issue:
        fields: dict[str, Any] = {
            "id": "42",
            "identifier": "repo-42",
            "number": 42,
            "title": "Add retry backoff",
            "body": None,
            "author": "reporter",
            "github_state": "open",
            "state": StateLabel.TODO,
            "state_labels": ("issuebot/todo",),
            "labels": ("issuebot/todo",),
            "url": "https://github.com/example/repo/issues/42",
            "assignees": (),
            "created_at": datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
            "updated_at": datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            "closed_at": None,
            "linked_pr": None,
            "dispatchable": True,
        }
        fields.update(overrides)
        return Issue(**fields)

    return factory


def with_search_path(url: str, schema: str) -> str:
    """The same URL with ``options=-c search_path=<schema>`` appended to its query string."""
    parts = urlsplit(url)
    extra = "options=" + quote(f"-c search_path={schema}", safe="")
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit(parts._replace(query=query))


@contextmanager
def _fresh_schema() -> Iterator[str]:
    if not _DATABASE_URL:
        pytest.skip("DATABASE_URL is not set; database tests need a PostgreSQL server")
    schema = f"issuebot_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(_DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield with_search_path(_DATABASE_URL, schema)
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def db_url() -> Iterator[str]:
    """A DATABASE_URL scoped to a fresh schema; skipped when no database is configured."""
    with _fresh_schema() as url:
        yield url
