"""Tests for the database connection helpers (no database needed)."""

import psycopg
import pytest

from issuebot.db import (
    LOCK_TIMEOUT_S,
    RECONNECT_DELAYS_S,
    REDACTED,
    STATEMENT_TIMEOUT_S,
    StoreError,
    StoreUnavailableError,
    classify,
    describe,
    error_text,
    is_postgres_url,
    reconnect_delay,
    redact,
    session_statements,
)

URL = "postgresql://issuebot:s3cret@db.example:5433/issuebot?sslmode=require"


def test_is_postgres_url_accepts_both_schemes_and_rejects_others() -> None:
    assert is_postgres_url(URL)
    assert is_postgres_url("postgres://u@h/db")
    assert not is_postgres_url("mysql://u@h/db")
    assert not is_postgres_url("not a url")
    assert not is_postgres_url("http://[bad")


def test_describe_drops_the_password_and_keeps_the_rest() -> None:
    assert describe(URL) == "postgresql://issuebot@db.example:5433/issuebot"
    assert describe("postgresql://db/issuebot") == "postgresql://db/issuebot"
    assert describe("postgresql://[bad") == REDACTED
    assert describe("postgresql://u@h:notaport/db") == REDACTED


def test_redact_removes_the_url_and_the_bare_password() -> None:
    text = f"connection to {URL} failed: password s3cret rejected"
    assert redact(text, URL) == f"connection to {REDACTED} failed: password {REDACTED} rejected"
    assert redact("nothing here", URL) == "nothing here"


def test_redact_copes_without_a_password_or_with_a_malformed_url() -> None:
    assert redact("x postgresql://u@h/db y", "postgresql://u@h/db") == f"x {REDACTED} y"
    assert redact("left alone", "postgresql://[bad") == "left alone"


def test_reconnect_delay_walks_the_table_and_then_repeats_the_last_value() -> None:
    assert [reconnect_delay(n) for n in range(1, 9)] == [1, 2, 4, 8, 16, 30, 30, 30]
    assert reconnect_delay(0) == RECONNECT_DELAYS_S[0]


def test_error_text_keeps_the_first_line_only() -> None:
    assert error_text(psycopg.OperationalError("refused\n\tIs the server running?")) == "refused"
    assert error_text(psycopg.OperationalError("")) == "OperationalError"


def test_classify_separates_connection_failures_from_statement_failures() -> None:
    lost = classify(psycopg.OperationalError(f"server closed {URL}"), URL)
    assert isinstance(lost, StoreUnavailableError)
    assert lost.message == f"OperationalError: server closed {REDACTED}"
    broken = classify(psycopg.InterfaceError("the connection is closed"), URL)
    assert isinstance(broken, StoreUnavailableError)
    bad = classify(psycopg.DataError("invalid input for s3cret"), URL)
    assert isinstance(bad, StoreError)
    assert bad.message == f"DataError: invalid input for {REDACTED}"


@pytest.mark.parametrize("exc", [psycopg.OperationalError("x"), psycopg.DataError("y")])
def test_classify_never_leaks_the_url(exc: psycopg.Error) -> None:
    assert "s3cret" not in classify(exc, URL).message


def test_every_connection_bounds_its_lock_and_statement_waits() -> None:
    """The connect timeout bounds the handshake; these bound what comes after it (#110)."""
    assert session_statements() == (
        "SET TIME ZONE 'UTC'",
        f"SET lock_timeout = '{LOCK_TIMEOUT_S}s'",
        f"SET statement_timeout = '{STATEMENT_TIMEOUT_S}s'",
    )
    assert 0 < LOCK_TIMEOUT_S < STATEMENT_TIMEOUT_S
