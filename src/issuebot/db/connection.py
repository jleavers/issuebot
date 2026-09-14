"""Connection helpers: connect, describe and redact a database URL, reconnect backoff."""

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import psycopg
from psycopg import AsyncConnection

from issuebot.db.errors import DatabaseError, StoreError, StoreUnavailableError

CONNECT_TIMEOUT_S = 5
# Every connection's wait on a lock and on a statement (#110). The connect timeout bounds
# the handshake alone; without these, a statement that blocks on the migration's advisory
# lock, or on anything a co-tenant of the store holds, waits forever with no log line and no
# exit, and so no restart. A lock the migration cannot take in this long is held by someone
# who is not going to let go; a statement this long is not one issuebot makes.
LOCK_TIMEOUT_S = 10
STATEMENT_TIMEOUT_S = 60
RECONNECT_DELAYS_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
REDACTED = "<database url>"
POSTGRES_SCHEMES = ("postgresql", "postgres")
APPLICATION_NAME = "issuebot"

Connector = Callable[[str], Awaitable[AsyncConnection]]


def is_postgres_url(url: str) -> bool:
    try:
        return urlsplit(url).scheme in POSTGRES_SCHEMES
    except ValueError:
        return False


def describe(url: str) -> str:
    """``postgresql://user@host:port/db`` without the password, for log lines."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return REDACTED
    user = f"{parts.username}@" if parts.username else ""
    host = parts.hostname or ""
    suffix = f":{port}" if port else ""
    return f"{parts.scheme}://{user}{host}{suffix}{parts.path}"


def redact(text: str, url: str) -> str:
    """Replace the full URL and, on its own, its password with a placeholder."""
    redacted = text.replace(url, REDACTED)
    try:
        password = urlsplit(url).password
    except ValueError:
        return redacted
    if password:
        redacted = redacted.replace(password, REDACTED)
    return redacted


def reconnect_delay(attempt: int) -> float:
    """The delay before reconnect attempt ``attempt`` (1-based); the last value from then on."""
    index = min(max(attempt, 1), len(RECONNECT_DELAYS_S)) - 1
    return RECONNECT_DELAYS_S[index]


def error_text(exc: BaseException) -> str:
    """The first line of an exception's message (libpq appends hints on further lines)."""
    lines = str(exc).strip().splitlines()
    return lines[0] if lines else type(exc).__name__


def classify(exc: psycopg.Error, url: str) -> DatabaseError:
    """StoreUnavailableError for connection-level failures, StoreError otherwise; redacted."""
    message = redact(f"{type(exc).__name__}: {error_text(exc)}", url)
    if isinstance(exc, psycopg.OperationalError | psycopg.InterfaceError):
        return StoreUnavailableError(message)
    return StoreError(message)


def session_statements() -> tuple[str, ...]:
    """What every connection runs first: the time zone, and the lock and statement timeouts.

    ``SET`` statements rather than libpq ``options``, because a URL may carry ``options`` of
    its own (the test fixtures' ``search_path``) and a keyword would replace them whole.
    """
    return (
        "SET TIME ZONE 'UTC'",
        f"SET lock_timeout = '{LOCK_TIMEOUT_S}s'",
        f"SET statement_timeout = '{STATEMENT_TIMEOUT_S}s'",
    )


async def connect(url: str) -> AsyncConnection:
    """One autocommit connection with a bounded connect timeout, a UTC session time zone,
    and a bounded wait on any lock or statement (``session_statements``)."""
    conn = await AsyncConnection.connect(
        url,
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT_S,
        application_name=APPLICATION_NAME,
    )
    for statement in session_statements():
        await conn.execute(statement)
    return conn
