"""The allow-listing CONNECT proxy that bounds the session's network egress (#126).

Hermetic: every connection in this file is to a loopback socket the test started, so nothing
here reaches a name the proxy would have to resolve. The compose topology the proxy sits in --
a worker whose networks are all internal -- is what the CI ``docker`` job proves; ``validate``
probes it in a live deployment.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from issuebot.egress import (
    ALLOW_ENV,
    DEFAULT_ALLOW,
    MAX_REQUEST_BYTES,
    PROBE_DENIED_HOST,
    PROXY_ENV_NAMES,
    Rule,
    allow_rules,
    allowed,
    configured_proxy,
    normalise_host,
    parse_allow,
    parse_connect_target,
    parse_rule,
    probe_proxy,
    reachable_directly,
    serve,
    split_allow,
)

# --- the allow-list, as text ----------------------------------------------------------


def test_split_allow_takes_commas_or_whitespace() -> None:
    assert split_allow("a.example, b.example  c.example\nd.example") == [
        "a.example",
        "b.example",
        "c.example",
        "d.example",
    ]
    assert split_allow("") == []
    assert split_allow(None) == []


def test_a_bare_name_is_that_host_on_443() -> None:
    assert parse_rule("example.com") == Rule("example.com", 443, False)


def test_a_name_may_carry_its_own_port() -> None:
    assert parse_rule("registry.example.com:8443") == Rule("registry.example.com", 8443, False)


def test_a_leading_dot_admits_the_domain_and_everything_under_it() -> None:
    rule = parse_rule(".example.com")
    assert rule == Rule("example.com", 443, True)
    assert rule is not None
    assert rule.matches("example.com", 443)
    assert rule.matches("cdn.assets.example.com", 443)
    # Not a suffix of the *name*: the dot is a label boundary, not a string one.
    assert not rule.matches("notexample.com", 443)
    assert not rule.matches("example.com.evil.test", 443)


def test_the_port_is_part_of_the_rule() -> None:
    rule = parse_rule("example.com")
    assert rule is not None
    assert rule.matches("example.com", 443)
    assert not rule.matches("example.com", 8443)


@pytest.mark.parametrize(
    "entry",
    [
        "",
        "   ",
        "example.com:",
        "example.com:0",
        "example.com:70000",
        "example.com:https",
        "http://example.com",
        "user:pass@example.com",
        "example.com/path",
        "exa mple.com",
        "exämple.com",
        "example..com",
    ],
)
def test_an_entry_that_is_not_a_host_makes_no_rule(entry: str) -> None:
    assert parse_rule(entry) is None


def test_parse_allow_keeps_what_parsed_and_complains_about_the_rest() -> None:
    rules, complaints = parse_allow(["a.example", "not a host", "b.example", "a.example"])
    assert [str(rule) for rule in rules] == ["a.example:443", "b.example:443"]
    assert complaints == ("'not a host' is not a host name, or host:port",)


def test_the_default_list_covers_the_workflows_own_needs_and_nothing_else() -> None:
    """The bar the issue set: Anthropic for ``claude``, GitHub for ``gh`` and ``git``.

    A registry is the *target* repository's need and differs per deployment, so it is the
    operator's (``ISSUEBOT_EGRESS_ALLOW``) and not a default nobody chose.
    """
    assert DEFAULT_ALLOW == (
        "api.anthropic.com",
        "statsig.anthropic.com",
        "github.com",
        "api.github.com",
        "objects.githubusercontent.com",
        "www.githubstatus.com",
    )
    assert all(
        name.endswith((".anthropic.com", ".github.com", ".githubusercontent.com"))
        or name == "github.com"
        or name == "www.githubstatus.com"
        for name in DEFAULT_ALLOW
    )


def test_the_operator_extends_the_default_and_never_replaces_it() -> None:
    rules, complaints = allow_rules({ALLOW_ENV: "pypi.org files.pythonhosted.org"})
    names = [rule.name for rule in rules]
    assert complaints == ()
    assert names[: len(DEFAULT_ALLOW)] == list(DEFAULT_ALLOW)
    assert names[len(DEFAULT_ALLOW) :] == ["pypi.org", "files.pythonhosted.org"]


def test_a_typo_in_the_operators_list_costs_its_own_entry_alone() -> None:
    rules, complaints = allow_rules({ALLOW_ENV: "pypi.org, http://nope/"})
    assert len(complaints) == 1
    assert "pypi.org" in [rule.name for rule in rules]
    assert allowed("api.github.com", 443, rules)


# --- host names -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Example.COM", "example.com"),
        ("example.com.", "example.com"),
        ("  example.com  ", "example.com"),
        ("[::1]", "::1"),
        ("127.0.0.1", "127.0.0.1"),
    ],
)
def test_normalise_host_spells_a_name_one_way(raw: str, expected: str) -> None:
    assert normalise_host(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", ".", "exa mple.com", "example.com/x", "user@example.com", "exämple.com", "a" * 254],
)
def test_normalise_host_refuses_what_is_not_a_name(raw: str) -> None:
    assert normalise_host(raw) is None


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("example.com:443", ("example.com", 443)),
        ("Example.com:8443", ("example.com", 8443)),
        ("[2001:db8::1]:443", ("2001:db8::1", 443)),
    ],
)
def test_parse_connect_target(target: str, expected: tuple[str, int]) -> None:
    assert parse_connect_target(target) == expected


@pytest.mark.parametrize(
    "target",
    ["example.com", "example.com:", "example.com:x", "example.com:0", "[2001:db8::1]", "", ":443"],
)
def test_parse_connect_target_refuses_the_rest(target: str) -> None:
    assert parse_connect_target(target) is None


# --- the proxy itself -----------------------------------------------------------------


class _Upstream:
    """A loopback server that echoes what it is sent, standing in for an allowed host."""

    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.read(64)
            writer.write(b"pong:" + data)
            await writer.drain()
            writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()


async def _proxy(entries: list[str]) -> tuple[asyncio.AbstractServer, str]:
    rules, _ = parse_allow(entries)
    server = await serve(rules, bind="127.0.0.1", port=0)
    return server, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"


def _speak(url: str, payload: bytes, *, then: bytes | None = None) -> bytes:
    """One request to the proxy; the first chunk back, plus the reply to ``then``."""
    host = url.removeprefix("http://")
    address, _, port = host.rpartition(":")
    with socket.create_connection((address, int(port)), 5) as sock:
        sock.settimeout(5)
        sock.sendall(payload)
        head = sock.recv(4096)
        if then is None:
            return head
        sock.sendall(then)
        return head + sock.recv(4096)


@pytest.mark.asyncio
async def test_an_allowed_name_is_tunnelled_byte_for_byte() -> None:
    upstream = _Upstream()
    await upstream.start()
    server, url = await _proxy([f"localhost:{upstream.port}"])
    request = f"CONNECT localhost:{upstream.port} HTTP/1.1\r\nHost: x\r\n\r\n".encode()
    answer = await asyncio.to_thread(_speak, url, request, then=b"hello")
    assert answer.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n")
    assert answer.endswith(b"pong:hello")
    server.close()
    await upstream.stop()


@pytest.mark.asyncio
async def test_a_name_off_the_list_is_refused_and_told_where_to_add_it() -> None:
    server, url = await _proxy(["allowed.test"])
    request = b"CONNECT evil.test:443 HTTP/1.1\r\n\r\n"
    answer = await asyncio.to_thread(_speak, url, request)
    assert answer.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    assert b"evil.test:443 is not on the egress allow-list" in answer
    assert ALLOW_ENV.encode() in answer
    server.close()


@pytest.mark.asyncio
async def test_an_allowed_name_on_a_port_that_is_not_is_refused() -> None:
    server, url = await _proxy(["allowed.test"])
    answer = await asyncio.to_thread(_speak, url, b"CONNECT allowed.test:8443 HTTP/1.1\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    server.close()


@pytest.mark.asyncio
async def test_plain_http_is_not_proxied_at_all() -> None:
    """CONNECT only, so the proxy never sees a URL, a header or a body -- and egress is HTTPS
    only, which is a bound worth having in a process that runs text anybody can write."""
    server, url = await _proxy(["allowed.test"])
    request = b"GET http://allowed.test/secret HTTP/1.1\r\nHost: allowed.test\r\n\r\n"
    answer = await asyncio.to_thread(_speak, url, request)
    assert answer.startswith(b"HTTP/1.1 405 Method Not Allowed\r\n")
    assert b"Allow: CONNECT" in answer
    server.close()


@pytest.mark.asyncio
async def test_an_unparseable_request_is_a_400() -> None:
    server, url = await _proxy(["allowed.test"])
    assert (await asyncio.to_thread(_speak, url, b"nonsense\r\n\r\n")).startswith(
        b"HTTP/1.1 400 Bad Request\r\n"
    )
    assert (await asyncio.to_thread(_speak, url, b"CONNECT nope HTTP/1.1\r\n\r\n")).startswith(
        b"HTTP/1.1 400 Bad Request\r\n"
    )
    server.close()


@pytest.mark.asyncio
async def test_a_request_head_that_never_ends_is_bounded() -> None:
    server, url = await _proxy(["allowed.test"])
    flood = b"CONNECT allowed.test:443 HTTP/1.1\r\n" + b"X: y\r\n" * MAX_REQUEST_BYTES
    answer = await asyncio.to_thread(_speak, url, flood)
    assert answer.startswith(b"HTTP/1.1 431 ")
    server.close()


@pytest.mark.asyncio
async def test_an_allowed_name_that_cannot_be_reached_is_a_502_and_not_a_hang() -> None:
    # A closed loopback port: allowed by name, refused by the host.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    server, url = await _proxy([f"localhost:{dead}"])
    answer = await asyncio.to_thread(
        _speak, url, f"CONNECT localhost:{dead} HTTP/1.1\r\n\r\n".encode()
    )
    assert answer.startswith(b"HTTP/1.1 502 Bad Gateway\r\n")
    server.close()


# --- the probes validate uses ---------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_proxy_reads_the_status_the_proxy_answered() -> None:
    upstream = _Upstream()
    await upstream.start()
    server, url = await _proxy([f"localhost:{upstream.port}"])
    assert await asyncio.to_thread(probe_proxy, url, "localhost", upstream.port) == (
        200,
        "Connection established",
    )
    assert (await asyncio.to_thread(probe_proxy, url, PROBE_DENIED_HOST))[:1] == (403,)
    server.close()
    await upstream.stop()


def test_probe_proxy_words_its_own_failure() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    answer = probe_proxy(f"http://127.0.0.1:{dead}", "example.com", timeout_s=2)
    assert isinstance(answer, str)
    assert "did not answer" in answer
    assert isinstance(probe_proxy("ftp://proxy.test", "example.com"), str)


def test_reachable_directly_is_false_for_a_port_with_nobody_on_it() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    assert reachable_directly("127.0.0.1", dead, timeout_s=1) is False


def test_reachable_directly_is_true_for_one_that_answers() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        assert reachable_directly("127.0.0.1", listener.getsockname()[1], timeout_s=2) is True


# --- the environment ------------------------------------------------------------------


def test_the_proxy_variables_are_both_cases_of_all_three() -> None:
    """Both cases on purpose: curl deliberately ignores an upper-case ``HTTP_PROXY``, while
    other clients read only the upper-case spelling."""
    assert set(PROXY_ENV_NAMES) == {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }


def test_configured_proxy_prefers_the_lower_case_spelling() -> None:
    assert configured_proxy({}) is None
    assert configured_proxy({"HTTPS_PROXY": "  "}) is None
    assert configured_proxy({"HTTPS_PROXY": "http://a:3128"}) == "http://a:3128"
    assert (
        configured_proxy({"HTTPS_PROXY": "http://a:3128", "https_proxy": "http://b:3128"})
        == "http://b:3128"
    )
