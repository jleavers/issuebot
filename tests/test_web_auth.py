"""The dashboard's identity gate and the refresh route's cross-site proof (#73).

Authorisation is a property of the request the app checks itself, never of where the socket
is bound: every read carries the credential, and the one write carries something a
cross-site form cannot send. The pure rules live in ``issuebot.web.auth``; the tests through
the app prove that no route, the raw turn parts above all, is reachable by a path that skips
the gate.
"""

from collections.abc import Iterator

import pytest
import structlog

from fakes.web import API, BASE, PASSWORD, REFRESH_HEADERS, RUN_ID, Harness, basic_auth
from issuebot.web import SECURITY_HEADERS, create_app
from issuebot.web.auth import (
    CHALLENGE,
    PROOF_HEADER,
    PROVENANCE_HEADER,
    credential_matches,
    presented_password,
    refresh_refusal,
)


@pytest.fixture
def h() -> Iterator[Harness]:
    harness = Harness()
    harness.seed_issue()
    with harness.client, harness.anonymous:
        yield harness


RAW = f"{BASE}/issues/7/runs/{RUN_ID}/turns/1"
GATED_PATHS = [
    "/",
    f"{BASE}/",
    f"{BASE}/partials/dashboard",
    f"{BASE}/issues",
    f"{BASE}/issues/7",
    RAW,
    f"{RAW}/prompt",
    f"{RAW}/stream",
    f"{RAW}/stderr",
    "/api/v1/repos",
    f"{API}/state",
    f"{API}/issues/7",
    f"{API}/stats?window=7d",
    "/no/such/path",
]


# --- the pure rules ----------------------------------------------------------------------------


def encoded(raw: str) -> str:
    import base64

    return "Basic " + base64.b64encode(raw.encode()).decode()


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("Bearer abc", None),
        ("Basic", None),
        ("Basic ???", None),
        (encoded("no-colon"), None),
        (encoded("alice:pw"), "pw"),
        (encoded(":pw"), "pw"),
        (encoded("alice:pw:with:colons"), "pw:with:colons"),
        ("basic " + encoded("x:y")[6:], "y"),
        (encoded("alice:pässword"), "pässword"),
        ("Basic /w==", None),  # valid base64, not UTF-8
    ],
)
def test_presented_password_reads_basic_and_nothing_else(header: str | None, expected) -> None:
    assert presented_password(header) == expected


def test_credential_matches_is_exact_and_none_never_matches() -> None:
    assert credential_matches("secret-value-12", "secret-value-12")
    assert not credential_matches("secret-value-1", "secret-value-12")
    assert not credential_matches("", "secret-value-12")
    assert not credential_matches(None, "secret-value-12")


@pytest.mark.parametrize(
    ("headers", "refused"),
    [
        ({}, True),
        ({PROOF_HEADER: "true"}, False),
        ({PROOF_HEADER: "true", PROVENANCE_HEADER: "same-origin"}, False),
        ({PROOF_HEADER: "true", PROVENANCE_HEADER: "none"}, False),
        ({PROOF_HEADER: "true", PROVENANCE_HEADER: "cross-site"}, True),
        ({PROOF_HEADER: "true", PROVENANCE_HEADER: " Cross-Site "}, True),
        ({PROVENANCE_HEADER: "same-origin"}, True),  # provenance alone is not the proof
        ({PROOF_HEADER: ""}, True),
    ],
)
def test_refresh_refusal(headers: dict[str, str], refused: bool) -> None:
    assert (refresh_refusal(headers) is not None) is refused


def test_create_app_refuses_an_empty_password() -> None:
    with pytest.raises(ValueError, match="ISSUEBOT_WEB_PASSWORD"):
        create_app(Harness().database, password="")


# --- the gate through the app -------------------------------------------------------------------


@pytest.mark.parametrize("path", GATED_PATHS)
def test_every_route_challenges_without_the_credential(h: Harness, path: str) -> None:
    response = h.anonymous.get(path)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == CHALLENGE
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    # No transcript, no repository name, no redirect: the body is the challenge and nothing else.
    assert "claude-opus-5" not in response.text
    assert "example/repo" not in response.text


def test_the_challenge_is_json_under_the_api_and_a_page_elsewhere(h: Harness) -> None:
    api = h.anonymous.get(f"{API}/state")
    assert api.headers["content-type"].startswith("application/json")
    assert api.json() == {
        "error": {"code": "unauthorized", "message": "the dashboard needs its password"}
    }
    page = h.anonymous.get(f"{BASE}/")
    assert page.headers["content-type"].startswith("text/html")
    assert "401" in page.text


