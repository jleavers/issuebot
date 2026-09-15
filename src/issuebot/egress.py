"""The session's network egress, bounded by an allow-listing ``CONNECT`` proxy (#126).

#109 fixed two of the three properties of a session's authority -- its tool set and the reach
of its token -- and left the third where it was: the session's network egress was the
container's, and a compose network cannot filter by name. ``WebFetch`` and ``WebSearch`` are
denied to the model, but a ``curl`` under ``Bash`` left the container for anywhere, so a
hostile issue that got past triage could exfiltrate ``GH_TOKEN`` to a host of its choosing, or
fetch the next page of its own instructions from one.

This module is the filter. It is a forward proxy that speaks exactly one method, ``CONNECT``,
and answers it only for a host on an allow-list; the worker and every session reach it through
``HTTPS_PROXY``, and compose gives the worker no other route off the host. Two halves, and
neither is sufficient alone:

* **The network** is what makes the proxy unavoidable. A container whose every network is
  ``internal`` has no default route, so the proxy is not a policy the session is asked to
  observe -- it is the only way out. ``validate``'s ``egress`` check probes both halves, since
  a shared network created without ``--internal`` leaves a route the proxy knows nothing about.
* **The allow-list** is what makes the route narrow. It is the operator's
  (``ISSUEBOT_EGRESS_ALLOW``), over a default that covers the workflow's own needs: Anthropic
  for ``claude``, GitHub for ``gh`` and ``git``, and nothing else.

``CONNECT`` is enough, and is deliberately all of it. The proxy reads the host name out of the
request line and never sees a byte of the TLS session it then relays, so nothing here has to
hold a certificate authority, and a filter that cannot read the traffic cannot be blamed for
what it failed to notice in it. The cost is that plain ``http://`` is refused outright rather
than forwarded: a registry that is not served over TLS cannot be reached from a session, which
is a bound worth having in a process that runs text anybody can write.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from issuebot.log import get_logger

# What the workflow itself needs, and nothing else -- the operator's registries are the
# operator's to add (`ALLOW_ENV`). Every name here is reached by a process issuebot spawns:
#
#   api.anthropic.com        `claude -p`, every turn.
#   platform.claude.com      where `claude` authenticates: `/oauth/authorize` for the login
#                            recipe in the README, and `/v1/oauth/token` for the exchange and
#                            *refresh* of the OAuth credential it runs with -- the
#                            CLAUDE_CODE_OAUTH_TOKEN a container session is handed, or the host
#                            route's own login. Without it an
#                            existing deployment keeps working until its access token expires
#                            and then fails every session, reporting a 403 about an allow-list
#                            rather than a credential -- which is why it is in the default and
#                            not left to the operator to discover.
#   claude.ai                the same login's origin, which the client sends itself with.
#   github.com               `gh repo clone`, and every `git fetch`/`push` in a workspace.
#   api.github.com           every `gh api`, `gh issue`, `gh pr` call, the worker's own polls
#                            among them.
#   objects.githubusercontent.com   release assets and raw objects `gh` redirects to.
#   www.githubstatus.com     the Statuspage summary `issuebot.github.status` reads to annotate
#                            a dispatch hold (#88).
#   hooks.slack.com          the worker's own notifications (`issuebot.notifications`), which
#                            go out through `urllib` and so through this proxy. Silent if it
#                            is refused -- `urllib_post` never raises -- so an upgrade would
#                            otherwise cost a deployment every `blocked` line with nothing but
#                            a log entry to say so. A Slack-compatible endpoint on another
#                            host is the operator's to add, and `validate` says so.
#
# Not here on purpose: pypi.org, registry.npmjs.org and the rest. They are the *target*
# repository's needs rather than the workflow's, they differ per deployment, and a default
# that carried them would be a default nobody had chosen. Nor is a telemetry host: the shipped
# `claude` names none, and one it named would be a name a session could post to.
DEFAULT_ALLOW: tuple[str, ...] = (
    "api.anthropic.com",
    "platform.claude.com",
    "claude.ai",
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "www.githubstatus.com",
    "hooks.slack.com",
)
# The operator's extension, read from the environment rather than from `WORKFLOW.md`: the proxy
# is a service of its own with no workflow to load, and the list belongs to the deployment
# (`.env`) rather than to the repository the worker happens to watch.
ALLOW_ENV = "ISSUEBOT_EGRESS_ALLOW"
# The variables every client issuebot spawns reads to find this proxy: `claude`, `gh`, `git`,
# `uv`, `pip`, `npm` and `curl` all honour them. Both cases of each, because they are not
# interchangeable: curl deliberately ignores an upper-case `HTTP_PROXY` (a CGI script's
# environment carries the request's `Proxy:` header under that name), while other clients read
# only the upper-case spelling. A deployment that set one case would silently leave the other
# half of its tooling looking for a route that is not there.
PROXY_ENV_NAMES: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
# What `validate` asks the proxy about. `.invalid` is reserved by RFC 2606 and resolves
# nowhere, so a proxy that admits it has no allow-list worth the name -- and an operator whose
# own list happens to name it has said so in as many words.
PROBE_DENIED_HOST = "egress-probe.invalid"
# The one name on the default list the workflow cannot work without: every poll, every label
# move and every `gh` call a session makes goes to it, so a proxy that will not admit it is a
# deployment that has not started rather than one with a narrow list.
PROBE_REQUIRED_HOST = "api.github.com"
# And what it asks the *network* about: a name off the default list that really does answer on
# 443, so that "no route" is the container's doing rather than the host being down. RFC 2606
# reserves it for exactly this kind of use. Nothing is sent: the socket is opened and closed.
PROBE_DIRECT_HOST = "example.com"
# squid's, and what every HTTP client's documentation uses in its examples.
DEFAULT_PORT = 3128
# Loopback, like `issuebot web`: authority is not a property of where a socket is bound, but a
# service that filters egress has no business being reachable by default. compose passes
# 0.0.0.0 explicitly, behind a network only the worker joins.
DEFAULT_BIND = "127.0.0.1"
# The one port an entry implies. A CONNECT to any other port needs an entry that names it.
DEFAULT_TARGET_PORT = 443
# A request line plus headers. Generous for what a CONNECT carries and small enough that a
# client which never sends the blank line cannot cost anything.
MAX_REQUEST_BYTES = 8 * 1024
# How long the request may take to arrive. The tunnel that follows is not bounded here: a turn
# of `claude -p` is a single long-lived CONNECT, and an idle timeout on it would be a limit on
# how long a session may think. The gap this actually bounds is the one between a client
# connecting and finishing its request line -- every client issuebot spawns sends it at
# once -- and it is also how long a connection that says nothing occupies a slot in
# MAX_CONNECTIONS below, which is what makes such a connection churn rather than accumulate.
REQUEST_TIMEOUT_S = 10.0
# How long the proxy waits for the allowed host itself.
UPSTREAM_TIMEOUT_S = 30.0
_RELAY_CHUNK = 64 * 1024
# How many tunnels may be established at once, and how many connections may be accepted at
# once. Both are needed, and the second is the one that bounds *sustained* descriptor growth: a
# tunnel is counted only once its upstream is open, so a peer that connects and then says
# nothing holds a socket for REQUEST_TIMEOUT_S while counting towards nothing. MAX_CONNECTIONS
# is checked before a byte is read, so that shape is refused rather than accumulated. It is not
# a bound on the descriptor *table*: the check runs before the increment, so a refused
# connection is not itself counted and a burst larger than the limit still holds its sockets
# for as long as it takes to answer them.
#
# Be clear about what a shared counter can and cannot do here, because the obvious reading is
# wrong. A session is the adversary, the worker's own polls go through this same proxy, and the
# two cannot be told apart -- they share a container, and the proxy sees only sockets. So a
# shared ceiling is a shared *availability* ceiling: whatever the number, a session that reaches
# it refuses the worker too. The number is therefore chosen to sit well above any load this
# deployment produces -- a handful of concurrent sessions, each with a turn and a few `gh`
# calls -- rather than close to it, because a limit tight enough to be reached is a denial of
# service an attacker gets for free. What it buys is a definite 503 rather than `accept()`
# failing with EMFILE, which is the worse failure: asyncio answers that by removing the reader
# and re-arming it `ACCEPT_RETRY_DELAY` (1 s) later, so the listener stutters a second at a
# time and drops the pending backlog -- every client degraded, rather than one refused plainly.
# That also means 2048 client sockets plus up to MAX_TUNNELS upstreams assumes a descriptor
# limit well above ~2.3k, which every current Docker default is.
#
# The answer to a session that simply wants the proxy down is not this counter, which it can
# always reach; it is that the session is issuebot's own child, bounded by the run's timeouts,
# and that the attempt is in the log.
MAX_TUNNELS = 256
MAX_CONNECTIONS = 2048
# How long the proxy waits for its established tunnels on the way out. Since 3.12.1
# `Server.wait_closed()` waits for every handler task, and nothing bounds a tunnel's time -- one
# turn of `claude -p` is a single long CONNECT -- so waiting for them outright would hold the
# process until Docker's SIGKILL on every `docker compose up -d egress`, which is the documented
# way to change the allow-list. The listening socket closes at once either way; this is only how
# long the relays get, and the container is going away with them.
SHUTDOWN_DRAIN_S = 5.0
# Names a hostname may be made of. Anything else -- a credential in a userinfo part, a path, a
# space, a byte outside ASCII -- is not a host this proxy will look up.
_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-._")


@dataclass(frozen=True, slots=True)
class Rule:
    """One allow-list entry: a host (or a domain suffix) and the port it may be reached on."""

    name: str
    port: int
    subdomains: bool

    def matches(self, host: str, port: int) -> bool:
        if port != self.port:
            return False
        if host == self.name:
            return True
        return self.subdomains and host.endswith("." + self.name)

    def __str__(self) -> str:
        prefix = "." if self.subdomains else ""
        return f"{prefix}{self.name}:{self.port}"


def split_allow(text: str | None) -> list[str]:
    """The entries in an ``ISSUEBOT_EGRESS_ALLOW`` value: commas or whitespace, either way."""
    if not text:
        return []
    return [entry for entry in text.replace(",", " ").split() if entry]


def parse_rule(entry: str) -> Rule | None:
    """One entry as a ``Rule``, or ``None`` when it is not one.

    ``example.com`` is that host on 443; ``example.com:8443`` names the port; a leading dot
    (``.example.com``) matches the domain *and* every name under it, which is how an operator
    admits a CDN whose hosts they cannot enumerate.
    """
    text = entry.strip()
    if not text:
        return None
    subdomains = text.startswith(".")
    if subdomains:
        text = text[1:]
    name, sep, port_text = text.rpartition(":")
    if sep:
        if not port_text.isdigit():
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None
    else:
        name, port = text, DEFAULT_TARGET_PORT
    host = normalise_host(name)
    if host is None:
        return None
    return Rule(host, port, subdomains)


def parse_allow(entries: Iterable[str]) -> tuple[tuple[Rule, ...], tuple[str, ...]]:
    """The rules an operator's entries make, and a complaint for each one that makes none.

    Total: a typo costs its own entry and never the service. The proxy logs the complaints and
    serves the rules that parsed, because the alternative -- refusing to start -- is a worker
    with no egress at all, which fails every session rather than the one request the typo was
    about.
    """
    rules: list[Rule] = []
    complaints: list[str] = []
    for entry in entries:
        rule = parse_rule(entry)
        if rule is None:
            complaints.append(f"{entry!r} is not a host name, or host:port")
        elif rule not in rules:
            rules.append(rule)
    return tuple(rules), tuple(complaints)


def allow_rules(environ: Mapping[str, str]) -> tuple[tuple[Rule, ...], tuple[str, ...]]:
    """The deployment's allow-list: the default, extended by ``ISSUEBOT_EGRESS_ALLOW``."""
    return parse_allow([*DEFAULT_ALLOW, *split_allow(environ.get(ALLOW_ENV))])


