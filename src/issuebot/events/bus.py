"""Synchronous in-process event bus with failure-isolated sinks."""

from collections.abc import Iterable
from typing import Protocol

from issuebot.events.types import Event
from issuebot.log import get_logger


class EventSink(Protocol):
    """A consumer of events. ``handle`` must be quick; sinks doing IO enqueue internally."""

    name: str

    def handle(self, event: Event) -> None: ...


class EventBus:
    """Fans each published event out to every sink; a raising sink never affects the others."""

    def __init__(self, sinks: Iterable[EventSink] = ()) -> None:
        self._sinks: list[EventSink] = []
        self.failures: dict[str, int] = {}
        self._log = get_logger(__name__)
        for sink in sinks:
            self.add_sink(sink)

    @property
    def sinks(self) -> tuple[EventSink, ...]:
        return tuple(self._sinks)

    def add_sink(self, sink: EventSink) -> None:
        if any(existing.name == sink.name for existing in self._sinks):
            raise ValueError(f"sink {sink.name!r} is already registered")
        self._sinks.append(sink)

    def remove_sink(self, name: str) -> None:
        self._sinks = [sink for sink in self._sinks if sink.name != name]

    def publish(self, event: Event) -> None:
        for sink in list(self._sinks):
            try:
                sink.handle(event)
            except Exception:
                self.failures[sink.name] = self.failures.get(sink.name, 0) + 1
                self._log.exception("event_sink_failed", sink=sink.name, event_kind=event.kind)
