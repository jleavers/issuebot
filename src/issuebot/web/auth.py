"""The dashboard's identity check and the refresh route's cross-site proof (#73).

Authorisation is a property of the request, never of the route the packet took to reach the
socket: every read carries the credential, and the one write carries something a cross-site
page cannot produce. The credential is one shared password, ``ISSUEBOT_WEB_PASSWORD``,
presented as HTTP Basic under any username. Basic needs no login page, no session store and
no signing key; a browser caches it per realm, so htmx's live partial and the charts' fetches
carry it without a line of script, and ``curl -u`` covers the JSON API.

Because a browser replays cached Basic credentials on a cross-site form POST, the credential
alone does not prove that ``POST .../refresh`` came from the operator's own page. The proof
is a custom request header: an HTML form cannot set one, and a cross-site script cannot
either without a CORS preflight, which this app never answers. The header is the one htmx
already sends from the Poll-now button. A browser that names the request's provenance
(``Sec-Fetch-Site``) is believed when it says ``cross-site``, whatever else the request
carries.

Everything here is pure, so the rules are tested as values rather than through the app.
"""

import base64
import binascii
import hmac
from collections.abc import Mapping

REALM = "issuebot"
CHALLENGE = f'Basic realm="{REALM}", charset="UTF-8"'
PROOF_HEADER = "HX-Request"
PROVENANCE_HEADER = "Sec-Fetch-Site"


def presented_password(authorization: str | None) -> str | None:
    """The password inside a ``Basic`` authorization header, or None when there is none.

    The username is not read: the dashboard has one credential and any name presents it.
    Anything that is not well-formed Basic -- another scheme, bad base64, bytes that are not
    UTF-8, no colon -- is no credential at all, which the caller answers with the challenge.
    """
    if not authorization:
        return None
    scheme, _, encoded = authorization.strip().partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except binascii.Error, UnicodeDecodeError, ValueError:
        return None
    _username, colon, password = decoded.partition(":")
    return password if colon else None


def credential_matches(presented: str | None, password: str) -> bool:
    """Constant-time equality against the configured password; None never matches."""
    if presented is None:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), password.encode("utf-8"))


def refresh_refusal(headers: Mapping[str, str]) -> str | None:
    """Why a refresh request is refused, or None when it carries the proof.

    ``headers`` is read case-insensitively by the caller's mapping (Starlette's is); a plain
    dict in a test wants the canonical spellings above.
    """
    provenance = headers.get(PROVENANCE_HEADER)
    if provenance is not None and provenance.strip().lower() == "cross-site":
        return f"refresh refused: {PROVENANCE_HEADER} is cross-site"
    if not headers.get(PROOF_HEADER):
        return f"refresh needs the {PROOF_HEADER} header, which a cross-site form cannot send"
    return None