def configured_proxy(environ: Mapping[str, str]) -> str | None:
    """The proxy this process would send an HTTPS request through, or ``None`` for none.

    Either case of the variable, lower first, which is the precedence curl and requests both
    use; ``HTTPS_PROXY`` rather than ``HTTP_PROXY`` because a ``CONNECT`` proxy is what there
    is, and everything issuebot reaches is ``https``.
    """
    for name in ("https_proxy", "HTTPS_PROXY"):
        value = environ.get(name, "").strip()
        if value:
            return value
    return None


def normalise_host(host: str) -> str | None:
    """A host name as it will be compared, or ``None`` for anything that is not one.

    Lower-cased, with the brackets of an IPv6 literal and a trailing root dot removed, and
    restricted to the characters a host name is made of. Nothing is folded or decoded: a name
    the proxy cannot spell plainly is a name it refuses, which is the safe direction for a
    comparison that decides whether a connection leaves the host.
    """
    text = host.strip()
    if text.startswith("[") or text.endswith("]"):
        # Brackets mean an IPv6 literal and nothing else, so what is inside one is held to
        # `ipaddress` rather than to the name rules below: `[example.com/x]` is not a host,
        # and this is the one parser that decides whether a connection leaves the container.
        if not (text.startswith("[") and text.endswith("]")):
            return None
        try:
            return str(ip_address(text[1:-1]).compressed).lower()
        except ValueError:
            return None
    text = text.rstrip(".").lower()
    if not text or len(text) > 253:
        return None
    if not set(text) <= _HOST_CHARS:
        return None
    if ".." in text:
        return None
    return text


