"""The FastAPI app: the JSON API, the health check, error envelopes and response headers.

Every request that reads opens one connection through ``Database.queries()`` and closes it when
the response is built. The app never writes to a table; its one write is ``NOTIFY``.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from issuebot.config import Settings
from issuebot.db import MAX_WINDOW_DAYS, Database, DatabaseError
from issuebot.log import get_logger
from issuebot.web.views import (
    RECENT_EVENTS_LIMIT,
    REFRESH_MIN_INTERVAL_S,
    iso,
    issue_document,
    snapshot_age_s,
    state_document,
    stats_document,
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
_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith(JSON_PREFIXES)


def envelope(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


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
    app.state.settings = settings

    def error_response(request: Request, status: int, code: str, message: str) -> Response:
        if _wants_json(request):
            return envelope(status, code, message)
        return PlainTextResponse(f"{status} {message}", status_code=status)

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
            }
        )

    return app
