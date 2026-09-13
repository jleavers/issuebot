"""githubstatus.com, read as annotation and never as a gate (#88).

The Statuspage summary is a *lagging* indicator: during the incident of 2026-09-13 a merge
broke at 08:53Z, the incident was not declared until 09:16Z, and the endpoint answered "All
Systems Operational" in between. So nothing here decides whether issuebot works an issue. It
exists to put a name next to first-party evidence the worker has already gathered -- a GitHub
fetch this worker could not make -- and to answer one advisory ``validate`` check.

Everything is therefore total: an unreachable page, a slow one, a 500, a body that is not
JSON or not the shape expected, all read as "no answer", which costs the annotation and
nothing else.
"""

import http.client
import json
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from issuebot.log import get_logger

SUMMARY_URL = "https://www.githubstatus.com/api/v2/summary.json"
# Short: a worker only asks while it is already held, and validate is a human waiting at a
# terminal. Neither should spend long on a third party that cannot decide anything.
SUMMARY_TIMEOUT_S = 5.0
# The body is a third party's and ends up in a log line, a jsonb column and an HTML page, so
# both the read and the text built from it are bounded.
MAX_BODY_BYTES = 256 * 1024
MAX_NAMED_COMPONENTS = 6
MAX_DETAIL_CHARS = 200
_OPERATIONAL = "operational"
_USER_AGENT = "issuebot"
_SCHEMES = ("http", "https")


@dataclass(frozen=True, slots=True)
class GitHubStatus:
    """What the status page says, reduced to what an operator needs in one line."""

    indicator: str
    description: str
    impaired: tuple[str, ...]
    incidents: tuple[str, ...]

    @property
    def operational(self) -> bool:
        """Nothing to report: no unresolved incident and every component healthy."""
        return self.indicator in ("none", "") and not self.impaired and not self.incidents

    @property
    def detail(self) -> str:
        """One line: the impaired components, else the unresolved incidents, else the
        page's own description of itself.

        Components first and alone: Statuspage names the same outage twice when an incident
        is open against a component, and "Pull Requests, major outage" is the more useful
        half of "Pull Requests, major outage; Incident with Pull Requests".
        """
        named = self.impaired or self.incidents
        text = "; ".join(named) if named else self.description
        if len(text) > MAX_DETAIL_CHARS:
            text = text[: MAX_DETAIL_CHARS - 1].rstrip() + "…"
        return text


def fetch_status_summary(
    url: str = SUMMARY_URL, *, timeout_s: float = SUMMARY_TIMEOUT_S
) -> str | None:
    """GET the summary with urllib; ``None`` for anything that is not a readable body.

    Blocking, so callers on the event loop run it in a thread. Never raises: a status page
    that cannot answer must not be able to break the caller that asked (#17). Only ``http``
    and ``https`` are fetched, so no override of the URL can turn this into a file read.

    ``timeout_s`` bounds the connection and the read, not the name lookup: ``urlopen`` resolves
    before it has a socket to set a timeout on. A host whose nameservers are unreachable --
    which is one of the ways the ``gh`` polls come to fail in the first place -- can therefore
    spend its resolver's own budget here. Callers ask once per hold, so the cost is bounded by
    that rather than by this.
    """
    log = get_logger(__name__)
    try:
        if urlsplit(url).scheme not in _SCHEMES:
            log.debug("github_status_unreadable", error="unsupported URL scheme")
            return None
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read(MAX_BODY_BYTES)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        log.debug("github_status_unreadable", error=f"{type(exc).__name__}: {exc}")
        return None
    return body.decode("utf-8", errors="replace")


def parse_status_summary(payload: str | None) -> GitHubStatus | None:
    """The summary reduced to a ``GitHubStatus``; ``None`` when there is nothing usable.

    Pure and total. Every field is optional as far as this reader is concerned: the shape is
    Statuspage's and can change without issuebot being told, and a reading it cannot make is
    the same as no reading at all.
    """
    if not payload:
        return None
    try:
        document = json.loads(payload)
    except ValueError, TypeError, RecursionError:
        # RecursionError is the one json.loads raises that is neither: ~100k levels of nesting
        # is about 200 KB, which fits inside MAX_BODY_BYTES, so the read cap does not cover it.
        return None
    if not isinstance(document, dict):
        return None
    status = document.get("status")
    status = status if isinstance(status, dict) else {}
    indicator = _text(status.get("indicator"))
    description = _text(status.get("description"))
    impaired = _impaired(document.get("components"))
    incidents = _incident_names(document.get("incidents"))
    if not (indicator or description or impaired or incidents):
        return None
    return GitHubStatus(
        indicator=indicator,
        description=description or "status unknown",
        impaired=impaired,
        incidents=incidents,
    )


def _impaired(components: object) -> tuple[str, ...]:
    """``("Pull Requests, major outage", ...)`` for each component that is not operational.

    Group rows are skipped: Statuspage mirrors a group's worst child into the group, so
    counting both names the same outage twice.
    """
    if not isinstance(components, list):
        return ()
    named: list[str] = []
    for component in components:
        if not isinstance(component, dict) or component.get("group") is True:
            continue
        state = _text(component.get("status"))
        name = _text(component.get("name"))
        if not name or not state or state == _OPERATIONAL:
            continue
        named.append(f"{name}, {state.replace('_', ' ')}")
        if len(named) == MAX_NAMED_COMPONENTS:
            break
    return tuple(named)


def _incident_names(incidents: object) -> tuple[str, ...]:
    """The incidents' names: what the page reports when no component has been marked yet.

    Not filtered on ``status``: the Statuspage *summary* carries only unresolved incidents, and
    a reader that cannot rely on the shape (see ``parse_status_summary``) has no business
    deciding an incident is over from a field that may be missing.
    """
    if not isinstance(incidents, list):
        return ()
    named: list[str] = []
    for incident in incidents:
        if not isinstance(incident, dict):
            continue
        name = _text(incident.get("name"))
        if not name:
            continue
        named.append(name)
        if len(named) == MAX_NAMED_COMPONENTS:
            break
    return tuple(named)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
