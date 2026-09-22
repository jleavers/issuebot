"""Tests for issuebot.dsn: the one parser for ``database.url``, which fails closed (#105)."""

import pytest

from issuebot.dsn import REDACTED, describe, dsn_secrets, is_postgres_url, parse_url

URL = "postgresql://issuebot:s3cret@db.example:5433/issuebot?sslmode=require"
KEYWORDS = "host=db port=5432 user=issuebot password=s3cretpassword dbname=issuebot"


@pytest.mark.parametrize(
    "url",
    [
        URL,
        "postgres://u@h/db",
        "postgresql://db/issuebot",
        "postgresql://issuebot@/acme_test?host=/var/run/postgresql",
        "POSTGRESQL://u@h/db",
        # libpq's multi-host list, an IPv6 literal, and a port libpq will refuse at connect:
        # all URLs, all describable without their password.
        "postgresql://u:p@h1:5432,h2:5433/db",
        "postgresql://u:p@[::1]:5432/db",
        "postgresql://u@h:notaport/db",
    ],
)
def test_a_postgres_url_parses(url: str) -> None:
    assert is_postgres_url(url)
    assert parse_url(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        "mysql://u@h/db",
        "not a url",
        "http://[bad",
        KEYWORDS,
        # libpq's keyword/value form again: no ``//``, so ``urlsplit`` reads a path, not a host.
        "postgresql:host=db password=s3cretpassword",
        "postgresql://[::1/issuebot",
        "",
        # A URL prefix on keyword/value text: libpq's URL grammar has no whitespace.
        "postgresql://h/db password=s3cretpassword",
        " postgresql://u@h/db",
        # A password with an unencoded ``/``, ``?`` or ``#``: urlsplit ends the authority
        # inside it, and the userinfo would come out as the host.
        "postgresql://u:pa/ss@h/db",
        "postgresql://u:pa?ss@h/db",
        "postgresql://u:pa#ss@h/db",
    ],
)
def test_anything_else_is_not_a_postgres_url(url: str) -> None:
    assert not is_postgres_url(url)
    assert parse_url(url) is None


def test_describe_drops_the_password_and_keeps_the_rest() -> None:
    assert describe(URL) == "postgresql://issuebot@db.example:5433/issuebot"
    assert describe("postgresql://db/issuebot") == "postgresql://db/issuebot"
    assert describe("postgresql://issuebot@db/issuebot?password=s3cret") == (
        "postgresql://issuebot@db/issuebot"
    )
    assert (
        describe("postgresql://u:s3cret@h1:5432,h2:5433/db") == "postgresql://u@h1:5432,h2:5433/db"
    )
    assert describe("postgresql://u:s3cret@[::1]:5432/db") == "postgresql://u@[::1]:5432/db"
    assert describe("postgresql://u:p%40ss@h:notaport/db") == "postgresql://u@h:notaport/db"


@pytest.mark.parametrize(
    "url",
    [
        KEYWORDS,
        "postgresql:host=db password=s3cretpassword",
        "postgresql://[bad",
        "s3cretpassword",
        "postgresql://h/db password=s3cretpassword",
        "postgresql://u:s3cretpassword/x@h/db",
        "postgresql://u:s3cretpassword?x@h/db",
        "postgresql://u:s3cretpassword#x@h/db",
    ],
)
def test_describe_is_the_placeholder_for_anything_it_cannot_take_apart(url: str) -> None:
    """The keyword/value spelling carries its password in clear, so a value that is not a URL
    is never echoed: a placeholder, not the string, whatever it turns out to hold."""
    assert describe(url) == REDACTED


def test_dsn_secrets_finds_the_url_password_in_the_userinfo_and_the_query() -> None:
    assert dsn_secrets(URL) == ("s3cret",)
    assert dsn_secrets("postgresql://u:p%40ss@h/db") == ("p%40ss", "p@ss")
    # The keyword reading runs over a URL too, and over-matches the query as one bare token:
    # a spelling no line ever holds, so it costs nothing, and the real values are found.
    assert dsn_secrets("postgresql://u@h/db?password=q%20r&password=other") == (
        "q%20r&password=other",
        "q%20r",
        "other",
        "q r",
    )
    assert dsn_secrets("postgresql://u@h/db") == ()
    assert dsn_secrets("postgresql://u:@h/db?password=") == ()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:s3cretpassword@h1:5432,h2:5433/db",
        "postgresql://u:s3cretpassword@h:notaport/db",
        "postgresql://u:s3cretpassword@h:70000/db",
        "mysql://u:s3cretpassword@h/db",
        "postgresql:host=db password=s3cretpassword",
        "postgresql://h/db password=s3cretpassword",
    ],
)
def test_dsn_secrets_reads_the_url_password_whatever_else_is_wrong_with_the_url(url: str) -> None:
    """The mask is carried by the value, not by whichever guard ran first: a URL issuebot
    refuses, or one libpq will, still has its password known."""
    assert dsn_secrets(url) == ("s3cretpassword",)


def test_dsn_secrets_finds_the_keyword_password_bare_or_quoted() -> None:
    assert dsn_secrets(KEYWORDS) == ("s3cretpassword",)
    assert dsn_secrets("host=db password='s3 cret' dbname=x") == ("s3 cret",)
    assert dsn_secrets("host=db password = 'it\\'s' dbname=x") == ("it's",)
    assert dsn_secrets("host=db PASSWORD=Upper dbname=x") == ("Upper",)
    assert dsn_secrets("host=db user_password=nope") == ()
    assert dsn_secrets("host=db dbname=x") == ()
    assert dsn_secrets("") == ()
