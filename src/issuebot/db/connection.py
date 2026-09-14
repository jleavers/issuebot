"""Connection helpers: connect, describe and redact a database URL, reconnect backoff."""

from collections.abc import Awaitable, Callable

import psycopg
from psycopg import AsyncConnection

from issuebot.db.errors import DatabaseError, StoreError, StoreUnavailableError
from issuebot.dsn import POSTGRES_SCHEMES, REDACTED, describe, dsn_secrets, is_postgres_url

__all__ = [
    "APPLICATION_NAME",
    "CONNECT_TIMEOUT_S",
    "NOT_A_URL",
    "POSTGRES_SCHEMES",
    "RECONNECT_DELAYS_S",
    "REDACTED",
    "Connector",
    "classify",
    "connect",
    "describe",
    "error_text",
    "is_postgres_url",
    "reconnect_delay",
    "redact",
]

CONNECT_TIMEOUT_S = 5
RECONNECT_DELAYS_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
APPLICATION_NAME = "issuebot"
NOT_A_URL = "database.url is not a postgresql:// URL (libpq's keyword/value form is not accepted)"

Connector = Callable[[str], Awaitable[AsyncConnection]]


def redact(text: str, url: str) -> str:
    """Replace the full DSN and, on its own, its password with a placeholder.

    The password is found by ``dsn_secrets`` in whichever spelling the DSN uses (#105), so a
    keyword/value DSN that ``Database`` refuses is still masked in the line that refuses it.
    """
    redacted = text.replace(url, REDACTED)
    for secret in dsn_secrets(url):
        redacted = redacted.replace(secret, REDACTED)
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


async def connect(url: str) -> AsyncConnection:
    """One autocommit connection with a bounded connect timeout and a UTC session time zone."""
    conn = await AsyncConnection.connect(
        url,
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT_S,
        application_name=APPLICATION_NAME,
    )
    await conn.execute("SET TIME ZONE 'UTC'")
    return conn
