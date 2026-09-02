"""Tests for structured logging configuration."""

import io
import json
import logging

import pytest

from issuebot.log import (
    bind_issue_context,
    bind_session_context,
    clear_context,
    configure_logging,
    get_logger,
)


@pytest.fixture(autouse=True)
def _reset_context() -> None:
    clear_context()
    yield
    clear_context()


def _configure(fmt: str = "json", level: str = "INFO") -> io.StringIO:
    stream = io.StringIO()
    configure_logging(level=level, fmt=fmt, stream=stream)  # type: ignore[arg-type]
    return stream


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_json_line_has_fixed_keys() -> None:
    stream = _configure()
    get_logger("issuebot.test").info("hello", answer=42)
    (record,) = _lines(stream)
    assert record["event"] == "hello"
    assert record["level"] == "info"
    assert record["logger"] == "issuebot.test"
    assert record["answer"] == 42
    assert str(record["timestamp"]).endswith("Z")
    assert list(record.keys())[:4] == ["timestamp", "level", "logger", "event"]


def test_bound_issue_and_session_context_appear() -> None:
    stream = _configure()
    bind_issue_context(issue_number=7, issue_identifier="issuebot-7")
    bind_session_context(session_id="abc")
    get_logger().info("working")
    (record,) = _lines(stream)
    assert record["issue_number"] == 7
    assert record["issue_identifier"] == "issuebot-7"
    assert record["session_id"] == "abc"


def test_clear_context_removes_bound_fields() -> None:
    stream = _configure()
    bind_session_context(session_id="abc")
    clear_context()
    get_logger().info("after")
    (record,) = _lines(stream)
    assert "session_id" not in record


def test_stdlib_logging_uses_same_renderer() -> None:
    stream = _configure()
    logging.getLogger("third_party").warning("careful %s", "now")
    (record,) = _lines(stream)
    assert record["event"] == "careful now"
    assert record["level"] == "warning"
    assert record["logger"] == "third_party"


def test_level_filters_below_threshold() -> None:
    stream = _configure(level="WARNING")
    get_logger().info("dropped")
    get_logger().warning("kept")
    records = _lines(stream)
    assert [r["event"] for r in records] == ["kept"]


def test_console_format_renders_without_error() -> None:
    stream = _configure(fmt="console")
    get_logger().info("console line", key="value")
    output = stream.getvalue()
    assert "console line" in output
    assert "key=value" in output
