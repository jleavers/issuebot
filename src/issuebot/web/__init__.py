"""The dashboard: a FastAPI app over the Phase 6 database, its view models and templates."""

from issuebot.web.app import CHART_DAYS, JSON_PREFIXES, SECURITY_HEADERS, create_app
from issuebot.web.views import (
    CHART_POLL_S,
    LIVE_POLL_S,
    RECENT_EVENTS_LIMIT,
    REFRESH_MIN_INTERVAL_S,
    RUN_ID_PATTERN,
    STALE_FACTOR,
)

__all__ = [
    "CHART_DAYS",
    "CHART_POLL_S",
    "JSON_PREFIXES",
    "LIVE_POLL_S",
    "RECENT_EVENTS_LIMIT",
    "REFRESH_MIN_INTERVAL_S",
    "RUN_ID_PATTERN",
    "SECURITY_HEADERS",
    "STALE_FACTOR",
    "create_app",
]
