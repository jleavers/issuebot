"""The shape of ``database.url``: one parser for every place that describes, masks or accepts it.

A leaf module on purpose. ``issuebot.db`` imports ``issuebot.agent`` (for the turn capture), so
``issuebot.agent.scrub`` cannot import ``issuebot.db.connection`` without a cycle, and the two
used to carry their own ``urlsplit`` each -- which is how both came to fail open on a spelling
neither was written for (#105): psycopg accepts libpq's keyword/value conninfo as well as the
URL, and ``urlsplit`` on ``host=db password=s3cret dbname=issuebot`` reports no scheme and no
password rather than raising, so ``describe`` returned the whole string and ``redact`` masked
nothing. Everything here fails closed instead: a value that is not a
``postgresql://`` URL is described by the placeholder, and its password is looked for in
whichever spelling it holds.
"""

import re
from urllib.parse import SplitResult, unquote, urlsplit

REDACTED = "<database url>"
POSTGRES_SCHEMES = ("postgresql", "postgres")

# libpq's keyword/value form: ``password = 's3 cret'`` or ``password=s3cret``. A quoted value may
# escape a quote or a backslash with a backslash; a bare one runs to the next whitespace.
_KEYWORD_PASSWORD = re.compile(
    r"(?<![\w-])password\s*=\s*(?:'((?:\\.|[^'\\])*)'|(\S+))", re.IGNORECASE
)


def parse_url(url: str) -> SplitResult | None:
    """The parts of a ``postgresql://`` URL, or ``None`` for anything else.

    A URL here means what ``describe`` can take apart without its password: ``urlsplit``
    accepts it, the scheme is PostgreSQL's, and the authority marker ``//`` follows it
    (``postgresql:host=db`` is keyword/value text libpq would refuse, and ``urlsplit`` would
    read the rest as a path). The host part is not judged: libpq's multi-host form
    (``h1:5432,h2:5433``) is a URL, and a port that is not a number is libpq's error to make
    at connect time, redacted like any other. Two shapes are refused because ``urlsplit``
    reads them in a way ``describe`` would then echo: whitespace anywhere, which libpq's URL
    grammar has none of and keyword/value text is full of (``postgresql://h/db password=x``
    is that text with a URL prefix), and an ``@`` after the authority that the authority does
    not hold, which is a userinfo whose password carried an unencoded ``/``, ``?`` or ``#``,
    so that ``urlsplit`` ended the authority inside the password.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in POSTGRES_SCHEMES:
        return None
    rest = url[len(parts.scheme) + 1 :]
    if not rest.startswith("//"):
        return None
    if any(char.isspace() for char in url):
        return None
    if "@" in rest and "@" not in parts.netloc:
        return None
    return parts


def is_postgres_url(url: str) -> bool:
    """True for a ``postgresql://`` or ``postgres://`` URL; the only spelling issuebot
    accepts, since it is the only one it can describe without the password."""
    return parse_url(url) is not None


def describe(url: str) -> str:
    """``postgresql://user@host:port/db`` without the password, for log lines.

    The placeholder for anything ``parse_url`` rejects: a value this function cannot take apart
    is one it must not echo, since the keyword/value spelling carries its password in clear.
    The host part is the authority after its userinfo, as written, so a multi-host list or an
    IPv6 literal comes out as it went in.
    """
    parts = parse_url(url)
    if parts is None:
        return REDACTED
    user = f"{parts.username}@" if parts.username else ""
    hostport = parts.netloc.rpartition("@")[2]
    return f"{parts.scheme}://{user}{hostport}{parts.path}"


def dsn_secrets(url: str) -> tuple[str, ...]:
    """Every spelling of the password a DSN carries, longest first; empty when it has none.

    A URL keeps it in the userinfo (``postgresql://u:p@h/db``) or as a ``password`` query
    parameter (``?password=p``, which libpq accepts too), and each is returned as written and
    percent-decoded, since an error message may quote either. A keyword/value DSN keeps it in a
    ``password=`` keyword, bare or single-quoted. Nothing here decides whether the DSN is
    accepted -- ``Database`` does -- so a spelling issuebot refuses is still masked wherever it
    was quoted before the refusal: both readings run over every value, whatever it is, since
    a keyword in a URL's path is a hybrid libpq would read as keyword text (an over-match on
    ``?password=x&sslmode=...`` costs one bare token that no line ever holds).
    """
    found: list[str] = []
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if parts is not None:
        if parts.password:
            found.append(parts.password)
        for pair in parts.query.split("&"):
            name, _, value = pair.partition("=")
            if name == "password":
                found.append(value)
        # libpq percent-decodes both (and, unlike a form, never reads ``+`` as a space).
        found.extend(unquote(value) for value in list(found))
    for quoted, bare in _KEYWORD_PASSWORD.findall(url):
        found.append(bare or quoted.replace("\\'", "'").replace("\\\\", "\\"))
    unique = [value for value in dict.fromkeys(found) if value]
    return tuple(sorted(unique, key=len, reverse=True))
