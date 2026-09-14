"""The FastAPI app: pages, the live partial, the JSON API, the health check, static files.

Every page and JSON route lives under a repository prefix -- ``/r/{owner}/{name}`` for the
pages, ``/api/v1/repos/{owner}/{name}`` for the JSON -- because one database now holds every
worker's rows. ``/`` redirects to the repository the browser last picked (a cookie) or the
first registered one, and the header's dropdown is how a reader moves between them.

Every request that reads opens one connection through ``Database.queries()`` and closes it when
the response is built. The app never writes to a table; its one write is ``NOTIFY``. Templates
render with autoescape on and ``StrictUndefined``; every response carries the security headers,
the one an unhandled exception raises included (#106): the layer that adds them decorates the
``send`` channel rather than the response the next layer returns, and answers the exception
itself through that channel before re-raising it, so the next such exception is covered before
anyone finds it.

Every request but the static files carries the credential (#73, ``issuebot.web.auth``): one
gate, ahead of routing, so the pages, the JSON API, the raw turn parts, the live partial and a
path that matches nothing all answer 401 with the ``Basic`` challenge until it does. ``/healthz``
is the one route with an anonymous answer, and it is liveness alone: the database up or not,
no repository named, so compose's healthcheck and an uptime monitor need no secret. That
exemption inherits the gate's obligation (#106): the anonymous answer comes from the verdict the
process already holds, refreshed by at most one probe per ``LIVENESS_CACHE_S``, so a caller with
no credential cannot open a connection per request against the hub cluster's backends. The
refresh route asks for one thing more, a proof a cross-site page cannot produce.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, PackageLoader, StrictUndefined
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Send
from starlette.types import Scope as ASGIScope

from issuebot.config import GitHubLabels
from issuebot.db import (
    ISSUE_LIST_LIMIT,
    MAX_WINDOW_DAYS,
    PROMPT_LIMIT,
    STDERR_LIMIT,
    Database,
    DatabaseError,
    Queries,
    RepoQueries,
)
from issuebot.db.queries import IssueRow, RepoRow, RunRow, TurnRow, TurnSummaryRow
from issuebot.log import get_logger
from issuebot.web.auth import CHALLENGE, credential_matches, presented_password, refresh_refusal
from issuebot.web.transcript import parse_transcript
from issuebot.web.views import (
    CHART_POLL_S,
    LIVE_POLL_S,
    RECENT_EVENTS_LIMIT,
    REFRESH_MIN_INTERVAL_S,
    REPO_COOKIE,
    RUN_ID_PATTERN,
    RepoContext,
    age_text,
    compact,
    dashboard_context,
    describe_event,
    dispatch_hold,
    duration_text,
    is_board_state,
    iso,
    issue_document,
    issue_filters,
    money,
    repo_base,
    repo_context,
    repo_labels,
    repo_options,
    safe_href,
    snapshot_age_s,
    stamp_text,
    state_document,
    stats_document,
    thousands,
    turn_url,
    window_days,
    worker_status,
    worst_status,
)

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
JSON_PREFIXES = ("/api/", "/healthz")
CHART_DAYS = 30
RAW_PART_PATTERN = r"^(prompt|stream|stderr)$"
STATIC_ROOT = files("issuebot.web") / "static"
_HTTP_CODES = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    500: "internal_error",
    503: "unavailable",
}
OPEN_PREFIXES = ("/static/",)
LIVENESS_PATH = "/healthz"
# How long the anonymous ``/healthz`` answer stands before a probe refreshes it: compose asks
# every 30 s, so a verdict this old is still the one it would have got.
LIVENESS_CACHE_S = 10.0
_RAW_EXTENSIONS = {"prompt": "md", "stream": "jsonl", "stderr": "log"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith(JSON_PREFIXES)


def envelope(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def template_environment() -> Environment:
    """The package's templates with autoescape, StrictUndefined and the display filters."""
    env = Environment(
        loader=PackageLoader("issuebot.web", "templates"),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["href"] = safe_href
    env.filters["age"] = age_text
    env.filters["stamp"] = stamp_text
    env.filters["duration"] = duration_text
    env.filters["compact"] = compact
    env.filters["money"] = money
    env.filters["thousands"] = thousands
    return env


class _Refresh:
    """The refresh throttle: at most one NOTIFY per REFRESH_MIN_INTERVAL_S from this process."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.last: float | None = None

    def coalesced(self) -> bool:
        """True when a NOTIFY went out less than the interval ago (so this one is skipped)."""
        last = self.last
        return last is not None and self._clock() - last < REFRESH_MIN_INTERVAL_S

    def sent(self) -> None:
        self.last = self._clock()


class _Liveness:
    """The anonymous probe's verdict, held for ``LIVENESS_CACHE_S`` from the last probe (#106).

    ``/healthz`` is the gate's one exemption, and a probe that opened a connection per request
    handed the scarcest shared resource -- the hub cluster's backends, one fork and one
    authentication each, since ``Database`` keeps no pool -- to any caller with no credential.
    So the anonymous branch answers from the verdict the process already holds, probes only once
    that has aged out, and while a probe is in flight every other anonymous caller waits for
    that one rather than opening its own: at most one connection per interval, whatever the
    flood. Both verdicts are held, since a failure repeated is a connection attempt repeated.
    The authenticated branch keeps its live probe and records what it saw.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._ok: bool | None = None
        self._at: float | None = None

    def held(self) -> bool | None:
        """The verdict while it is fresh, else None."""
        if self._at is None or self._clock() - self._at >= LIVENESS_CACHE_S:
            return None
        return self._ok

    def record(self, ok: bool) -> None:
        self._ok = ok
        self._at = self._clock()

    async def verdict(self, probe: Callable[[], Awaitable[bool]]) -> bool:
        """The held verdict, or one ``probe`` shared by every caller that arrives during it."""
        held = self.held()
        if held is not None:
            return held
        async with self._lock:
            held = self.held()
            if held is not None:
                return held
            ok = await probe()
            self.record(ok)
            return ok


class _SecureExit:
    """ASGI middleware: the security headers on every response, the raised ones included (#106).

    Starlette's ``ServerErrorMiddleware`` sits outside every user middleware and answers an
    unhandled exception by itself, so a ``BaseHTTPMiddleware`` that decorated the response
    ``call_next`` returned never saw that 500, and it left with no CSP, no ``nosniff``, no
    ``Referrer-Policy`` and no ``X-Frame-Options``. This layer decorates the ``send`` channel
    instead, so every ``http.response.start`` that passes it carries the headers, and when the
    app raises before one has, it answers through the same channel with the envelope or page
    the other errors get -- then re-raises, so the exception still reaches uvicorn's log and a
    test client that expects it. ``on_error`` builds that response; should it raise too, a
    plain 500 goes out with the headers rather than nothing at all.
    """

    def __init__(self, app: ASGIApp, *, on_error: Callable[[Request, Exception], Response]) -> None:
        self.app = app
        self.on_error = on_error

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception as exc:
            if not started:
                try:
                    response = self.on_error(Request(scope), exc)
                except Exception:
                    response = PlainTextResponse("internal server error", status_code=500)
                await response(scope, receive, send_with_headers)
            raise


@dataclass(frozen=True, slots=True)
class Scope:
    """What a prefixed request is for: the repository, its labels, its siblings, its reads."""

    repo: RepoContext
    labels: GitHubLabels
    repos: list[RepoRow]
    queries: RepoQueries


async def load_scope(queries: Queries, owner: str, name: str) -> Scope:
    """The registry row for the prefix, its labels and the dropdown's list.

    404 when the repository is not registered, 503 when its stored labels do not validate.
    It runs before any scoped read, so an unknown prefix costs one query and no more, and a
    row nobody can lay a board out from says so rather than half-rendering a page.
    """
    full = f"{owner}/{name}"
    repos = await queries.repos()
    row = next((candidate for candidate in repos if candidate.repo == full), None)
    if row is None:
        raise HTTPException(404, f"repository {full} is not registered")
    try:
        labels = repo_labels(row)
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc
    return Scope(repo=repo_context(full), labels=labels, repos=repos, queries=queries.scoped(full))


def create_app(
    database: Database,
    *,
    password: str,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    """The dashboard app over ``database``, gated by ``password`` (HTTP Basic, any username);
    ``clock`` and ``now`` are seams for tests. An empty password is refused here rather than
    letting the gate compare against nothing."""
    if not password:
        raise ValueError("the dashboard needs a password: export ISSUEBOT_WEB_PASSWORD")
    app = FastAPI(title="issuebot", docs_url=None, redoc_url=None, openapi_url=None)
    log = get_logger(__name__)
    refreshes: dict[str, _Refresh] = {}
    env = template_environment()
    app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")

    def render(
        name: str,
        *,
        status_code: int = 200,
        repo: RepoContext | None = None,
        repos: Sequence[RepoRow] = (),
        kind: str = "dashboard",
        query: str = "",
        **context: Any,
    ) -> HTMLResponse:
        options = repo_options(list(repos), repo.name, kind, query) if repo is not None else []
        text = env.get_template(name).render(
            repo=repo,
            repo_options=options,
            live_poll_s=LIVE_POLL_S,
            chart_poll_s=CHART_POLL_S,
            chart_days=CHART_DAYS,
            now=now(),
            **context,
        )
        return HTMLResponse(text, status_code=status_code)

    def error_response(request: Request, status: int, code: str, message: str) -> Response:
        if _wants_json(request):
            return envelope(status, code, message)
        return render("error.html", status=status, code=code, message=message, status_code=status)

    def challenge(request: Request) -> Response:
        response = error_response(request, 401, "unauthorized", "the dashboard needs its password")
        response.headers["WWW-Authenticate"] = CHALLENGE
        return response

    # Starlette wraps the last-added middleware outermost, so the gate is added first and the
    # security headers second: a 401 leaves with the same headers as every other response, and
    # so does the 500 an exception raised inside the gate or past it becomes.
    @app.middleware("http")
    async def require_identity(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """The one gate, ahead of routing, so no path reaches a read without the credential.

        A credential that is presented and wrong is a 401 everywhere, ``/healthz`` included:
        the anonymous liveness answer is for a probe that carries nothing, never a cover for a
        guess. A request without one is not logged, since a browser's first visit is one.
        """
        # ``url.path`` is root_path + path; nothing sets a root path here, and under one the
        # two exemptions would stop matching and fail closed rather than open.
        path = request.url.path
        if path.startswith(OPEN_PREFIXES):
            return await call_next(request)
        presented = presented_password(request.headers.get("Authorization"))
        authenticated = credential_matches(presented, password)
        if not authenticated:
            if presented is not None:
                client = request.client.host if request.client is not None else None
                log.warning("web_auth_rejected", path=path, client=client)
            if path != LIVENESS_PATH or presented is not None:
                return challenge(request)
        request.state.authenticated = authenticated
        return await call_next(request)

    def unhandled(request: Request, exc: Exception) -> Response:
        """The 500 for an exception no handler claimed: the type and the path, never the
        message, which is uvicorn's to log with the traceback once the layer re-raises."""
        log.error("web_unhandled_error", path=request.url.path, error=type(exc).__name__)
        return error_response(request, 500, "internal_error", "internal server error")

    app.add_middleware(_SecureExit, on_error=unhandled)

    @app.exception_handler(DatabaseError)
    async def database_error(request: Request, exc: DatabaseError) -> Response:
        log.warning("web_database_error", path=request.url.path, error=exc.message)
        return error_response(request, 503, "database_unavailable", exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        code = _HTTP_CODES.get(exc.status_code, f"http_{exc.status_code}")
        return error_response(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        return error_response(request, 404, "not_found", "not found")

    # --- pages ------------------------------------------------------------------------------

    async def live_context(scope: Scope) -> dict[str, Any]:
        queries = scope.queries
        row = await queries.snapshot()
        groups = await queries.issues_by_state()
        counts = await queries.state_counts()
        closed_1d = await queries.closed_count(timedelta(days=1))
        closed_7d = await queries.closed_count(timedelta(days=7))
        runs_1d = await queries.runs_count(timedelta(days=1))
        runs_7d = await queries.runs_count(timedelta(days=7))
        totals_1d = await queries.run_totals(timedelta(days=1))
        totals_7d = await queries.run_totals(timedelta(days=7))
        return dashboard_context(
            row,
            groups,
            counts=counts,
            closed_1d=closed_1d,
            closed_7d=closed_7d,
            runs_1d=runs_1d,
            runs_7d=runs_7d,
            totals_1d=totals_1d,
            totals_7d=totals_7d,
            now=now(),
            labels=scope.labels,
        )

    async def load_turn(
        scope: Scope, number: int, run_id: str, turn_number: int
    ) -> tuple[IssueRow, RunRow, TurnRow]:
        queries = scope.queries
        issue = await queries.issue(number)
        runs = await queries.runs_for_issue(number) if issue is not None else []
        run = next((candidate for candidate in runs if candidate.run_id == run_id), None)
        turn = await queries.turn(run_id, turn_number) if run is not None else None
        if issue is None or run is None or turn is None:
            raise HTTPException(404, f"issue #{number} has no run {run_id} turn {turn_number}")
        return issue, run, turn

    @app.get("/")
    async def index(request: Request) -> Response:
        """The cookie's repository, or the first registered one; a plain page with none."""
        async with database.queries() as queries:
            repos = await queries.repos()
        if not repos:
            return render("no-repos.html")
        names = {row.repo for row in repos}
        # app.js percent-encodes what it stores, and nothing in the cookie path decodes it:
        # a repository name carries a "/", so the raw value would never match a registration.
        chosen = unquote(request.cookies.get(REPO_COOKIE) or "")
        target = chosen if chosen in names else repos[0].repo
        return RedirectResponse(repo_base(target) + "/", status_code=302)

    @app.get("/r/{owner}/{name}/")
    async def dashboard(owner: str, name: str) -> HTMLResponse:
        async with database.queries() as queries:
            scope = await load_scope(queries, owner, name)
            live = await live_context(scope)
        return render("index.html", repo=scope.repo, repos=scope.repos, live=live)

    @app.get("/r/{owner}/{name}/partials/dashboard")
    async def partial_dashboard(owner: str, name: str) -> HTMLResponse:
        path = f"/r/{owner}/{name}/partials/dashboard"
        try:
            async with database.queries() as queries:
                scope = await load_scope(queries, owner, name)
                live = await live_context(scope)
        except DatabaseError as exc:
            log.warning("web_database_error", path=path, error=exc.message)
            return render(
                "partials/dashboard.html",
                repo=repo_context(f"{owner}/{name}"),
                live={"unavailable": exc.message},
                status_code=503,
            )
        return render("partials/dashboard.html", repo=scope.repo, repos=scope.repos, live=live)

    @app.get("/r/{owner}/{name}/issues")
    async def issues_page(owner: str, name: str, state: str | None = None) -> HTMLResponse:
        """One column in full, or every column: what the board's overflow links point at."""
        if state is not None and not is_board_state(state):
            raise HTTPException(404, f"there is no {state} column")
        async with database.queries() as queries:
            scope = await load_scope(queries, owner, name)
            counts = await scope.queries.state_counts()
            rows = await scope.queries.issues_for_state(state)
        return render(
            "issues.html",
            repo=scope.repo,
            repos=scope.repos,
            kind="issues",
            query=f"state={state}" if state else "",
            rows=rows,
            filters=issue_filters(state, counts, scope.labels, scope.repo.base),
            truncated=len(rows) >= ISSUE_LIST_LIMIT,
            limit=ISSUE_LIST_LIMIT,
        )

    @app.get("/r/{owner}/{name}/issues/{number}")
    async def issue_page(owner: str, name: str, number: int) -> HTMLResponse:
        async with database.queries() as queries:
            scope = await load_scope(queries, owner, name)
            issue = await scope.queries.issue(number)
            if issue is None:
                raise HTTPException(404, f"issue #{number} is not known")
            runs = await scope.queries.runs_for_issue(number)
            turns = await scope.queries.turn_summaries_for_issue(number)
            events = await scope.queries.events_for_issue(number, RECENT_EVENTS_LIMIT)
            snapshot = await scope.queries.snapshot()
        turns_by_run: dict[str, list[TurnSummaryRow]] = {}
        for turn in turns:
            turns_by_run.setdefault(turn.run_id, []).append(turn)
        document = issue_document(issue, runs, turns, events, snapshot, scope.repo.base)
        return render(
            "issue.html",
            repo=scope.repo,
            repos=scope.repos,
            kind="issue",
            issue=issue,
            runs=runs,
            turns_by_run=turns_by_run,
            events=[(event, describe_event(event)) for event in events],
            running=document["running"],
            retry=document["retry"],
        )

    @app.get("/r/{owner}/{name}/issues/{number}/runs/{run_id}/turns/{turn_number}")
    async def turn_page(
        owner: str,
        name: str,
        number: int,
        turn_number: int,
        run_id: str = PathParam(pattern=RUN_ID_PATTERN),
    ) -> HTMLResponse:
        async with database.queries() as queries:
            scope = await load_scope(queries, owner, name)
            issue, run, turn = await load_turn(scope, number, run_id, turn_number)
        return render(
            "turn.html",
            repo=scope.repo,
            repos=scope.repos,
            kind="turn",
            issue=issue,
            run=run,
            turn=turn,
            transcript=parse_transcript(turn.stream),
            raw_url=turn_url(scope.repo.base, number, run_id, turn_number),
            prompt_cut=turn.prompt_bytes > PROMPT_LIMIT,
            stderr_cut=turn.stderr_bytes > STDERR_LIMIT,
        )

    @app.get("/r/{owner}/{name}/issues/{number}/runs/{run_id}/turns/{turn_number}/{part}")
    async def turn_raw(
        owner: str,
        name: str,
        number: int,
        turn_number: int,
        run_id: str = PathParam(pattern=RUN_ID_PATTERN),
        part: str = PathParam(pattern=RAW_PART_PATTERN),
    ) -> PlainTextResponse:
        async with database.queries() as queries:
            scope = await load_scope(queries, owner, name)
            _issue, _run, turn = await load_turn(scope, number, run_id, turn_number)
        filename = f"{run_id}-turn-{turn_number}.{_RAW_EXTENSIONS[part]}"
        headers = {"Content-Disposition": f'inline; filename="{filename}"'}
        return PlainTextResponse(getattr(turn, part), headers=headers)

    # --- the JSON API -----------------------------------------------------------------------

    @app.get("/api/v1/repos")
    async def api_repos() -> JSONResponse:
        """Every registration and its worker: what the dropdown is built from."""
        async with database.queries() as queries:
            repos = await queries.repos()
            snapshots = await queries.snapshots()
        current = now()
        return JSONResponse(
            {
                "repos": [
                    {
                        "repo": row.repo,
                        "url": repo_base(row.repo) + "/",
                        "worker": worker_status(snapshots.get(row.repo), current),
                        "snapshot_at": iso(snapshots[row.repo].at)
                        if row.repo in snapshots
                        else None,
                    }
                    for row in repos
                ]
            }
        )

    @app.get("/api/v1/repos/{owner}/{name}/state")
    async def api_state(owner: str, name: str) -> JSONResponse:
        async with database.queries() as queries:
            scope = await load_scope(queries, owner, name)
            row = await scope.queries.snapshot()
        return JSONResponse(state_document(row, now()))

    @app.get("/api/v1/repos/{owner}/{name}/issues/{number}")
    async def api_issue(owner: str, name: str, number: int) -> JSONResponse:
        async with database.queries() as queries:
            scope = await load_scope(queries, owner, name)
            issue = await scope.queries.issue(number)
            if issue is None:
                return envelope(404, "unknown_issue", f"issue #{number} is not known")
            runs = await scope.queries.runs_for_issue(number)
            turns = await scope.queries.turn_summaries_for_issue(number)
            events = await scope.queries.events_for_issue(number, RECENT_EVENTS_LIMIT)
            snapshot = await scope.queries.snapshot()
        document = issue_document(issue, runs, turns, events, snapshot, scope.repo.base)
        return JSONResponse(document)

    @app.get("/api/v1/repos/{owner}/{name}/stats")
    async def api_stats(owner: str, name: str, window: str | None = None) -> JSONResponse:
        days = window_days(window)
        if days is None:
            message = f"window must be <N>d with 1 <= N <= {MAX_WINDOW_DAYS}"
            return envelope(400, "invalid_window", message)
        async with database.queries() as queries:
            scope = await load_scope(queries, owner, name)
            closed = await scope.queries.closed_count(timedelta(days=days))
            runs = await scope.queries.runs_count(timedelta(days=days))
            counts = await scope.queries.state_counts()
            series = await scope.queries.daily_series(days)
        return JSONResponse(stats_document(days, closed, runs, counts, series))

    @app.post("/api/v1/repos/{owner}/{name}/refresh")
    async def api_refresh(request: Request, owner: str, name: str) -> JSONResponse:
        """NOTIFY this repository's worker; the throttle is per repository, not per process.

        The one write, so the credential is not enough: a browser replays it on a cross-site
        form POST, and the proof is what such a form cannot send (``issuebot.web.auth``).
        """
        refusal = refresh_refusal(request.headers)
        if refusal is not None:
            log.warning("web_refresh_refused", repo=f"{owner}/{name}", reason=refusal)
            return envelope(403, "forbidden", refusal)
        async with database.queries() as queries:
            scope = await load_scope(queries, owner, name)
        refresh = refreshes.setdefault(scope.repo.name, _Refresh(clock))
        coalesced = refresh.coalesced()
        if not coalesced:
            await database.notify_refresh(scope.repo.name)
            refresh.sent()
            log.info("web_refresh_requested", repo=scope.repo.name)
        body: dict[str, Any] = {
            "queued": not coalesced,
            "coalesced": coalesced,
            "requested_at": iso(now()),
            "operations": ["poll", "reconcile"],
        }
        return JSONResponse(body, status_code=202)

    liveness = _Liveness(clock)

    async def probe() -> bool:
        """Opening a connection is the probe; nothing is read."""
        try:
            async with database.queries():
                return True
        except DatabaseError:
            return False

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        """The database, and one entry per registered worker; ``worker`` is the worst of them.

        An anonymous probe gets liveness alone -- ``status`` and ``database`` -- and not the
        workers, the repository names or the error text, which is what the credential is for.
        It gets it from ``liveness``, the verdict this process already holds, so a flood of
        anonymous probes costs one connection per ``LIVENESS_CACHE_S`` and no more (#106); the
        credential's probe is live, and what it sees is the next anonymous answer.
        """
        authenticated = bool(getattr(request.state, "authenticated", False))
        if not authenticated:
            if await liveness.verdict(probe):
                return JSONResponse({"status": "ok", "database": "ok"})
            return JSONResponse(
                {"status": "unavailable", "database": "unavailable"}, status_code=503
            )
        try:
            async with database.queries() as queries:
                repos = await queries.repos()
                snapshots = await queries.snapshots()
        except DatabaseError as exc:
            liveness.record(False)
            body = {"status": "unavailable", "database": "unavailable", "error": exc.message}
            return JSONResponse(body, status_code=503)
        liveness.record(True)
        current = now()
        workers: dict[str, Any] = {}
        for row in repos:
            snapshot = snapshots.get(row.repo)
            workers[row.repo] = {
                "status": worker_status(snapshot, current),
                "snapshot_at": iso(snapshot.at) if snapshot is not None else None,
                "snapshot_age_s": snapshot_age_s(snapshot, current)
                if snapshot is not None
                else None,
                "dispatch_hold": dispatch_hold(snapshot),
            }
        return JSONResponse(
            {
                "status": "ok",
                "database": "ok",
                "worker": worst_status(entry["status"] for entry in workers.values()),
                "workers": workers,
            }
        )

    return app
