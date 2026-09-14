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
        "postgresql://issuebot@/arrowbot_test?host=/var/run/postgresql",
        "POSTGRESQL://u@h/db",
    ],
)
def test_a_well_formed_postgres_url_parses(url: str) -> None:
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
        "postgresql://u@h:notaport/db",
        "postgresql://u@h:70000/db",
        "postgresql://[::1/issuebot",
        "",
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


@pytest.mark.parametrize(
    "url",
    [
        KEYWORDS,
        "postgresql:host=db password=s3cretpassword",
        "postgresql://[bad",
        "postgresql://u@h:notaport/db",
        "s3cretpassword",
    ],
)
def test_describe_is_the_placeholder_for_anything_it_cannot_take_apart(url: str) -> None:
    """The keyword/value spelling carries its password in clear, so a value that is not a URL
    is never echoed: a placeholder, not the string, whatever it turns out to hold."""
    assert describe(url) == REDACTED


def test_dsn_secrets_finds_the_url_password_in_the_userinfo_and_the_query() -> None:
    assert dsn_secrets(URL) == ("s3cret",)
    assert dsn_secrets("postgresql://u:p%40ss@h/db") == ("p%40ss", "p@ss")
    assert dsn_secrets("postgresql://u@h/db?password=q%20r&password=other") == (
        "q%20r",
        "other",
        "q r",
    )
    assert dsn_secrets("postgresql://u@h/db") == ()
    assert dsn_secrets("postgresql://u:@h/db?password=") == ()


def test_dsn_secrets_finds_the_keyword_password_bare_or_quoted() -> None:
    assert dsn_secrets(KEYWORDS) == ("s3cretpassword",)
    assert dsn_secrets("host=db password='s3 cret' dbname=x") == ("s3 cret",)
    assert dsn_secrets("host=db password = 'it\\'s' dbname=x") == ("it's",)
    assert dsn_secrets("host=db PASSWORD=Upper dbname=x") == ("Upper",)
    assert dsn_secrets("host=db user_password=nope") == ()
    assert dsn_secrets("host=db dbname=x") == ()
    assert dsn_secrets("") == ()