def parse_connect_target(target: str) -> tuple[str, int] | None:
    """``host:port`` from a ``CONNECT`` request line, or ``None``.

    The port is mandatory, as RFC 9110 requires of the ``authority-form`` target, so nothing
    here has to guess what a client meant.
    """
    text = target.strip()
    if text.startswith("["):
        host_part, sep, port_text = text.partition("]")
        if not sep or not port_text.startswith(":"):
            return None
        host_part += "]"
        port_text = port_text[1:]
    else:
        host_part, sep, port_text = text.rpartition(":")
        if not sep:
            return None
    if not port_text.isdigit():
        return None
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None
    host = normalise_host(host_part)
    if host is None:
        return None
    return host, port


def allowed(host: str, port: int, rules: Sequence[Rule]) -> bool:
    """Whether the allow-list admits this host on this port."""
    return any(rule.matches(host, port) for rule in rules)


def _response(status: int, reason: str, body: str = "") -> bytes:
    """One complete HTTP/1.1 response, closed after it.

    Every refusal says which host it was about and names the variable that would admit it: the
    reader is an operator reading a session's transcript days later, and "403 Forbidden" on its
    own has sent more than one of them looking for the fault in their token.
    """
    payload = body.encode("utf-8", errors="replace")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
    )
    if status == 405:
        head += "Allow: CONNECT\r\n"
    return head.encode("ascii") + b"\r\n" + payload


