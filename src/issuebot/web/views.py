"""Pure builders for what the pages and the API show: no I/O, no HTML, JSON-safe values only."""

import re
from dataclasses import fields
from datetime import UTC, date, datetime
from typing import Any, Literal

from issuebot.config import GitHubLabels
from issuebot.db.queries import (
    MAX_WINDOW_DAYS,
    DailyPoint,
    EventRow,
    IssueRow,
    RunRow,
    RunTotals,
    SnapshotRow,
    TurnSummaryRow,
)
from issuebot.github import StateLabel

LIVE_POLL_S = 10
CHART_POLL_S = 60
STALE_FACTOR = 3
REFRESH_MIN_INTERVAL_S = 5.0
RECENT_EVENTS_LIMIT = 50
RUN_ID_PATTERN = r"^\d{8}T\d{6}Z-[0-9a-f]{6}$"
DEFAULT_WINDOW_DAYS = 7
DEFAULT_POLL_INTERVAL_MS = 30_000

WorkerStatus = Literal["ok", "held", "stale", "none"]

_WINDOW = re.compile(r"^(\d+)d$")
_WORKER_KEYS = (
    "tick_count",
    "last_tick_at",
    "poll_interval_ms",
    "max_concurrent_agents",
    "workflow_path",
    "config_valid",
    "config_error",
)
_TOTAL_KEYS = ("input_tokens", "output_tokens", "total_tokens")
_COUNTER_KEYS = ("runs_started", "runs_ended", "issues_completed", "issues_cancelled", "blocked")


def iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def snapshot_age_s(row: SnapshotRow, now: datetime) -> float:
    return round(max((now - row.written_at).total_seconds(), 0.0), 3)


def poll_interval_s(row: SnapshotRow) -> float:
    """The worker's poll interval from its snapshot; 30 s when the snapshot lacks it."""
    value = row.data.get("poll_interval_ms")
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value) / 1000
    return DEFAULT_POLL_INTERVAL_MS / 1000


def dispatch_hold(row: SnapshotRow | None) -> dict[str, Any] | None:
    """The snapshot's dispatch hold when it names a reason, else None (the data is JSON).

    A worker holding dispatch keeps ticking, so nothing else in the snapshot says it has
    stopped claiming issues (#29).
    """
    if row is None:
        return None
    hold = row.data.get("dispatch_hold")
    if not isinstance(hold, dict):
        return None
    reason = hold.get("reason")
    if not isinstance(reason, str) or not reason:
        return None
    kind = hold.get("kind")
    return {
        "kind": kind if isinstance(kind, str) and kind else "unknown",
        "reason": reason,
        "since": hold.get("since"),
    }


def worker_status(row: SnapshotRow | None, now: datetime) -> WorkerStatus:
    """``none`` without a snapshot, ``stale`` past STALE_FACTOR poll intervals, ``held``
    while the worker ticks without claiming, else ``ok``.
    """
    if row is None:
        return "none"
    if snapshot_age_s(row, now) > STALE_FACTOR * poll_interval_s(row):
        return "stale"
    return "held" if dispatch_hold(row) is not None else "ok"


def window_days(text: str | None) -> int | None:
    """``<N>d`` with 1 <= N <= MAX_WINDOW_DAYS as N; the default when absent; None when bad."""
    if not text:
        return DEFAULT_WINDOW_DAYS
    match = _WINDOW.match(text)
    if match is None:
        return None
    days = int(match.group(1))
    return days if 1 <= days <= MAX_WINDOW_DAYS else None


def safe_href(url: object) -> str | None:
    """The URL when it is an https URL, else None (so a template renders no link at all)."""
    return url if isinstance(url, str) and url.startswith("https://") else None


def turn_url(number: int, run_id: str, turn_number: int) -> str:
    return f"/issues/{number}/runs/{run_id}/turns/{turn_number}"


def turn_label(run_id: str, turn_number: int) -> str:
    return f"run {run_id} turn {turn_number}"


# --- template filters -------------------------------------------------------------------------


