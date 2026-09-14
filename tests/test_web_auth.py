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
from issuebot.web.app import LIVENESS_CACHE_S
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


@pytest.mark.parametrize(
    "path",
    [
        f"/static/..{RAW}/stream",
        f"/static/..{API}/issues/7",
        "/static/../healthz",
        "/static/../../__init__.py",
    ],
)
def test_a_dot_segment_under_static_reaches_nothing(path: str) -> None:
    """The open prefix cannot be a way around the gate. httpx normalises ``..`` on the client,
    so this drives the ASGI app with the raw path a hand-built request would carry: the mount
    claims the whole prefix ahead of every route and ``StaticFiles`` refuses the traversal."""
    import asyncio

    harness = Harness()
    harness.seed_issue()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(harness.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert start["status"] == 404, (path, start["status"])
    assert b"claude-opus-5" not in body and b"example/repo" not in body


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


def test_healthz_anonymous_opens_one_connection_per_interval(h: Harness) -> None:
    """The exemption inherits the gate's obligation (#106): a flood of anonymous probes is
    answered from the verdict the process holds, and one probe refreshes it once it has aged."""
    for _ in range(50):
        assert h.anonymous.get("/healthz").status_code == 200
    assert h.database.opened == 1
    h.clock.mono += LIVENESS_CACHE_S - 1
    assert h.anonymous.get("/healthz").status_code == 200
    assert h.database.opened == 1
    h.clock.mono += 1
    assert h.anonymous.get("/healthz").status_code == 200
    assert h.database.opened == 2


def test_healthz_anonymous_holds_a_failure_too(h: Harness) -> None:
    """A failure repeated is a connection attempt repeated, so the 503 is held for the same
    interval; the next probe after it sees the recovery."""
    from issuebot.db import StoreUnavailableError

    h.database.queries_error = StoreUnavailableError("cannot connect: db.internal refused")
    for _ in range(20):
        assert h.anonymous.get("/healthz").status_code == 503
    assert h.database.opened == 1
    h.database.queries_error = None
    assert h.anonymous.get("/healthz").status_code == 503
    h.clock.mono += LIVENESS_CACHE_S
    assert h.anonymous.get("/healthz").status_code == 200
    assert h.database.opened == 2


def test_healthz_credential_probes_live_and_refreshes_the_anonymous_answer(h: Harness) -> None:
    """The credential's probe is never cached, and what it sees is the next anonymous answer."""
    from issuebot.db import StoreUnavailableError

    assert h.anonymous.get("/healthz").status_code == 200
    assert h.database.opened == 1
    for _ in range(3):
        assert h.client.get("/healthz").status_code == 200
    assert h.database.opened == 4
    h.database.queries_error = StoreUnavailableError("cannot connect: db.internal refused")
    assert h.client.get("/healthz").status_code == 503
    assert h.database.opened == 5
    # Within the interval, but the credential's probe just saw the outage.
    assert h.anonymous.get("/healthz").status_code == 503
    assert h.database.opened == 5


async def test_liveness_shares_one_probe_between_concurrent_callers() -> None:
    """Every anonymous caller that arrives during a probe waits for that one (#106)."""
    import asyncio

    from issuebot.web.app import _Liveness

    clock = [0.0]
    probes = 0
    release = asyncio.Event()

    async def probe() -> bool:
        nonlocal probes
        probes += 1
        await release.wait()
        return True

    liveness = _Liveness(lambda: clock[0])
    callers = [asyncio.ensure_future(liveness.verdict(probe)) for _ in range(25)]
    await asyncio.sleep(0)
    assert probes == 1
    release.set()
    assert await asyncio.gather(*callers) == [True] * 25
    assert probes == 1
    assert await liveness.verdict(probe) is True
    assert probes == 1
    clock[0] += LIVENESS_CACHE_S
    assert await liveness.verdict(probe) is True
    assert probes == 2


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
    # The preflight a cross-site script would trigger is itself a challenge, never a CORS answer.
    preflight = h.anonymous.options(f"{API}/refresh")
    assert preflight.status_code == 401
    assert "access-control-allow-origin" not in preflight.headers


def test_refresh_accepts_the_button_and_a_shell(h: Harness) -> None:
    button = h.client.post(
        f"{API}/refresh", headers={**REFRESH_HEADERS, PROVENANCE_HEADER: "same-origin"}
    )
    assert button.status_code == 202 and button.json()["queued"] is True
    assert h.database.notified_repos == ["example/repo"]
    h.clock.mono += 60
    shell = h.client.post(f"{API}/refresh", headers=REFRESH_HEADERS)
    assert shell.status_code == 202
