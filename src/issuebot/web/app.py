"""The FastAPI app: pages, the live partial, the JSON API, the health check, static files.

Every request that reads opens one connection through ``Database.queries()`` and closes it when
the response is built. The app never writes to a table; its one write is ``NOTIFY``. Templates
render with autoescape on and ``StrictUndefined``; every response carries the security headers.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, PackageLoader, StrictUndefined
from starlette.exceptions import HTTPException as StarletteHTTPException

from issuebot.config import Settings
from issuebot.db import MAX_WINDOW_DAYS, PROMPT_LIMIT, STDERR_LIMIT, Database, DatabaseError
from issuebot.db.queries import IssueRow, RunRow, TurnRow, TurnSummaryRow
from issuebot.log import get_logger
from issuebot.web.transcript import parse_transcript
from issuebot.web.views import (
    CHART_POLL_S,
    LIVE_POLL_S,
    RECENT_EVENTS_LIMIT,
    REFRESH_MIN_INTERVAL_S,
    RUN_ID_PATTERN,
    age_text,
    compact,
    dashboard_context,
    describe_event,
    dispatch_hold,
    duration_text,
    iso,
    issue_document,
    money,
    safe_href,
    snapshot_age_s,
    stamp_text,
    state_document,
    stats_document,
    thousands,
    turn_url,
    window_days,
    worker_status,
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
_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}
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


def create_app(
    database: Database,
    settings: Settings,
    *,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    """The dashboard app over ``database``; ``clock`` and ``now`` are seams for tests."""
    app = FastAPI(title="issuebot", docs_url=None, redoc_url=None, openapi_url=None)
    log = get_logger(__name__)
    refresh = _Refresh(clock)
    env = template_environment()
    labels = settings.github.labels
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")

    def render(name: str, *, status_code: int = 200, **context: Any) -> HTMLResponse:
        text = env.get_template(name).render(
            repo=settings.github.repo,
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

    @app.middleware("http")
    async def add_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

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

    async def live_context() -> dict[str, Any]:
        async with database.queries() as queries:
            row = await queries.snapshot()
            groups = await queries.issues_by_state()
            closed_1d = await queries.closed_count(timedelta(days=1))
            closed_7d = await queries.closed_count(timedelta(days=7))
            runs_1d = await queries.runs_count(timedelta(days=1))
            runs_7d = await queries.runs_count(timedelta(days=7))
            totals_1d = await queries.run_totals(timedelta(days=1))
            totals_7d = await queries.run_totals(timedelta(days=7))
        return dashboard_context(
            row,
            groups,
            closed_1d=closed_1d,
            closed_7d=closed_7d,
            runs_1d=runs_1d,
            runs_7d=runs_7d,
            totals_1d=totals_1d,
            totals_7d=totals_7d,
            now=now(),
            labels=labels,
        )

    async def load_turn(
        number: int, run_id: str, turn_number: int
    ) -> tuple[IssueRow, RunRow, TurnRow]:
        async with database.queries() as queries:
            issue = await queries.issue(number)
            runs = await queries.runs_for_issue(number) if issue is not None else []
            run = next((candidate for candidate in runs if candidate.run_id == run_id), None)
            turn = await queries.turn(run_id, turn_number) if run is not None else None
        if issue is None or run is None or turn is None:
            raise HTTPException(404, f"issue #{number} has no run {run_id} turn {turn_number}")
        return issue, run, turn

    @app.get("/")
    async def index() -> HTMLResponse:
        return render("index.html", live=await live_context())

    @app.get("/partials/dashboard")
    async def partial_dashboard() -> HTMLResponse:
        try:
            live = await live_context()
        except DatabaseError as exc:
            log.warning("web_database_error", path="/partials/dashboard", error=exc.message)
            live = {"unavailable": exc.message}
            return render("partials/dashboard.html", live=live, status_code=503)
        return render("partials/dashboard.html", live=live)

    @app.get("/issues/{number}")
    async def issue_page(number: int) -> HTMLResponse:
        async with database.queries() as queries:
            issue = await queries.issue(number)
            if issue is None:
                raise HTTPException(404, f"issue #{number} is not known")
            runs = await queries.runs_for_issue(number)
            turns = await queries.turn_summaries_for_issue(number)
            events = await queries.events_for_issue(number, RECENT_EVENTS_LIMIT)
            snapshot = await queries.snapshot()
        turns_by_run: dict[str, list[TurnSummaryRow]] = {}
        for turn in turns:
            turns_by_run.setdefault(turn.run_id, []).append(turn)
        document = issue_document(issue, runs, turns, events, snapshot)
        return render(
            "issue.html",
            issue=issue,
            runs=runs,
            turns_by_run=turns_by_run,
            events=[(event, describe_event(event)) for event in events],
            running=document["running"],
            retry=document["retry"],
        )

    @app.get("/issues/{number}/runs/{run_id}/turns/{turn_number}")
    async def turn_page(
        number: int, turn_number: int, run_id: str = PathParam(pattern=RUN_ID_PATTERN)
    ) -> HTMLResponse:
        issue, run, turn = await load_turn(number, run_id, turn_number)
        return render(
            "turn.html",
            issue=issue,
            run=run,
            turn=turn,
            transcript=parse_transcript(turn.stream),
            raw_url=turn_url(number, run_id, turn_number),
            prompt_cut=turn.prompt_bytes > PROMPT_LIMIT,
            stderr_cut=turn.stderr_bytes > STDERR_LIMIT,
        )

    @app.get("/issues/{number}/runs/{run_id}/turns/{turn_number}/{part}")
    async def turn_raw(
        number: int,
        turn_number: int,
        run_id: str = PathParam(pattern=RUN_ID_PATTERN),
        part: str = PathParam(pattern=RAW_PART_PATTERN),
    ) -> PlainTextResponse:
        _issue, _run, turn = await load_turn(number, run_id, turn_number)
        filename = f"{run_id}-turn-{turn_number}.{_RAW_EXTENSIONS[part]}"
        headers = {"Content-Disposition": f'inline; filename="{filename}"'}
        return PlainTextResponse(getattr(turn, part), headers=headers)

    # --- the JSON API -----------------------------------------------------------------------

    @app.get("/api/v1/state")
    async def api_state() -> JSONResponse:
        async with database.queries() as queries:
            row = await queries.snapshot()
        return JSONResponse(state_document(row, now()))

    @app.get("/api/v1/issues/{number}")
    async def api_issue(number: int) -> JSONResponse:
        async with database.queries() as queries:
            issue = await queries.issue(number)
            if issue is None:
                return envelope(404, "unknown_issue", f"issue #{number} is not known")
            runs = await queries.runs_for_issue(number)
            turns = await queries.turn_summaries_for_issue(number)
            events = await queries.events_for_issue(number, RECENT_EVENTS_LIMIT)
            snapshot = await queries.snapshot()
        return JSONResponse(issue_document(issue, runs, turns, events, snapshot))

    @app.get("/api/v1/stats")
    async def api_stats(window: str | None = None) -> JSONResponse:
        days = window_days(window)
        if days is None:
            message = f"window must be <N>d with 1 <= N <= {MAX_WINDOW_DAYS}"
            return envelope(400, "invalid_window", message)
        async with database.queries() as queries:
            closed = await queries.closed_count(timedelta(days=days))
            runs = await queries.runs_count(timedelta(days=days))
            counts = await queries.state_counts()
            series = await queries.daily_series(days)
        return JSONResponse(stats_document(days, closed, runs, counts, series))

    @app.post("/api/v1/refresh")
    async def api_refresh() -> JSONResponse:
        coalesced = refresh.coalesced()
        if not coalesced:
            await database.notify_refresh()
            refresh.sent()
            log.info("web_refresh_requested")
        body: dict[str, Any] = {
            "queued": not coalesced,
            "coalesced": coalesced,
            "requested_at": iso(now()),
            "operations": ["poll", "reconcile"],
        }
        return JSONResponse(body, status_code=202)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        try:
            async with database.queries() as queries:
                row = await queries.snapshot()
        except DatabaseError as exc:
            body = {"status": "unavailable", "database": "unavailable", "error": exc.message}
            return JSONResponse(body, status_code=503)
        current = now()
        return JSONResponse(
            {
                "status": "ok",
                "database": "ok",
                "snapshot_at": iso(row.at) if row is not None else None,
                "snapshot_age_s": snapshot_age_s(row, current) if row is not None else None,
                "worker": worker_status(row, current),
                "dispatch_hold": dispatch_hold(row),
            }
        )

    return app