def age_text(value: object, now: datetime) -> str:
    """``12 s ago``, ``3 min ago``, ``2 h ago``, ``4 d ago``; ``-`` for None; other text as is."""
    moment = _datetime(value)
    if moment is None:
        return "-" if value is None else str(value)
    seconds = max(int((now - moment).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def stamp_text(value: object) -> str:
    """A second-precision UTC stamp for a datetime or an ISO 8601 string; ``-`` for None."""
    moment = _datetime(value)
    if moment is None:
        return "-" if value is None else str(value)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def duration_text(value: object) -> str:
    """Milliseconds as ``3m21s``; ``-`` for None."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "-"
    total = int(value) // 1000
    return f"{total // 60}m{total % 60:02d}s"


def money(value: object) -> str:
    return f"${_float(value):.2f}"


def thousands(value: object) -> str:
    return f"{_int(value):,}"


def compact(value: object) -> str:
    """A magnitude at a glance: ``950``, ``1.0K``, ``203K``, ``39.2M``, ``1.2B``.

    The hero's token figures run to ten digits, which no tile that size can hold. The exact
    number stays on the tile as its ``title``; ``thousands`` renders it everywhere else.
    """
    number = _int(value)
    for suffix, size in (("B", 10**9), ("M", 10**6), ("K", 10**3)):
        if abs(number) >= size:
            scaled = number / size
            return f"{scaled:.1f}{suffix}" if abs(scaled) < 100 else f"{scaled:,.0f}{suffix}"
    return f"{number:,}"


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# --- the live region --------------------------------------------------------------------------


def dashboard_context(
    row: SnapshotRow | None,
    groups: dict[str, list[IssueRow]],
    *,
    counts: dict[str, int],
    closed_1d: int,
    closed_7d: int,
    runs_1d: int,
    runs_7d: int,
    totals_1d: RunTotals,
    totals_7d: RunTotals,
    now: datetime,
    labels: GitHubLabels,
) -> dict[str, Any]:
    """What partials/dashboard.html renders: the worker line, hero stats, panels, columns."""
    running = [running_entry(entry) for entry in _entries(row, "running")]
    retrying = [retry_entry(entry) for entry in _entries(row, "retrying")]
    data = row.data if row is not None else {}
    worker: dict[str, Any] = {"status": worker_status(row, now)}
    if row is not None:
        worker.update(
            written_at=iso(row.written_at),
            tick_count=data.get("tick_count"),
            poll_interval_ms=data.get("poll_interval_ms"),
            max_concurrent_agents=data.get("max_concurrent_agents"),
            config_valid=data.get("config_valid"),
            config_error=data.get("config_error"),
            dispatch_hold=dispatch_hold(row),
        )
    names = labels.model_dump()
    # `total` is the whole column (state_counts, uncapped); `rows` is the BOARD_LIMIT the
    # board draws. The header counts the first so it never understates the board, and the
    # difference is what the overflow link offers. `max` because the two reads are separate
    # queries: a column that grew between them must not count backwards.
    columns = []
    for role in StateLabel:
        rows = groups.get(role.value, [])
        total = counts.get(role.value, len(rows))
        columns.append(
            {
                "role": role.value,
                "label": names[role.value],
                "rows": rows,
                "total": total,
                "overflow": max(total - len(rows), 0),
            }
        )
    return {
        "unavailable": None,
        "worker": worker,
        "hero": {
            "closed_1d": closed_1d,
            "closed_7d": closed_7d,
            "runs_1d": runs_1d,
            "runs_7d": runs_7d,
            "running": len(running),
            "retrying": len(retrying),
            "cost_1d": totals_1d.cost_usd,
            "cost_7d": totals_7d.cost_usd,
            "tokens_1d": totals_1d.total_tokens,
            "tokens_7d": totals_7d.total_tokens,
        },
        "running": running,
        "retrying": retrying,
        "columns": columns,
    }


# --- the issues list page ---------------------------------------------------------------------


def is_board_state(state: str) -> bool:
    """Whether ``state`` names one of the board's columns (so the list page will show it)."""
    return state in {role.value for role in StateLabel}


def issue_filters(
    state: str | None, counts: dict[str, int], labels: GitHubLabels
) -> list[dict[str, Any]]:
    """The list page's filter row: "all" first, then one per column, each with its count.

    ``counts`` is ``state_counts``, the same uncapped read the board's headers use, so a
    filter and the column header it came from never disagree. "all" sums the five rather
    than counting rows, because the page itself stops at ``ISSUE_LIST_LIMIT``.
    """
    names = labels.model_dump()
    filters: list[dict[str, Any]] = [
        {
            "role": None,
            "label": "all",
            "href": "/issues",
            "current": state is None,
            "total": sum(counts.get(role.value, 0) for role in StateLabel),
        }
    ]
    for role in StateLabel:
        filters.append(
            {
                "role": role.value,
                "label": names[role.value],
                "href": f"/issues?state={role.value}",
                "current": state == role.value,
                "total": counts.get(role.value, 0),
            }
        )
    return filters


# --- the API documents ----------------------------------------------------------------------


def running_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """One RunningRow (as the worker serialised it) in the Symphony shape."""
    number = entry.get("issue_number")
    return {
        "issue_id": str(number) if number is not None else None,
        "issue_identifier": entry.get("identifier"),
        "issue_number": number,
        "issue_url": entry.get("url"),
        "title": entry.get("title"),
        "state": entry.get("state"),
        "run_id": entry.get("run_id"),
        "session_id": entry.get("session_id"),
        "attempt": entry.get("attempt"),
        "rework": entry.get("rework"),
        "resumed": entry.get("resumed"),
        "turn_count": entry.get("turns"),
        "last_event": entry.get("last_event"),
        "started_at": entry.get("started_at"),
        "last_event_at": entry.get("last_activity_at"),
        "stop_cause": entry.get("stop_cause"),
    }


def retry_entry(entry: dict[str, Any]) -> dict[str, Any]:
    number = entry.get("issue_number")
    return {
        "issue_id": str(number) if number is not None else None,
        "issue_identifier": entry.get("identifier"),
        "issue_number": number,
        "issue_url": entry.get("url"),
        "attempt": entry.get("attempt"),
        "kind": entry.get("kind"),
        "due_at": entry.get("due_at"),
        "error": entry.get("error"),
    }


def _entries(row: SnapshotRow | None, key: str) -> list[dict[str, Any]]:
    if row is None:
        return []
    value = row.data.get(key)
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def state_document(row: SnapshotRow | None, now: datetime) -> dict[str, Any]:
    """GET /api/v1/state: the worker's last snapshot reshaped; empty, not missing, without one."""
    running = [running_entry(entry) for entry in _entries(row, "running")]
    retrying = [retry_entry(entry) for entry in _entries(row, "retrying")]
    data = row.data if row is not None else {}
    totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    counters = data.get("counters") if isinstance(data.get("counters"), dict) else {}
    worker = None
    if row is not None:
        status = worker_status(row, now)
        worker = {key: data.get(key) for key in _WORKER_KEYS}
        worker["dispatch_hold"] = dispatch_hold(row)
        worker["status"] = status
        worker["stale"] = status == "stale"
    return {
        "generated_at": iso(row.at) if row is not None else None,
        "written_at": iso(row.written_at) if row is not None else None,
        "snapshot_age_s": snapshot_age_s(row, now) if row is not None else None,
        "worker": worker,
        "counts": {"running": len(running), "retrying": len(retrying)},
        "running": running,
        "retrying": retrying,
        "claude_totals": {
            **{key: _int(totals.get(key)) for key in _TOTAL_KEYS},
            "cost_usd": _float(totals.get("cost_usd")),
            "seconds_running": _float(totals.get("seconds_running")),
        },
        "counters": {key: _int(counters.get(key)) for key in _COUNTER_KEYS},
    }


def stats_document(
    days: int, closed: int, runs: int, counts: dict[str, int], series: list[DailyPoint]
) -> dict[str, Any]:
    """GET /api/v1/stats."""
    return {
        "window": f"{days}d",
        "days": days,
        "closed": closed,
        "runs": runs,
        "by_state": dict(counts),
        "series": [
            {"day": iso(point.day), "closed": point.closed, "runs": point.runs} for point in series
        ],
    }


def issue_document(
    issue: IssueRow,
    runs: list[RunRow],
    turns: list[TurnSummaryRow],
    events: list[EventRow],
    snapshot: SnapshotRow | None,
) -> dict[str, Any]:
    """GET /api/v1/issues/<n>: the row, the snapshot's entries, runs with captured turns, events.

    ``runs[].turns`` stays the run's turn count (a ``runs`` column); the captured turn rows
    are ``runs[].captured_turns``.
    """
    running = next(
        (
            entry
            for entry in _entries(snapshot, "running")
            if entry.get("issue_number") == issue.number
        ),
        None,
    )
    retry = next(
        (
            entry
            for entry in _entries(snapshot, "retrying")
            if entry.get("issue_number") == issue.number
        ),
        None,
    )
    run_documents = []
    logs = []
    for run in runs:
        run_turns = []
        for turn in turns:
            if turn.run_id != run.run_id:
                continue
            url = turn_url(issue.number, run.run_id, turn.turn_number)
            run_turns.append({**row_dict(turn), "url": url})
            logs.append(
                {
                    "run_id": run.run_id,
                    "turn_number": turn.turn_number,
                    "label": turn_label(run.run_id, turn.turn_number),
                    "url": url,
                }
            )
        run_documents.append({**row_dict(run), "captured_turns": run_turns})
    return {
        "issue": row_dict(issue),
        "running": running_entry(running) if running is not None else None,
        "retry": retry_entry(retry) if retry is not None else None,
        "runs": run_documents,
        "logs": logs,
        "recent_events": [row_dict(event) for event in events],
    }


def row_dict(row: object) -> dict[str, Any]:
    """A frozen row as a JSON-safe dict: datetimes and dates as ISO 8601 strings."""
    return {f.name: _json_value(getattr(row, f.name)) for f in fields(row)}  # type: ignore[arg-type]


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


# --- one line per event -----------------------------------------------------------------------


def describe_event(event: EventRow) -> str:
    """A one-line description of an event for the issue page; plain text, kind-specific."""
    payload = event.payload
    kind = event.kind
    if kind == "state_changed":
        before = payload.get("from_label") or "unlabelled"
        after = payload.get("to_label") or "unlabelled"
        text = f"{payload.get('actor', '?')} changed {before} to {after}"
        return _with_pr(text, payload.get("pr_url"))
    if kind == "run_started":
        return f"run {payload.get('run_id', '?')} started (attempt {payload.get('attempt', '?')})"
    if kind == "run_ended":
        turns = _int(payload.get("turns"))
        word = "turn" if turns == 1 else "turns"
        text = (
            f"run {payload.get('run_id', '?')} {payload.get('outcome', '?')} after {turns} {word}, "
            f"${_float(payload.get('cost_usd')):.2f}, {_int(payload.get('input_tokens'))} in / "
            f"{_int(payload.get('output_tokens'))} out"
        )
        error = payload.get("error")
        return f"{text}: {error}" if error else text
    if kind == "pr_opened":
        return _with_pr(
            f"pull request #{payload.get('pr_number', '?')} opened", payload.get("pr_url")
        )
    if kind == "blocked":
        return f"blocked: {payload.get('reason', '?')}"
    if kind == "issue_completed":
        if payload.get("resolution") == "no_change":
            return "completed: no change needed"
        return _with_pr("completed", payload.get("pr_url"))
    if kind == "issue_cancelled":
        return f"cancelled: {payload.get('reason', '?')}"
    if kind == "notification_sent":
        return f"{payload.get('channel', '?')} notified about {payload.get('about_kind', '?')}"
    return kind


def _with_pr(text: str, pr_url: object) -> str:
    return f"{text} ({pr_url})" if pr_url else text
