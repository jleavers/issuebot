"""Connection helpers: connect, describe and redact a database URL, reconnect backoff."""

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import psycopg
from psycopg import AsyncConnection

from issuebot.db.errors import DatabaseError, StoreError, StoreUnavailableError

CONNECT_TIMEOUT_S = 5
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
