"""Structured logging: structlog to stderr, JSON by default, contextvars for run context."""

import logging
import sys
from typing import Literal, TextIO

import structlog

LogFormat = Literal["json", "console"]


def configure_logging(
    *,
    level: str = "INFO",
    fmt: LogFormat = "json",
    stream: TextIO = sys.stderr,
) -> None:
    """Configure structlog and the standard library to emit one line per event to ``stream``.

    Safe to call repeatedly (tests reconfigure with fresh streams).
    """
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
    ]
    renderer: structlog.types.Processor
    if fmt == "json":
        shared.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=stream.isatty())

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name) if name else structlog.get_logger()


def bind_issue_context(*, issue_number: int, issue_identifier: str) -> None:
    structlog.contextvars.bind_contextvars(
        issue_number=issue_number, issue_identifier=issue_identifier
    )


def bind_session_context(*, session_id: str) -> None:
    structlog.contextvars.bind_contextvars(session_id=session_id)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
