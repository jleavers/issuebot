"""Sink that writes every event to the structured log."""

import structlog

from issuebot.events.types import Event
from issuebot.log import get_logger


class LogSink:
    name = "log"

    def __init__(self, logger: structlog.stdlib.BoundLogger | None = None) -> None:
        self._log = logger or get_logger("issuebot.events")

    def handle(self, event: Event) -> None:
        data = event.to_dict()
        kind = data.pop("kind")
        self._log.info(kind, **data)