@pytest.mark.parametrize("path", GATED_PATHS[:-1])
def test_every_route_answers_with_the_credential(h: Harness, path: str) -> None:
    response = h.client.get(path, follow_redirects=False)
    assert response.status_code in (200, 302), (path, response.status_code)


def test_the_username_is_not_read(h: Harness) -> None:
    for username in ("", "alice", "issuebot"):
        response = h.anonymous.get(f"{API}/state", headers=basic_auth(PASSWORD, username))
        assert response.status_code == 200, username


@pytest.mark.parametrize(
    "headers",
    [
        basic_auth("wrong"),
        basic_auth(PASSWORD[:-1]),
        basic_auth(PASSWORD + "x"),
        {"Authorization": "Bearer " + PASSWORD},
        {"Authorization": "Basic not-base64!"},
    ],
)
def test_a_wrong_or_malformed_credential_is_refused(h: Harness, headers: dict[str, str]) -> None:
    response = h.anonymous.get(f"{RAW}/stream", headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == CHALLENGE


def test_a_wrong_credential_is_logged_without_its_value(h: Harness) -> None:
    with structlog.testing.capture_logs() as logs:
        h.anonymous.get(f"{API}/state", headers=basic_auth("guess-1234567890"))
        # A request with no credential at all is a browser's first visit: not logged.
        h.anonymous.get(f"{API}/state")
    rejected = [entry for entry in logs if entry["event"] == "web_auth_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["path"] == f"{API}/state"
    assert "guess-1234567890" not in repr(rejected)


def test_static_files_are_open(h: Harness) -> None:
    assert h.anonymous.get("/static/app.css").status_code == 200
    assert h.anonymous.get("/static/vendor/htmx.min.js").status_code == 200


# --- /healthz: liveness for anyone, the workers for the credential -----------------------------


def test_healthz_anonymous_is_liveness_alone(h: Harness) -> None:
    response = h.anonymous.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
    assert "WWW-Authenticate" not in response.headers


def test_healthz_authenticated_names_the_workers(h: Harness) -> None:
    body = h.client.get("/healthz").json()
    assert body["status"] == "ok" and "workers" in body and "example/repo" in body["workers"]


def test_healthz_anonymous_hides_the_database_error(h: Harness) -> None:
    from issuebot.db import StoreUnavailableError

    h.database.queries_error = StoreUnavailableError("cannot connect: db.internal refused")
    anonymous = h.anonymous.get("/healthz")
    assert anonymous.status_code == 503
    assert anonymous.json() == {"status": "unavailable", "database": "unavailable"}
    authenticated = h.client.get("/healthz")
    assert authenticated.status_code == 503
    assert authenticated.json()["error"] == "cannot connect: db.internal refused"


def test_healthz_with_a_wrong_credential_is_a_challenge_not_liveness(h: Harness) -> None:
    response = h.anonymous.get("/healthz", headers=basic_auth("wrong"))
    assert response.status_code == 401


# --- the refresh route: credential and proof ---------------------------------------------------


def test_refresh_needs_the_credential_before_the_proof(h: Harness) -> None:
    assert h.anonymous.post(f"{API}/refresh", headers=REFRESH_HEADERS).status_code == 401
    assert h.database.notified_repos == []


def test_refresh_refuses_a_cross_site_form_even_with_the_credential(h: Harness) -> None:
    # What a browser sends for a form on another origin: the cached credential, a form body,
    # no custom header. With and without the provenance header, since older clients lack it.
    form = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://evil.test"}
    plain = h.client.post(f"{API}/refresh", headers=form, content="x=1")
    assert plain.status_code == 403
    assert plain.json()["error"]["code"] == "forbidden"
    assert PROOF_HEADER in plain.json()["error"]["message"]
    named = h.client.post(
        f"{API}/refresh", headers={**form, PROVENANCE_HEADER: "cross-site"}, content="x=1"
    )
    assert named.status_code == 403
    # A cross-site script that adds the header would face a preflight the app does not answer.
    forged = h.client.post(
        f"{API}/refresh", headers={**REFRESH_HEADERS, PROVENANCE_HEADER: "cross-site"}
    )
    assert forged.status_code == 403
    assert h.database.notified_repos == []
    assert h.anonymous.options(f"{API}/refresh").status_code in (401, 405)


def test_refresh_accepts_the_button_and_a_shell(h: Harness) -> None:
    button = h.client.post(
        f"{API}/refresh", headers={**REFRESH_HEADERS, PROVENANCE_HEADER: "same-origin"}
    )
    assert button.status_code == 202 and button.json()["queued"] is True
    assert h.database.notified_repos == ["example/repo"]
    h.clock.mono += 60
    shell = h.client.post(f"{API}/refresh", headers=REFRESH_HEADERS)
    assert shell.status_code == 202