class Proxy:
    """A ``CONNECT``-only forward proxy over a fixed allow-list."""

    def __init__(
        self,
        rules: Sequence[Rule],
        *,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
        upstream_timeout_s: float = UPSTREAM_TIMEOUT_S,
        max_tunnels: int = MAX_TUNNELS,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        self._rules = tuple(rules)
        self._request_timeout_s = request_timeout_s
        self._upstream_timeout_s = upstream_timeout_s
        self._max_tunnels = max_tunnels
        self._max_connections = max_connections
        # Three-quarters of the ceiling: the level the count must fall back to before another
        # saturation is worth a line of its own.
        self._recovered_at = max_connections * 3 // 4
        self._open = 0
        self._accepted = 0
        self._saturated = False
        self._log = get_logger(__name__)

    @property
    def rules(self) -> tuple[Rule, ...]:
        return self._rules

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One client connection, from its request line to the end of its tunnel.

        Never raises: a proxy is the only way out of the worker's network, so a connection that
        fails in a way nobody anticipated must cost that connection and not the service. Hence
        the two catches below -- the three types a connection ordinarily fails with, at DEBUG,
        and then `Exception` for everything else, at ERROR with its traceback, since that is a
        bug in the one chokepoint the whole deployment's egress goes through.
        `CancelledError` is a `BaseException` and so still propagates, which is what lets the
        server shut down.

        The accept-time bound is here rather than in `_serve` because it is about the
        descriptor, which this connection is already holding: past it the answer is 503 and the
        socket closes at once, without a read, a name lookup or a timeout to wait out.
        """
        if self._accepted >= self._max_connections:
            if not self._saturated:
                # The edge rather than every refusal. This is the cheapest line in the process
                # to provoke -- no request sent, no name looked up -- and compose sets no
                # logging options, so one line per connection would be the flood's second
                # payload. Cleared with hysteresis below rather than by the next admitted
                # connection: at the ceiling a slot frees constantly, so a single-step edge
                # would re-arm on each one and log per refusal after all.
                self._saturated = True
                self._log.warning(
                    "egress_connections_exhausted",
                    accepted=self._accepted,
                    limit=self._max_connections,
                )
            with contextlib.suppress(OSError):
                await _reply(
                    writer,
                    _response(
                        503,
                        "Service Unavailable",
                        f"the proxy is already holding {self._max_connections} connections\n",
                    ),
                )
            await _close(writer)
            return
        if self._saturated and self._accepted < self._recovered_at:
            # Far enough below the ceiling to call the episode over, so the next one is logged.
            self._saturated = False
        self._accepted += 1
        try:
            await self._serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            # The three a connection fails with in the ordinary course of events: a peer that
            # went away, a truncated request, a request head over the limit. Not worth a line
            # above DEBUG, since a session may produce them all day.
            self._log.debug("egress_connection_failed", error=f"{type(exc).__name__}: {exc}")
        except Exception:
            # Anything else is a bug in this module, and this module is the whole deployment's
            # egress. Swallowing it honours the contract above, but it goes out with its
            # traceback at ERROR: at DEBUG it would reach an operator as "some requests just
            # fail", with nothing in the log at any level they actually run.
            self._log.exception("egress_connection_error")
        finally:
            self._accepted -= 1
            await _close(writer)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(self._request_timeout_s):
                head = await reader.readuntil(b"\r\n\r\n")
        except TimeoutError:
            await _reply(writer, _response(408, "Request Timeout", "no request in time\n"))
            return
        except asyncio.LimitOverrunError:
            await _reply(
                writer,
                _response(431, "Request Header Fields Too Large", "request head too large\n"),
            )
            return
        except asyncio.IncompleteReadError:
            return
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = request_line.split()
        if len(parts) != 3:
            await _reply(writer, _response(400, "Bad Request", "not a request line\n"))
            return
        method, target, _version = parts
        if method.upper() != "CONNECT":
            self._log.warning("egress_method_refused", method=method.upper()[:16])
            await _reply(
                writer,
                _response(
                    405,
                    "Method Not Allowed",
                    f"{method.upper()[:16]} is not proxied; this proxy speaks CONNECT only, "
                    "so egress is HTTPS only\n",
                ),
            )
            return
        destination = parse_connect_target(target)
        if destination is None:
            await _reply(writer, _response(400, "Bad Request", "not a host:port target\n"))
            return
        host, port = destination
        if self._open >= self._max_tunnels:
            # Before the allow-list, so a flood is refused without a name lookup, and logged
            # once per refusal because this is the shape of a session exhausting the proxy the
            # worker's own polls depend on.
            self._log.warning("egress_tunnels_exhausted", open=self._open, limit=self._max_tunnels)
            await _reply(
                writer,
                _response(
                    503,
                    "Service Unavailable",
                    f"the proxy is already relaying {self._max_tunnels} connections\n",
                ),
            )
            return
        if not allowed(host, port, self._rules):
            # The one line an operator goes looking for, and the one a reviewer reads as the
            # record of an attempt: WARNING, with the name that was asked for and nothing else
            # off the wire.
            self._log.warning("egress_denied", host=host, port=port)
            await _reply(
                writer,
                _response(
                    403,
                    "Forbidden",
                    f"{host}:{port} is not on the egress allow-list; add it to "
                    f"{ALLOW_ENV} in the deployment's .env to admit it\n",
                ),
            )
            return
        try:
            async with asyncio.timeout(self._upstream_timeout_s):
                upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
        except (OSError, TimeoutError) as exc:
            self._log.warning(
                "egress_upstream_failed", host=host, port=port, error=type(exc).__name__
            )
            await _reply(writer, _response(502, "Bad Gateway", f"cannot reach {host}:{port}\n"))
            return
        self._log.debug("egress_allowed", host=host, port=port)
        self._open += 1
        try:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await _tunnel(reader, writer, upstream_reader, upstream_writer)
        finally:
            self._open -= 1
            await _close(upstream_writer)


async def _reply(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(payload)
    with contextlib.suppress(OSError):
        await writer.drain()


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError):
        writer.close()
        await writer.wait_closed()


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError, asyncio.LimitOverrunError):
        while chunk := await reader.read(_RELAY_CHUNK):
            writer.write(chunk)
            await writer.drain()
    with contextlib.suppress(OSError):
        writer.write_eof()


async def _tunnel(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    """Copy both ways; the *reply* direction is what says when the tunnel is over.

    Not the first of the two to finish. A client that has sent everything it means to send and
    half-closes its side is waiting for an answer, and cancelling the other direction there
    would hand it an empty response; ``_relay`` already passes the half-close on as
    ``write_eof``, so the request direction ending is news for the upstream and not for this.
    When the upstream closes, the request direction is cancelled with it -- and if the client
    disappears instead, the write that follows fails and ends this wait anyway.

    Nothing bounds an established tunnel's time: one turn of ``claude -p`` is a single long
    CONNECT, and an idle timeout on it would be a limit on how long a session may think.
    """
    to_upstream = asyncio.ensure_future(_relay(client_reader, upstream_writer))
    to_client = asyncio.ensure_future(_relay(upstream_reader, client_writer))
    try:
        await to_client
    finally:
        to_upstream.cancel()
        await asyncio.gather(to_upstream, to_client, return_exceptions=True)


async def serve(
    rules: Sequence[Rule], *, bind: str = DEFAULT_BIND, port: int = DEFAULT_PORT
) -> asyncio.Server:
    """Start the proxy and return the server, for a caller that will await its closing."""
    proxy = Proxy(rules)
    return await asyncio.start_server(proxy.handle, bind, port, limit=MAX_REQUEST_BYTES)


def probe_proxy(
    proxy_url: str, host: str, port: int = DEFAULT_TARGET_PORT, *, timeout_s: float = 10.0
) -> tuple[int, str] | str:
    """``CONNECT`` through the proxy at ``proxy_url``: the status it answered, or why not.

    Blocking and stdlib-only, like ``issuebot.github.status``: the one caller is ``validate``,
    which is a human at a terminal. The connection is closed the moment the status line is
    read, so an allowed probe costs the named host one accepted TCP connection and no bytes.
    A string return is the failure, already worded for the check's line.
    """
    parts = urlsplit(proxy_url if "//" in proxy_url else f"//{proxy_url}")
    try:
        proxy_host, proxy_port = parts.hostname, parts.port or DEFAULT_PORT
    except ValueError:
        return f"{proxy_url} is not a proxy URL"
    if parts.scheme not in ("", "http") or not proxy_host:
        return f"{proxy_url} is not an http:// proxy URL"
    try:
        with socket.create_connection((proxy_host, proxy_port), timeout_s) as sock:
            sock.settimeout(timeout_s)
            request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
            sock.sendall(request.encode("ascii"))
            line = b""
            while b"\r\n" not in line and len(line) < 512:
                chunk = sock.recv(512)
                if not chunk:
                    break
                line += chunk
    except (OSError, UnicodeEncodeError) as exc:
        return f"{proxy_host}:{proxy_port} did not answer ({type(exc).__name__})"
    status_line = line.split(b"\r\n", 1)[0].decode("latin-1")
    fields = status_line.split(None, 2)
    if len(fields) < 2 or not fields[1].isdigit():
        return f"{proxy_host}:{proxy_port} answered {status_line[:80]!r}, which is not HTTP"
    return int(fields[1]), fields[2] if len(fields) > 2 else ""


def reachable_directly(host: str, port: int = DEFAULT_TARGET_PORT, *, timeout_s: float) -> bool:
    """Whether a TCP connection to ``host`` leaves this container without the proxy.

    The other half of the invariant, and the half the proxy cannot speak for: an allow-list is
    a bound on egress only while there is no route around it. A connection is opened and closed
    at once; nothing is sent. A name that will not resolve, a refusal and a timeout all read as
    "no route", which is what a container on internal networks alone looks like.
    """
    try:
        with socket.create_connection((host, port), timeout_s):
            return True
    except OSError:
        return False
