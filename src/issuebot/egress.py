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
from urllib.parse import urlsplit

from issuebot.log import get_logger

# What the workflow itself needs, and nothing else -- the operator's registries are the
# operator's to add (`ALLOW_ENV`). Every name here is reached by a process issuebot spawns:
#
#   api.anthropic.com        `claude -p`, every turn.
#   statsig.anthropic.com    Claude Code's feature-flag endpoint, on Anthropic's own reference
#                            firewall list for the product. Same origin owner as the line
#                            above, so it widens the trust domain by nothing; a session that
#                            cannot reach it still runs, which is why it is the only
#                            non-essential name in the list.
#   github.com               `gh repo clone`, and every `git fetch`/`push` in a workspace.
#   api.github.com           every `gh api`, `gh issue`, `gh pr` call, the worker's own polls
#                            among them.
#   objects.githubusercontent.com   release assets and raw objects `gh` redirects to.
#   www.githubstatus.com     the Statuspage summary `issuebot.github.status` reads to annotate
#                            a dispatch hold (#88).
#
# Not here on purpose: pypi.org, registry.npmjs.org and the rest. They are the *target*
# repository's needs rather than the workflow's, they differ per deployment, and a default
# that carried them would be a default nobody had chosen.
DEFAULT_ALLOW: tuple[str, ...] = (
    "api.anthropic.com",
    "statsig.anthropic.com",
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "www.githubstatus.com",
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
# how long a session may think.
REQUEST_TIMEOUT_S = 30.0
# How long the proxy waits for the allowed host itself.
UPSTREAM_TIMEOUT_S = 30.0
_RELAY_CHUNK = 64 * 1024
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
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
        # An IPv6 literal, whose colons the allow-list compares verbatim.
        return text.lower() or None
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
    ) -> None:
        self._rules = tuple(rules)
        self._request_timeout_s = request_timeout_s
        self._upstream_timeout_s = upstream_timeout_s
        self._log = get_logger(__name__)

    @property
    def rules(self) -> tuple[Rule, ...]:
        return self._rules

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One client connection, from its request line to the end of its tunnel.

        Never raises: a proxy is the only way out of the worker's network, so a connection that
        fails in a way nobody anticipated must cost that connection and not the service.
        """
        try:
            await self._serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            self._log.debug("egress_connection_failed", error=f"{type(exc).__name__}: {exc}")
        finally:
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
        try:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await _tunnel(reader, writer, upstream_reader, upstream_writer)
        finally:
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
    """Copy both ways until either end is done, then stop the other.

    Both directions are needed for as long as either is live -- a turn of ``claude -p`` is one
    long request and a long streamed response -- so this waits for the first to finish and
    cancels the second rather than waiting for both: a half-closed peer that never reads again
    would otherwise hold the pair open for as long as the process runs.
    """
    tasks = [
        asyncio.ensure_future(_relay(client_reader, upstream_writer)),
        asyncio.ensure_future(_relay(upstream_reader, client_writer)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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
