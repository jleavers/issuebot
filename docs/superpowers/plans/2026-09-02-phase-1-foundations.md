# Phase 1: Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable, tested, containerised Python 3.14 project that loads and validates `WORKFLOW.md`, logs structured JSON, and publishes typed domain events to sinks, with CI, Dependabot and a compose stack (PostgreSQL 18 plus a worker that runs `validate`).

**Architecture:** One `src`-layout package `issuebot` with four foundations: `issuebot.config` (front-matter parsing → `$VAR`/path resolution → pydantic `Settings`), `issuebot.log` (structlog to stderr, JSON by default, contextvars for issue/session fields), `issuebot.events` (frozen dataclass events, a synchronous `EventBus` that isolates sink failures, a `LogSink`), and `issuebot.cli` (argparse; `validate` runs presence checks and prints a report). No call to `gh` or `claude` is made in this phase.

**Tech Stack:** Python 3.14, uv, hatchling, pydantic ≥ 2.12, PyYAML, structlog, pytest + pytest-asyncio, ruff 0.16.5, pre-commit, Docker (python:3.14-slim, gh from cli.github.com, Claude Code native installer), docker compose with postgres:18, GitHub Actions, Dependabot.

**Spec:** `docs/superpowers/specs/2026-09-02-phase-1-foundations-design.md` (parent: `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`).

## Global Constraints

- Python `>=3.14`; `.python-version` is `3.14`; `uv sync --frozen` everywhere; `uv.lock` committed.
- Package is `src/issuebot`; console script `issuebot = issuebot.cli:main`; module `issuebot.log` (never `issuebot.logging`).
- ruff: `target-version = "py314"`, line length 100, rules `E F I UP B N SIM RUF`; the ruff version in dev deps (`0.16.5`) equals the `ruff-pre-commit` rev (`v0.16.5`).
- All settings models use `extra="forbid"`.
- Logs go to stderr; stdout is CLI output only.
- Tests are hermetic: no network, no Docker, environment passed explicitly or via `monkeypatch`; the autouse `clean_env` fixture removes `GH_TOKEN`, `DATABASE_URL`, `SLACK_WEBHOOK_URL`, `ISSUEBOT_WORKSPACE_ROOT`, `ISSUEBOT_WORKFLOW`, `ISSUEBOT_LOG_LEVEL`, `ISSUEBOT_LOG_FORMAT`.
- Never push to `main`; work on branch `phase-1-foundations`; never run `rm -rf`, `git reset --hard`, `git clean -fd` (AGENTS.md).
- Linux host: Bash, `&&` chaining, `.sh` scripts only (AGENTS.md).
- Commit messages: conventional prefix (`feat:`, `chore:`, `docs:`, `ci:`) and whatever attribution trailer the executing harness requires.
- Run `uv run ruff check . && uv run ruff format --check .` before every commit; fix with `uv run ruff format .` and `uv run ruff check --fix .`.

---

## File map

| Path | Responsibility | Task |
|---|---|---|
| `pyproject.toml`, `uv.lock`, `.python-version` | project metadata, deps, ruff and pytest config | 1 |
| `src/issuebot/__init__.py` | `__version__` | 1 |
| `src/issuebot/__main__.py` | `python -m issuebot` | 1 |
| `src/issuebot/cli.py` | argparse entry point; `validate` | 1, 9 |
| `tests/conftest.py` | `clean_env` autouse fixture | 1 |
| `.pre-commit-config.yaml` | rev bumps | 1 |
| `src/issuebot/log.py` | structlog configuration, context helpers | 2 |
| `src/issuebot/events/types.py` | `Event` dataclasses, `EVENT_KINDS` | 3 |
| `src/issuebot/events/bus.py` | `EventSink` protocol, `EventBus` | 4 |
| `src/issuebot/events/log_sink.py` | `LogSink` | 4 |
| `src/issuebot/events/__init__.py` | re-exports | 3, 4 |
| `src/issuebot/config/errors.py` | `ConfigError` hierarchy | 5 |
| `src/issuebot/config/workflow.py` | `parse_workflow_text`, `load_workflow`, `Workflow` | 5, 8 |
| `src/issuebot/config/settings.py` | pydantic models | 6 |
| `src/issuebot/config/resolve.py` | `$VAR`, `~`, relative-path resolution | 7 |
| `src/issuebot/config/__init__.py` | re-exports | 5, 8 |
| `tests/fixtures/workflows/good.md`, `invalid.md` | CLI fixtures | 9 |
| `WORKFLOW.md`, `.env.example` | dogfood config, secrets template | 9 |
| `Dockerfile`, `.dockerignore`, `compose.yaml` | image and stack | 10 |
| `.github/workflows/ci.yml`, `.github/dependabot.yml` | CI, updates | 11 |
| `CLAUDE.md`, `README.md` | commands and layout | 12 |

---

### Task 1: Project scaffold and `--version`

**Files:**
- Create: `pyproject.toml`, `.python-version`, `src/issuebot/__init__.py`, `src/issuebot/__main__.py`, `src/issuebot/cli.py`, `tests/conftest.py`, `tests/test_cli.py`
- Modify: `.pre-commit-config.yaml`

**Interfaces:**
- Produces: `issuebot.__version__: str`; `issuebot.cli.main(argv: Sequence[str] | None = None) -> int`; `issuebot.cli.build_parser() -> argparse.ArgumentParser`; the `clean_env` autouse fixture.

- [ ] **Step 1: Create the feature branch**

Run: `git checkout -b phase-1-foundations` (skip if already on a feature branch or in a worktree created for this plan)

- [ ] **Step 2: Write `pyproject.toml` and `.python-version`**

`pyproject.toml`:

```toml
[project]
name = "issuebot"
version = "0.1.0"
description = "Symphony-style issue-to-PR agent orchestrator built on Claude and GitHub"
readme = "README.md"
requires-python = ">=3.14"
dependencies = [
  "pydantic>=2.12",
  "pyyaml>=6.0",
  "structlog>=25.1",
]

[project.scripts]
issuebot = "issuebot.cli:main"

[dependency-groups]
dev = [
  "pre-commit>=4.0",
  "pytest>=8.4",
  "pytest-asyncio>=1.0",
  "ruff==0.16.5",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/issuebot"]

[tool.ruff]
target-version = "py314"
line-length = 100
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "N", "SIM", "RUF"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

`.python-version`:

```
3.14
```

- [ ] **Step 3: Create the package skeleton and lock dependencies**

`src/issuebot/__init__.py`:

```python
"""issuebot: an issue-to-PR agent orchestrator built on Claude and GitHub."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("issuebot")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"
```

`src/issuebot/__main__.py`:

```python
"""Allow ``python -m issuebot``."""

import sys

from issuebot.cli import main

if __name__ == "__main__":
    sys.exit(main())
```

`src/issuebot/cli.py` (temporary stub so the package imports; replaced in Step 6):

```python
"""Command-line entry point for issuebot."""
```

Run: `uv sync`
Expected: creates `.venv` with Python 3.14 and writes `uv.lock`. Confirm with `uv run python --version` printing `Python 3.14.x`.

- [ ] **Step 4: Write the failing CLI tests and the shared fixture**

`tests/conftest.py`:

```python
"""Shared pytest fixtures."""

import pytest

_ENV_VARS = (
    "GH_TOKEN",
    "DATABASE_URL",
    "SLACK_WEBHOOK_URL",
    "ISSUEBOT_WORKSPACE_ROOT",
    "ISSUEBOT_WORKFLOW",
    "ISSUEBOT_LOG_LEVEL",
    "ISSUEBOT_LOG_FORMAT",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test independent of the developer's shell environment."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
```

`tests/test_cli.py`:

```python
"""Tests for the command-line entry point."""

import subprocess

import pytest

from issuebot import __version__
from issuebot.cli import main


def test_version_flag_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"issuebot {__version__}"


def test_no_command_prints_help_and_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage: issuebot" in capsys.readouterr().out


def test_unknown_command_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2


def test_installed_script_runs_version() -> None:
    result = subprocess.run(["issuebot", "--version"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert result.stdout.strip() == f"issuebot {__version__}"
```

- [ ] **Step 5: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ImportError: cannot import name 'main' from 'issuebot.cli'`.

- [ ] **Step 6: Implement the minimal CLI**

`src/issuebot/cli.py`:

```python
"""Command-line entry point for issuebot."""

import argparse
from collections.abc import Sequence

from issuebot import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issuebot",
        description="Issue-to-PR agent orchestrator for GitHub and Claude.",
    )
    parser.add_argument("--version", action="version", version=f"issuebot {__version__}")
    parser.add_subparsers(dest="command", metavar="<command>")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    return 0
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 4 passed.

- [ ] **Step 8: Bump pre-commit revs and run all hooks**

Replace `.pre-commit-config.yaml` with:

```yaml
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-added-large-files
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.5
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
```

Run: `uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files`
Expected: all hooks pass (fix any whitespace it reports and re-run).

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock .python-version src/issuebot tests/conftest.py tests/test_cli.py .pre-commit-config.yaml
git commit -m "feat: scaffold issuebot package with uv, ruff, pytest and version CLI"
```

---

### Task 2: Structured logging

**Files:**
- Create: `src/issuebot/log.py`, `tests/test_log.py`

**Interfaces:**
- Produces: `configure_logging(*, level: str = "INFO", fmt: Literal["json", "console"] = "json", stream: TextIO = sys.stderr) -> None`; `get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger`; `bind_issue_context(*, issue_number: int, issue_identifier: str) -> None`; `bind_session_context(*, session_id: str) -> None`; `clear_context() -> None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_log.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_log.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.log'`.

- [ ] **Step 3: Implement `issuebot.log`**

`src/issuebot/log.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_log.py -v`
Expected: 6 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/issuebot/log.py tests/test_log.py
git commit -m "feat: add structlog-based JSON logging with issue and session context"
```

---

### Task 3: Event types

**Files:**
- Create: `src/issuebot/events/__init__.py`, `src/issuebot/events/types.py`, `tests/test_events.py`

**Interfaces:**
- Produces: `Event` (base; `kind: ClassVar[str]`, `at: datetime`, `to_dict() -> dict[str, Any]`), `IssueEvent(Event)` (`issue_number: int`, `issue_identifier: str`), `StateChanged`, `RunStarted`, `RunEnded`, `PrOpened`, `Blocked`, `IssueCompleted`, `IssueCancelled`, `NotificationSent`, and `EVENT_KINDS: frozenset[str]`. Task 6 validates `notifications.slack.events` against `EVENT_KINDS`; Task 4 consumes `Event`.

- [ ] **Step 1: Write the failing tests**

`tests/test_events.py`:

```python
"""Tests for event types."""

import json
from datetime import UTC, datetime

from issuebot.events import (
    EVENT_KINDS,
    Blocked,
    Event,
    IssueCancelled,
    IssueCompleted,
    NotificationSent,
    PrOpened,
    RunEnded,
    RunStarted,
    StateChanged,
)

ALL_EVENTS: list[Event] = [
    StateChanged(
        issue_number=1,
        issue_identifier="repo-1",
        from_label="issuebot/todo",
        to_label="issuebot/in-progress",
        actor="issuebot",
    ),
    RunStarted(
        issue_number=1,
        issue_identifier="repo-1",
        run_id="run-1",
        attempt=1,
        session_id=None,
        workspace_path="/workspaces/repo-1",
    ),
    RunEnded(
        issue_number=1,
        issue_identifier="repo-1",
        run_id="run-1",
        outcome="succeeded",
        error=None,
        turns=2,
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.01,
        duration_s=12.5,
    ),
    PrOpened(issue_number=1, issue_identifier="repo-1", pr_number=9, pr_url="https://x/pull/9"),
    Blocked(issue_number=1, issue_identifier="repo-1", reason="turn budget exhausted"),
    IssueCompleted(issue_number=1, issue_identifier="repo-1", pr_url="https://x/pull/9"),
    IssueCancelled(issue_number=1, issue_identifier="repo-1", reason="closed without merge"),
    NotificationSent(
        issue_number=1, issue_identifier="repo-1", channel="slack", about_kind="state_changed"
    ),
]


def test_every_event_serialises_to_json() -> None:
    for event in ALL_EVENTS:
        data = event.to_dict()
        assert data["kind"] == event.kind
        assert data["issue_number"] == 1
        assert data["issue_identifier"] == "repo-1"
        datetime.fromisoformat(data["at"])
        json.dumps(data)


def test_event_kinds_registry_matches_classes() -> None:
    assert EVENT_KINDS == {
        "state_changed",
        "run_started",
        "run_ended",
        "pr_opened",
        "blocked",
        "issue_completed",
        "issue_cancelled",
        "notification_sent",
    }
    assert {event.kind for event in ALL_EVENTS} == EVENT_KINDS


def test_at_defaults_to_aware_utc_now() -> None:
    before = datetime.now(UTC)
    event = Blocked(issue_number=1, issue_identifier="repo-1", reason="x")
    assert event.at.tzinfo is UTC
    assert before <= event.at <= datetime.now(UTC)


def test_state_changed_pr_url_defaults_to_none() -> None:
    event = StateChanged(
        issue_number=1, issue_identifier="repo-1", from_label=None, to_label="x", actor="human"
    )
    assert event.to_dict()["pr_url"] is None


def test_events_are_immutable() -> None:
    event = Blocked(issue_number=1, issue_identifier="repo-1", reason="x")
    try:
        event.reason = "y"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("event was mutable")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.events'`.

- [ ] **Step 3: Implement the event types**

`src/issuebot/events/types.py`:

```python
"""Domain events published by the orchestrator and consumed by sinks."""

from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True)
class Event:
    """Base event. ``kind`` is a stable string identifier; ``at`` is an aware UTC timestamp."""

    kind: ClassVar[str] = "event"
    at: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe mapping with ``kind``, ISO 8601 ``at`` and every field."""
        data: dict[str, Any] = {"kind": self.kind, "at": self.at.isoformat()}
        for f in fields(self):
            if f.name != "at":
                data[f.name] = getattr(self, f.name)
        return data


@dataclass(frozen=True, kw_only=True)
class IssueEvent(Event):
    issue_number: int
    issue_identifier: str


@dataclass(frozen=True, kw_only=True)
class StateChanged(IssueEvent):
    kind: ClassVar[str] = "state_changed"
    from_label: str | None
    to_label: str | None
    actor: Literal["issuebot", "agent", "human"]
    pr_url: str | None = None


@dataclass(frozen=True, kw_only=True)
class RunStarted(IssueEvent):
    kind: ClassVar[str] = "run_started"
    run_id: str
    attempt: int
    session_id: str | None
    workspace_path: str


RunOutcome = Literal["succeeded", "failed", "timed_out", "stalled", "cancelled"]


@dataclass(frozen=True, kw_only=True)
class RunEnded(IssueEvent):
    kind: ClassVar[str] = "run_ended"
    run_id: str
    outcome: RunOutcome
    error: str | None
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_s: float


@dataclass(frozen=True, kw_only=True)
class PrOpened(IssueEvent):
    kind: ClassVar[str] = "pr_opened"
    pr_number: int
    pr_url: str


@dataclass(frozen=True, kw_only=True)
class Blocked(IssueEvent):
    kind: ClassVar[str] = "blocked"
    reason: str


@dataclass(frozen=True, kw_only=True)
class IssueCompleted(IssueEvent):
    kind: ClassVar[str] = "issue_completed"
    pr_url: str | None


@dataclass(frozen=True, kw_only=True)
class IssueCancelled(IssueEvent):
    kind: ClassVar[str] = "issue_cancelled"
    reason: str


@dataclass(frozen=True, kw_only=True)
class NotificationSent(IssueEvent):
    kind: ClassVar[str] = "notification_sent"
    channel: str
    about_kind: str


EVENT_TYPES: tuple[type[Event], ...] = (
    StateChanged,
    RunStarted,
    RunEnded,
    PrOpened,
    Blocked,
    IssueCompleted,
    IssueCancelled,
    NotificationSent,
)

EVENT_KINDS: frozenset[str] = frozenset(cls.kind for cls in EVENT_TYPES)
```

`src/issuebot/events/__init__.py`:

```python
"""Event types, the event bus and built-in sinks."""

from issuebot.events.types import (
    EVENT_KINDS,
    EVENT_TYPES,
    Blocked,
    Event,
    IssueCancelled,
    IssueCompleted,
    IssueEvent,
    NotificationSent,
    PrOpened,
    RunEnded,
    RunOutcome,
    RunStarted,
    StateChanged,
)

__all__ = [
    "EVENT_KINDS",
    "EVENT_TYPES",
    "Blocked",
    "Event",
    "IssueCancelled",
    "IssueCompleted",
    "IssueEvent",
    "NotificationSent",
    "PrOpened",
    "RunEnded",
    "RunOutcome",
    "RunStarted",
    "StateChanged",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_events.py -v`
Expected: 5 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/issuebot/events tests/test_events.py
git commit -m "feat: add frozen domain event types and EVENT_KINDS registry"
```

---

### Task 4: Event bus and log sink

**Files:**
- Create: `src/issuebot/events/bus.py`, `src/issuebot/events/log_sink.py`
- Modify: `src/issuebot/events/__init__.py`, `tests/test_events.py`

**Interfaces:**
- Consumes: `Event` from Task 3; `get_logger` from Task 2.
- Produces: `EventSink` protocol (`name: str`, `handle(event: Event) -> None`); `EventBus(sinks: Iterable[EventSink] = ())` with `add_sink(sink)`, `remove_sink(name)`, `publish(event)`, `sinks: tuple[EventSink, ...]`, `failures: dict[str, int]`; `LogSink(logger=None)` with `name == "log"`.

- [ ] **Step 1: Append the failing tests**

Replace the import block at the top of `tests/test_events.py` with:

```python
import io
import json
from datetime import UTC, datetime

import pytest

from issuebot.events import (
    EVENT_KINDS,
    Blocked,
    Event,
    EventBus,
    IssueCancelled,
    IssueCompleted,
    LogSink,
    NotificationSent,
    PrOpened,
    RunEnded,
    RunStarted,
    StateChanged,
)
from issuebot.log import configure_logging
```

Then append to the end of the file:

```python
# --- bus and sinks -------------------------------------------------------------


class _Recorder:
    def __init__(self, name: str) -> None:
        self.name = name
        self.seen: list[Event] = []

    def handle(self, event: Event) -> None:
        self.seen.append(event)


class _Exploder:
    name = "exploder"

    def handle(self, event: Event) -> None:
        raise RuntimeError("boom")


def _blocked() -> Blocked:
    return Blocked(issue_number=1, issue_identifier="repo-1", reason="x")


def test_publish_fans_out_in_registration_order() -> None:
    first, second = _Recorder("first"), _Recorder("second")
    bus = EventBus([first])
    bus.add_sink(second)
    event = _blocked()
    bus.publish(event)
    assert first.seen == [event]
    assert second.seen == [event]
    assert [s.name for s in bus.sinks] == ["first", "second"]


def test_raising_sink_is_isolated_counted_and_logged() -> None:
    stream = io.StringIO()
    configure_logging(fmt="json", stream=stream)  # type: ignore[arg-type]
    after = _Recorder("after")
    bus = EventBus([_Exploder(), after])
    bus.publish(_blocked())
    bus.publish(_blocked())
    assert len(after.seen) == 2
    assert bus.failures == {"exploder": 2}
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0]["event"] == "event_sink_failed"
    assert lines[0]["sink"] == "exploder"
    assert lines[0]["event_kind"] == "blocked"
    assert lines[0]["level"] == "error"


def test_duplicate_sink_name_is_rejected() -> None:
    bus = EventBus([_Recorder("dup")])
    with pytest.raises(ValueError, match="dup"):
        bus.add_sink(_Recorder("dup"))


def test_remove_sink_stops_delivery() -> None:
    sink = _Recorder("gone")
    bus = EventBus([sink])
    bus.remove_sink("gone")
    bus.publish(_blocked())
    assert sink.seen == []
    assert bus.sinks == ()


def test_log_sink_logs_kind_and_fields() -> None:
    stream = io.StringIO()
    configure_logging(fmt="json", stream=stream)  # type: ignore[arg-type]
    bus = EventBus([LogSink()])
    bus.publish(
        StateChanged(
            issue_number=3,
            issue_identifier="repo-3",
            from_label="issuebot/todo",
            to_label="issuebot/in-progress",
            actor="issuebot",
        )
    )
    (record,) = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert record["event"] == "state_changed"
    assert record["issue_number"] == 3
    assert record["to_label"] == "issuebot/in-progress"
    assert record["logger"] == "issuebot.events"
    assert record["level"] == "info"
    assert LogSink.name == "log"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_events.py -v`
Expected: FAIL with `ImportError: cannot import name 'EventBus' from 'issuebot.events'`.

- [ ] **Step 3: Implement the bus and the log sink**

`src/issuebot/events/bus.py`:

```python
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
```

`src/issuebot/events/log_sink.py`:

```python
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
```

Replace `src/issuebot/events/__init__.py` with:

```python
"""Event types, the event bus and built-in sinks."""

from issuebot.events.bus import EventBus, EventSink
from issuebot.events.log_sink import LogSink
from issuebot.events.types import (
    EVENT_KINDS,
    EVENT_TYPES,
    Blocked,
    Event,
    IssueCancelled,
    IssueCompleted,
    IssueEvent,
    NotificationSent,
    PrOpened,
    RunEnded,
    RunOutcome,
    RunStarted,
    StateChanged,
)

__all__ = [
    "EVENT_KINDS",
    "EVENT_TYPES",
    "Blocked",
    "Event",
    "EventBus",
    "EventSink",
    "IssueCancelled",
    "IssueCompleted",
    "IssueEvent",
    "LogSink",
    "NotificationSent",
    "PrOpened",
    "RunEnded",
    "RunOutcome",
    "RunStarted",
    "StateChanged",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_events.py -v`
Expected: 10 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/issuebot/events tests/test_events.py
git commit -m "feat: add EventBus with failure isolation and LogSink"
```

---

### Task 5: Config errors and front-matter parsing

**Files:**
- Create: `src/issuebot/config/__init__.py`, `src/issuebot/config/errors.py`, `src/issuebot/config/workflow.py`, `tests/test_workflow.py`

**Interfaces:**
- Produces: `ConfigError(message, *, path=None)` with `.code`, `.message`, `.path`; subclasses `MissingWorkflowFile`, `WorkflowParseError`, `FrontMatterNotAMap`, `MissingEnvironmentVariable(*, variable, field, path=None)` (`.variable`, `.field`), `SettingsValidationError(errors: list[tuple[str, str]], *, path=None)` (`.errors`); `parse_workflow_text(text: str) -> tuple[dict[str, Any], str]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_workflow.py`:

```python
"""Tests for WORKFLOW.md parsing and loading."""

from pathlib import Path

import pytest

from issuebot.config.errors import (
    ConfigError,
    FrontMatterNotAMap,
    MissingEnvironmentVariable,
    SettingsValidationError,
    WorkflowParseError,
)
from issuebot.config.workflow import parse_workflow_text

# --- parse_workflow_text -------------------------------------------------------


def test_splits_front_matter_and_body() -> None:
    raw, body = parse_workflow_text("---\ngithub:\n  repo: o/r\n---\n\nHello {{ issue.title }}\n")
    assert raw == {"github": {"repo": "o/r"}}
    assert body == "Hello {{ issue.title }}"


def test_crlf_input_is_normalised() -> None:
    raw, body = parse_workflow_text("---\r\npolling:\r\n  interval_ms: 5000\r\n---\r\nBody\r\n")
    assert raw == {"polling": {"interval_ms": 5000}}
    assert body == "Body"


def test_bom_is_ignored() -> None:
    raw, body = parse_workflow_text("﻿---\nx: 1\n---\nB")
    assert raw == {"x": 1}
    assert body == "B"


def test_no_front_matter_is_all_body() -> None:
    assert parse_workflow_text("Just a prompt\n") == ({}, "Just a prompt")


def test_empty_front_matter_is_empty_mapping() -> None:
    assert parse_workflow_text("---\n---\nBody") == ({}, "Body")


def test_unterminated_front_matter_is_parse_error() -> None:
    with pytest.raises(WorkflowParseError, match="never closed"):
        parse_workflow_text("---\ngithub: {}\nBody")


def test_invalid_yaml_is_parse_error() -> None:
    with pytest.raises(WorkflowParseError, match="invalid YAML"):
        parse_workflow_text("---\ngithub: [unclosed\n---\nBody")


def test_non_mapping_front_matter_is_rejected() -> None:
    with pytest.raises(FrontMatterNotAMap, match="mapping"):
        parse_workflow_text("---\n- a\n- b\n---\nBody")


# --- error types -----------------------------------------------------------------


def test_error_codes() -> None:
    assert WorkflowParseError("x").code == "workflow_parse_error"
    assert FrontMatterNotAMap("x").code == "workflow_front_matter_not_a_map"
    assert (
        MissingEnvironmentVariable(variable="V", field="f").code == "missing_environment_variable"
    )
    assert SettingsValidationError([]).code == "invalid_settings"


def test_error_str_includes_path_when_present() -> None:
    err = WorkflowParseError("bad", path=Path("/w/WORKFLOW.md"))
    assert str(err) == "/w/WORKFLOW.md: bad"
    assert isinstance(err, ConfigError)
    assert str(WorkflowParseError("bad")) == "bad"


def test_missing_environment_variable_message() -> None:
    err = MissingEnvironmentVariable(variable="MY_TOKEN", field="github.token")
    assert err.variable == "MY_TOKEN"
    assert err.field == "github.token"
    assert str(err) == "github.token references $MY_TOKEN, which is unset or empty"


def test_settings_validation_error_lists_fields() -> None:
    err = SettingsValidationError(
        [("polling.interval_ms", "too small"), ("agnet", "Extra inputs are not permitted")],
        path=Path("/w/WORKFLOW.md"),
    )
    assert str(err) == (
        "/w/WORKFLOW.md: 2 invalid setting(s)\n"
        "  polling.interval_ms: too small\n"
        "  agnet: Extra inputs are not permitted"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_workflow.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.config'`.

- [ ] **Step 3: Implement the errors and the parser**

`src/issuebot/config/errors.py`:

```python
"""Typed configuration errors. Every error has a stable ``code`` for logs and tests."""

from pathlib import Path


class ConfigError(Exception):
    code: str = "config_error"

    def __init__(self, message: str, *, path: Path | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.path = path

    def __str__(self) -> str:
        prefix = f"{self.path}: " if self.path is not None else ""
        return f"{prefix}{self.message}"


class MissingWorkflowFile(ConfigError):
    code = "missing_workflow_file"


class WorkflowParseError(ConfigError):
    code = "workflow_parse_error"


class FrontMatterNotAMap(ConfigError):
    code = "workflow_front_matter_not_a_map"


class MissingEnvironmentVariable(ConfigError):
    code = "missing_environment_variable"

    def __init__(self, *, variable: str, field: str, path: Path | None = None) -> None:
        super().__init__(f"{field} references ${variable}, which is unset or empty", path=path)
        self.variable = variable
        self.field = field


class SettingsValidationError(ConfigError):
    code = "invalid_settings"

    def __init__(self, errors: list[tuple[str, str]], *, path: Path | None = None) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} invalid setting(s)", path=path)

    def __str__(self) -> str:
        lines = [super().__str__()]
        lines.extend(f"  {field}: {message}" for field, message in self.errors)
        return "\n".join(lines)
```

`src/issuebot/config/workflow.py` (the loader half is added in Task 8):

```python
"""WORKFLOW.md: YAML front matter plus a Markdown prompt body."""

from typing import Any

import yaml

from issuebot.config.errors import FrontMatterNotAMap, WorkflowParseError

FRONT_MATTER_DELIMITER = "---"


def parse_workflow_text(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into ``(front_matter_mapping, stripped_body)``.

    CRLF is normalised, a leading BOM is dropped, and a file without a leading ``---``
    line is treated as body only with an empty mapping.
    """
    text = text.lstrip("﻿").replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != FRONT_MATTER_DELIMITER:
        return {}, text.strip()

    end = next(
        (i for i in range(1, len(lines)) if lines[i].rstrip() == FRONT_MATTER_DELIMITER),
        None,
    )
    if end is None:
        raise WorkflowParseError("front matter opened with '---' but never closed")

    front_matter = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        raw = yaml.safe_load(front_matter)
    except yaml.YAMLError as exc:
        raise WorkflowParseError(f"invalid YAML front matter: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise FrontMatterNotAMap(f"front matter must be a mapping, got {type(raw).__name__}")
    return raw, body.strip()
```

`src/issuebot/config/__init__.py` (extended in Task 8):

```python
"""Configuration: WORKFLOW.md loading, environment resolution and typed settings."""

from issuebot.config.errors import (
    ConfigError,
    FrontMatterNotAMap,
    MissingEnvironmentVariable,
    MissingWorkflowFile,
    SettingsValidationError,
    WorkflowParseError,
)
from issuebot.config.workflow import parse_workflow_text

__all__ = [
    "ConfigError",
    "FrontMatterNotAMap",
    "MissingEnvironmentVariable",
    "MissingWorkflowFile",
    "SettingsValidationError",
    "WorkflowParseError",
    "parse_workflow_text",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_workflow.py -v`
Expected: 12 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/issuebot/config tests/test_workflow.py
git commit -m "feat: add config error types and WORKFLOW.md front-matter parser"
```

---

### Task 6: Settings models

**Files:**
- Create: `src/issuebot/config/settings.py`, `tests/test_settings.py`

**Interfaces:**
- Consumes: `EVENT_KINDS` from Task 3.
- Produces: `Settings` and sub-models `GitHubSettings`, `GitHubLabels` (with `as_tuple() -> tuple[str, ...]`), `PollingSettings`, `WorkspaceSettings`, `HooksSettings`, `AgentSettings`, `ClaudeSettings`, `DatabaseSettings`, `NotificationsSettings`, `SlackSettings`, `ServerSettings`; type alias `PermissionMode`. Every field, default and constraint is in spec section 4.2.

- [ ] **Step 1: Write the failing tests**

`tests/test_settings.py`:

```python
"""Tests for the typed settings models."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from issuebot.config.settings import GitHubLabels, Settings

MINIMAL = {"github": {"repo": "owner/repo"}}


def _locs(exc: ValidationError) -> set[str]:
    return {".".join(str(p) for p in e["loc"]) for e in exc.errors()}


def test_minimal_config_applies_every_default() -> None:
    s = Settings.model_validate(MINIMAL)
    assert s.github.repo == "owner/repo"
    assert s.github.token is None
    assert s.github.labels.as_tuple() == (
        "issuebot/todo",
        "issuebot/in-progress",
        "issuebot/review",
        "issuebot/rework",
        "issuebot/complete",
    )
    assert s.polling.interval_ms == 30_000
    assert s.workspace.root == Path("/workspaces")
    assert s.hooks.after_create is None
    assert s.hooks.before_run is None
    assert s.hooks.after_run is None
    assert s.hooks.before_remove is None
    assert s.hooks.timeout_ms == 60_000
    assert s.agent.max_concurrent_agents == 3
    assert s.agent.max_turns == 5
    assert s.agent.max_attempts == 3
    assert s.agent.max_retry_backoff_ms == 300_000
    assert s.claude.command == "claude"
    assert s.claude.model is None
    assert s.claude.permission_mode == "auto"
    assert s.claude.max_budget_usd == 5.0
    assert s.claude.turn_timeout_ms == 3_600_000
    assert s.claude.stall_timeout_ms == 300_000
    assert s.claude.allowed_tools == []
    assert s.claude.disallowed_tools == []
    assert s.claude.append_system_prompt is None
    assert s.database.url is None
    assert s.notifications.slack.webhook_url is None
    assert s.notifications.slack.events == ["state_changed", "blocked"]
    assert s.server.port == 8080
    assert s.server.bind == "0.0.0.0"


def test_github_repo_is_required() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({})
    assert "github" in _locs(exc.value)


@pytest.mark.parametrize("repo", ["owner", "owner/", "/repo", "owner/repo/extra", "a b/c"])
def test_github_repo_must_be_owner_slash_name(repo: str) -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({"github": {"repo": repo}})
    assert "github.repo" in _locs(exc.value)


def test_secrets_are_secretstr() -> None:
    s = Settings.model_validate(
        {
            "github": {"repo": "o/r", "token": "tok"},
            "database": {"url": "postgresql://u:p@h/db"},
            "notifications": {"slack": {"webhook_url": "https://hooks/x"}},
        }
    )
    assert isinstance(s.github.token, SecretStr)
    assert s.github.token.get_secret_value() == "tok"
    assert "tok" not in repr(s.github.token)
    assert isinstance(s.database.url, SecretStr)
    assert isinstance(s.notifications.slack.webhook_url, SecretStr)


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({**MINIMAL, "agnet": {}})
    assert "agnet" in _locs(exc.value)


def test_unknown_nested_key_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({"github": {"repo": "o/r", "tokne": "x"}})
    assert "github.tokne" in _locs(exc.value)


def test_state_labels_must_be_distinct() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        GitHubLabels(todo="same", review="same")


def test_label_must_not_be_empty() -> None:
    with pytest.raises(ValidationError) as exc:
        GitHubLabels(todo="")
    assert "todo" in _locs(exc.value)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("polling", "interval_ms", 999),
        ("hooks", "timeout_ms", 0),
        ("agent", "max_concurrent_agents", 0),
        ("agent", "max_turns", 0),
        ("agent", "max_attempts", 0),
        ("agent", "max_retry_backoff_ms", 999),
        ("claude", "command", ""),
        ("claude", "max_budget_usd", 0),
        ("claude", "turn_timeout_ms", 0),
        ("claude", "permission_mode", "plan"),
        ("claude", "permission_mode", "manual"),
        ("server", "port", 65536),
        ("server", "port", -1),
        ("server", "bind", ""),
    ],
)
def test_constraints_reject_out_of_range_values(section: str, field: str, value: object) -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({**MINIMAL, section: {field: value}})
    assert f"{section}.{field}" in _locs(exc.value)


@pytest.mark.parametrize("mode", ["auto", "acceptEdits", "dontAsk", "bypassPermissions"])
def test_permission_mode_accepts_unattended_values(mode: str) -> None:
    s = Settings.model_validate({**MINIMAL, "claude": {"permission_mode": mode}})
    assert s.claude.permission_mode == mode


def test_stall_timeout_accepts_zero_and_negative_to_disable() -> None:
    s = Settings.model_validate({**MINIMAL, "claude": {"stall_timeout_ms": 0}})
    assert s.claude.stall_timeout_ms == 0
    s = Settings.model_validate({**MINIMAL, "claude": {"stall_timeout_ms": -1}})
    assert s.claude.stall_timeout_ms == -1


def test_workspace_root_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        Settings.model_validate({**MINIMAL, "workspace": {"root": "relative/path"}})


def test_hook_scripts_are_kept_verbatim() -> None:
    script = "git fetch origin && echo $HOME\n"
    s = Settings.model_validate({**MINIMAL, "hooks": {"before_run": script}})
    assert s.hooks.before_run == script


def test_slack_events_must_be_known_kinds() -> None:
    with pytest.raises(ValidationError, match="unknown event kinds: nope") as exc:
        Settings.model_validate(
            {**MINIMAL, "notifications": {"slack": {"events": ["state_changed", "nope"]}}}
        )
    assert "notifications.slack.events" in _locs(exc.value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_settings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.config.settings'`.

- [ ] **Step 3: Implement the models**

`src/issuebot/config/settings.py`:

```python
"""Typed runtime settings parsed from WORKFLOW.md front matter (after resolution)."""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from issuebot.events.types import EVENT_KINDS


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


RepoName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
NonEmptyStr = Annotated[str, Field(min_length=1)]
PermissionMode = Literal["auto", "acceptEdits", "dontAsk", "bypassPermissions"]


class GitHubLabels(_Model):
    todo: NonEmptyStr = "issuebot/todo"
    in_progress: NonEmptyStr = "issuebot/in-progress"
    review: NonEmptyStr = "issuebot/review"
    rework: NonEmptyStr = "issuebot/rework"
    complete: NonEmptyStr = "issuebot/complete"

    def as_tuple(self) -> tuple[str, ...]:
        return (self.todo, self.in_progress, self.review, self.rework, self.complete)

    @model_validator(mode="after")
    def _labels_are_distinct(self) -> Self:
        values = self.as_tuple()
        if len(set(values)) != len(values):
            raise ValueError("state labels must be distinct")
        return self


class GitHubSettings(_Model):
    repo: RepoName
    token: SecretStr | None = None
    labels: GitHubLabels = Field(default_factory=GitHubLabels)


class PollingSettings(_Model):
    interval_ms: int = Field(default=30_000, ge=1000)


class WorkspaceSettings(_Model):
    root: Path = Path("/workspaces")

    @field_validator("root")
    @classmethod
    def _root_is_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("workspace.root must be absolute after resolution")
        return value


class HooksSettings(_Model):
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = Field(default=60_000, ge=1)


class AgentSettings(_Model):
    max_concurrent_agents: int = Field(default=3, ge=1)
    max_turns: int = Field(default=5, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    max_retry_backoff_ms: int = Field(default=300_000, ge=1000)


class ClaudeSettings(_Model):
    command: NonEmptyStr = "claude"
    model: str | None = None
    permission_mode: PermissionMode = "auto"
    max_budget_usd: float = Field(default=5.0, gt=0)
    turn_timeout_ms: int = Field(default=3_600_000, ge=1)
    stall_timeout_ms: int = 300_000
    allowed_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    append_system_prompt: str | None = None


class DatabaseSettings(_Model):
    url: SecretStr | None = None


class SlackSettings(_Model):
    webhook_url: SecretStr | None = None
    events: list[str] = Field(default_factory=lambda: ["state_changed", "blocked"])

    @field_validator("events")
    @classmethod
    def _events_are_known(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - EVENT_KINDS)
        if unknown:
            known = ", ".join(sorted(EVENT_KINDS))
            raise ValueError(f"unknown event kinds: {', '.join(unknown)}; known kinds: {known}")
        return value


class NotificationsSettings(_Model):
    slack: SlackSettings = Field(default_factory=SlackSettings)


class ServerSettings(_Model):
    port: int = Field(default=8080, ge=0, le=65535)
    bind: NonEmptyStr = "0.0.0.0"


class Settings(_Model):
    github: GitHubSettings
    polling: PollingSettings = Field(default_factory=PollingSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    hooks: HooksSettings = Field(default_factory=HooksSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    notifications: NotificationsSettings = Field(default_factory=NotificationsSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_settings.py -v`
Expected: 34 passed (including parametrised cases).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/issuebot/config/settings.py tests/test_settings.py
git commit -m "feat: add pydantic settings models for WORKFLOW.md front matter"
```

---

### Task 7: Environment and path resolution

**Files:**
- Create: `src/issuebot/config/resolve.py`, `tests/test_resolve.py`

**Interfaces:**
- Consumes: `MissingEnvironmentVariable` from Task 5.
- Produces: `ENV_REF: re.Pattern[str]`; `resolve_env_value(value: Any, *, field: str, fallback: str | None, environ: Mapping[str, str]) -> Any`; `resolve_path(value: str, *, base_dir: Path) -> Path`; `resolve_config(raw: Mapping[str, Any], *, environ: Mapping[str, str], base_dir: Path) -> dict[str, Any]`; constants `SECRET_FIELDS`, `WORKSPACE_ROOT_FIELD`, `WORKSPACE_ROOT_FALLBACK`, `WORKSPACE_ROOT_DEFAULT`.

- [ ] **Step 1: Write the failing tests**

`tests/test_resolve.py`:

```python
"""Tests for $VAR, ~ and relative-path resolution of designated config fields."""

from pathlib import Path

import pytest

from issuebot.config.errors import MissingEnvironmentVariable
from issuebot.config.resolve import resolve_config, resolve_env_value

BASE = Path("/srv/workflows")


def test_explicit_reference_resolves_from_environ() -> None:
    out = resolve_config(
        {"github": {"repo": "o/r", "token": "$MY_TOKEN"}},
        environ={"MY_TOKEN": "abc"},
        base_dir=BASE,
    )
    assert out["github"]["token"] == "abc"


def test_explicit_reference_to_unset_variable_fails() -> None:
    with pytest.raises(MissingEnvironmentVariable) as exc:
        resolve_config({"github": {"token": "$MY_TOKEN"}}, environ={}, base_dir=BASE)
    assert exc.value.variable == "MY_TOKEN"
    assert exc.value.field == "github.token"


def test_explicit_reference_to_empty_variable_fails() -> None:
    with pytest.raises(MissingEnvironmentVariable):
        resolve_config({"github": {"token": "$MY_TOKEN"}}, environ={"MY_TOKEN": ""}, base_dir=BASE)


def test_omitted_secret_uses_fallback_variable() -> None:
    out = resolve_config({"github": {"repo": "o/r"}}, environ={"GH_TOKEN": "fb"}, base_dir=BASE)
    assert out["github"]["token"] == "fb"


def test_omitted_secret_without_fallback_is_absent() -> None:
    out = resolve_config({"github": {"repo": "o/r"}}, environ={}, base_dir=BASE)
    assert "token" not in out["github"]
    assert "database" not in out
    assert "notifications" not in out


def test_all_three_secret_fallbacks() -> None:
    out = resolve_config(
        {"github": {"repo": "o/r"}},
        environ={"GH_TOKEN": "t", "DATABASE_URL": "d", "SLACK_WEBHOOK_URL": "s"},
        base_dir=BASE,
    )
    assert out["github"]["token"] == "t"
    assert out["database"]["url"] == "d"
    assert out["notifications"]["slack"]["webhook_url"] == "s"


def test_literal_secret_passes_through() -> None:
    out = resolve_config({"github": {"token": "ghp_literal"}}, environ={}, base_dir=BASE)
    assert out["github"]["token"] == "ghp_literal"


def test_embedded_reference_is_not_interpolated() -> None:
    out = resolve_config({"github": {"token": "prefix-$X"}}, environ={"X": "1"}, base_dir=BASE)
    assert out["github"]["token"] == "prefix-$X"


def test_workspace_root_default_when_unset() -> None:
    out = resolve_config({}, environ={}, base_dir=BASE)
    assert out["workspace"]["root"] == "/workspaces"


def test_workspace_root_fallback_variable() -> None:
    out = resolve_config({}, environ={"ISSUEBOT_WORKSPACE_ROOT": "/data/ws"}, base_dir=BASE)
    assert out["workspace"]["root"] == "/data/ws"


def test_workspace_root_explicit_reference() -> None:
    out = resolve_config({"workspace": {"root": "$WS"}}, environ={"WS": "/mnt/ws"}, base_dir=BASE)
    assert out["workspace"]["root"] == "/mnt/ws"


def test_relative_workspace_root_resolves_against_workflow_dir() -> None:
    out = resolve_config({"workspace": {"root": "ws"}}, environ={}, base_dir=BASE)
    assert out["workspace"]["root"] == "/srv/workflows/ws"


def test_tilde_expands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/tester")
    out = resolve_config({"workspace": {"root": "~/ws"}}, environ={}, base_dir=BASE)
    assert out["workspace"]["root"] == "/home/tester/ws"


def test_hook_scripts_are_untouched() -> None:
    raw = {"hooks": {"before_run": "echo $HOME && ls ~"}}
    out = resolve_config(raw, environ={"HOME": "/x"}, base_dir=BASE)
    assert out["hooks"]["before_run"] == "echo $HOME && ls ~"


def test_input_mapping_is_not_mutated() -> None:
    raw = {"github": {"repo": "o/r"}}
    resolve_config(raw, environ={"GH_TOKEN": "t"}, base_dir=BASE)
    assert raw == {"github": {"repo": "o/r"}}


def test_non_mapping_section_is_left_for_validation() -> None:
    out = resolve_config({"github": "oops"}, environ={"GH_TOKEN": "t"}, base_dir=BASE)
    assert out["github"] == "oops"


def test_resolve_env_value_non_string_passes_through() -> None:
    assert resolve_env_value(42, field="f", fallback=None, environ={}) == 42
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_resolve.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.config.resolve'`.

- [ ] **Step 3: Implement the resolver**

`src/issuebot/config/resolve.py`:

```python
"""Resolve ``$VAR`` references, ``~`` and relative paths for designated config fields.

Only the fields listed here are touched. Hook scripts and every other string are
passed to validation verbatim.
"""

import copy
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from issuebot.config.errors import MissingEnvironmentVariable

ENV_REF = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")

SECRET_FIELDS: dict[tuple[str, ...], str] = {
    ("github", "token"): "GH_TOKEN",
    ("database", "url"): "DATABASE_URL",
    ("notifications", "slack", "webhook_url"): "SLACK_WEBHOOK_URL",
}
WORKSPACE_ROOT_FIELD: tuple[str, ...] = ("workspace", "root")
WORKSPACE_ROOT_FALLBACK = "ISSUEBOT_WORKSPACE_ROOT"
WORKSPACE_ROOT_DEFAULT = "/workspaces"


def resolve_env_value(
    value: Any,
    *,
    field: str,
    fallback: str | None,
    environ: Mapping[str, str],
) -> Any:
    """Apply the ``$VAR`` rules to one value.

    ``None`` (absent) -> the fallback variable if set and non-empty, else ``None``.
    ``"$NAME"`` -> that variable; unset or empty raises ``MissingEnvironmentVariable``.
    Anything else is returned unchanged.
    """
    if value is None:
        if fallback and environ.get(fallback):
            return environ[fallback]
        return None
    if isinstance(value, str) and (match := ENV_REF.match(value)):
        name = match.group(1)
        resolved = environ.get(name)
        if not resolved:
            raise MissingEnvironmentVariable(variable=name, field=field)
        return resolved
    return value


def resolve_path(value: str, *, base_dir: Path) -> Path:
    """Expand ``~``, resolve relative to ``base_dir`` and normalise to absolute."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=False)


def resolve_config(
    raw: Mapping[str, Any],
    *,
    environ: Mapping[str, str],
    base_dir: Path,
) -> dict[str, Any]:
    """Return a deep copy of ``raw`` with the designated fields resolved."""
    config: dict[str, Any] = copy.deepcopy(dict(raw))

    for keypath, fallback in SECRET_FIELDS.items():
        resolved = resolve_env_value(
            _get(config, keypath), field=".".join(keypath), fallback=fallback, environ=environ
        )
        _set(config, keypath, resolved)

    root = resolve_env_value(
        _get(config, WORKSPACE_ROOT_FIELD),
        field=".".join(WORKSPACE_ROOT_FIELD),
        fallback=WORKSPACE_ROOT_FALLBACK,
        environ=environ,
    )
    if root is None:
        root = WORKSPACE_ROOT_DEFAULT
    if isinstance(root, str):
        root = str(resolve_path(root, base_dir=base_dir))
    _set(config, WORKSPACE_ROOT_FIELD, root)
    return config


def _get(config: Mapping[str, Any], keypath: tuple[str, ...]) -> Any:
    node: Any = config
    for key in keypath:
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def _set(config: dict[str, Any], keypath: tuple[str, ...], value: Any) -> None:
    """Set ``value`` at ``keypath``; skip when it would create a key just to hold ``None``
    or would overwrite a non-mapping intermediate (left for validation to report)."""
    *parents, leaf = keypath
    node: Any = config
    for key in parents:
        if key not in node:
            if value is None:
                return
            node[key] = {}
        node = node[key]
        if not isinstance(node, dict):
            return
    if value is None and leaf not in node:
        return
    node[leaf] = value
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_resolve.py -v`
Expected: 17 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/issuebot/config/resolve.py tests/test_resolve.py
git commit -m "feat: resolve \$VAR, ~ and relative paths for designated config fields"
```

---

### Task 8: `load_workflow` and package exports

**Files:**
- Modify: `src/issuebot/config/workflow.py`, `src/issuebot/config/__init__.py`, `tests/test_workflow.py`

**Interfaces:**
- Consumes: `parse_workflow_text` (Task 5), `resolve_config` (Task 7), `Settings` (Task 6), errors (Task 5).
- Produces: `Workflow` frozen dataclass (`path: Path`, `config: Settings`, `prompt_template: str`, `raw_config: dict[str, Any]`, `source_mtime_ns: int`); `load_workflow(path: Path | str, *, environ: Mapping[str, str] | None = None) -> Workflow`; `issuebot.config` re-exports `load_workflow`, `Workflow`, `Settings` and all sub-models.

- [ ] **Step 1: Append the failing tests**

Replace the import block at the top of `tests/test_workflow.py` with:

```python
from pathlib import Path

import pytest

from issuebot.config import Settings, Workflow, load_workflow
from issuebot.config.errors import (
    ConfigError,
    FrontMatterNotAMap,
    MissingEnvironmentVariable,
    MissingWorkflowFile,
    SettingsValidationError,
    WorkflowParseError,
)
from issuebot.config.workflow import parse_workflow_text
```

Then append to the end of the file:

```python
# --- load_workflow ---------------------------------------------------------------

GOOD = """---
github:
  repo: o/r
  token: $TOKEN
workspace:
  root: ws
---

Prompt body
"""


def test_load_workflow_builds_settings_and_prompt(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text(GOOD, encoding="utf-8")
    wf = load_workflow(wf_path, environ={"TOKEN": "t"})
    assert isinstance(wf, Workflow)
    assert isinstance(wf.config, Settings)
    assert wf.config.github.repo == "o/r"
    assert wf.config.github.token is not None
    assert wf.config.github.token.get_secret_value() == "t"
    assert wf.config.workspace.root == (tmp_path / "ws").resolve()
    assert wf.prompt_template == "Prompt body"
    assert wf.raw_config["github"]["token"] == "$TOKEN"
    assert wf.path == wf_path.resolve()
    assert wf.source_mtime_ns == wf_path.stat().st_mtime_ns


def test_load_workflow_accepts_str_path(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text(GOOD, encoding="utf-8")
    assert load_workflow(str(wf_path), environ={"TOKEN": "t"}).config.github.repo == "o/r"


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(MissingWorkflowFile) as exc:
        load_workflow(tmp_path / "nope.md", environ={})
    assert exc.value.path == (tmp_path / "nope.md").resolve()
    assert "not found" in str(exc.value)


def test_parse_errors_carry_path(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text("---\ngithub: {}\nBody", encoding="utf-8")
    with pytest.raises(WorkflowParseError) as exc:
        load_workflow(wf_path, environ={})
    assert exc.value.path == wf_path.resolve()


def test_missing_env_reference_carries_path(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text(GOOD, encoding="utf-8")
    with pytest.raises(MissingEnvironmentVariable) as exc:
        load_workflow(wf_path, environ={})
    assert exc.value.path == wf_path.resolve()
    assert exc.value.variable == "TOKEN"


def test_invalid_settings_lists_fields(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text(
        "---\ngithub:\n  repo: o/r\npolling:\n  interval_ms: 5\nagnet: {}\n---\nBody",
        encoding="utf-8",
    )
    with pytest.raises(SettingsValidationError) as exc:
        load_workflow(wf_path, environ={})
    fields = {field for field, _ in exc.value.errors}
    assert "polling.interval_ms" in fields
    assert "agnet" in fields
    assert exc.value.path == wf_path.resolve()


def test_body_only_file_fails_on_missing_repo(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text("Just a prompt", encoding="utf-8")
    with pytest.raises(SettingsValidationError) as exc:
        load_workflow(wf_path, environ={})
    assert {field for field, _ in exc.value.errors} == {"github"}


def test_default_environ_is_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text("---\ngithub:\n  repo: o/r\n---\nBody", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "from-process")
    wf = load_workflow(wf_path)
    assert wf.config.github.token is not None
    assert wf.config.github.token.get_secret_value() == "from-process"


def test_workflow_is_frozen(tmp_path: Path) -> None:
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text("---\ngithub:\n  repo: o/r\n---\nBody", encoding="utf-8")
    wf = load_workflow(wf_path, environ={})
    with pytest.raises(AttributeError):
        wf.prompt_template = "changed"  # type: ignore[misc]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_workflow.py -v`
Expected: FAIL with `ImportError: cannot import name 'Settings' from 'issuebot.config'`.

- [ ] **Step 3: Implement `load_workflow` and the exports**

Replace `src/issuebot/config/workflow.py` with:

```python
"""WORKFLOW.md: YAML front matter plus a Markdown prompt body."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from issuebot.config.errors import (
    ConfigError,
    FrontMatterNotAMap,
    MissingWorkflowFile,
    SettingsValidationError,
    WorkflowParseError,
)
from issuebot.config.resolve import resolve_config
from issuebot.config.settings import Settings

FRONT_MATTER_DELIMITER = "---"


@dataclass(frozen=True)
class Workflow:
    """A loaded WORKFLOW.md: typed settings plus the prompt template."""

    path: Path
    config: Settings
    prompt_template: str
    raw_config: dict[str, Any]
    source_mtime_ns: int


def parse_workflow_text(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into ``(front_matter_mapping, stripped_body)``.

    CRLF is normalised, a leading BOM is dropped, and a file without a leading ``---``
    line is treated as body only with an empty mapping.
    """
    text = text.lstrip("﻿").replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != FRONT_MATTER_DELIMITER:
        return {}, text.strip()

    end = next(
        (i for i in range(1, len(lines)) if lines[i].rstrip() == FRONT_MATTER_DELIMITER),
        None,
    )
    if end is None:
        raise WorkflowParseError("front matter opened with '---' but never closed")

    front_matter = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        raw = yaml.safe_load(front_matter)
    except yaml.YAMLError as exc:
        raise WorkflowParseError(f"invalid YAML front matter: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise FrontMatterNotAMap(f"front matter must be a mapping, got {type(raw).__name__}")
    return raw, body.strip()


def load_workflow(path: Path | str, *, environ: Mapping[str, str] | None = None) -> Workflow:
    """Read, parse, resolve and validate a WORKFLOW.md.

    Every failure is a ``ConfigError`` subclass carrying the absolute path.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    resolved_path = Path(path).expanduser().resolve()
    try:
        text = resolved_path.read_text(encoding="utf-8")
        mtime_ns = resolved_path.stat().st_mtime_ns
    except FileNotFoundError:
        raise MissingWorkflowFile(
            f"workflow file not found: {resolved_path}", path=resolved_path
        ) from None
    except OSError as exc:
        raise MissingWorkflowFile(f"workflow file unreadable: {exc}", path=resolved_path) from exc

    try:
        raw, body = parse_workflow_text(text)
        resolved = resolve_config(raw, environ=env, base_dir=resolved_path.parent)
    except ConfigError as exc:
        exc.path = resolved_path
        raise

    try:
        settings = Settings.model_validate(resolved)
    except ValidationError as exc:
        raise SettingsValidationError(_format_errors(exc), path=resolved_path) from exc

    return Workflow(
        path=resolved_path,
        config=settings,
        prompt_template=body,
        raw_config=raw,
        source_mtime_ns=mtime_ns,
    )


def _format_errors(exc: ValidationError) -> list[tuple[str, str]]:
    return [
        (".".join(str(part) for part in error["loc"]) or "<root>", error["msg"])
        for error in exc.errors()
    ]
```

Replace `src/issuebot/config/__init__.py` with:

```python
"""Configuration: WORKFLOW.md loading, environment resolution and typed settings."""

from issuebot.config.errors import (
    ConfigError,
    FrontMatterNotAMap,
    MissingEnvironmentVariable,
    MissingWorkflowFile,
    SettingsValidationError,
    WorkflowParseError,
)
from issuebot.config.settings import (
    AgentSettings,
    ClaudeSettings,
    DatabaseSettings,
    GitHubLabels,
    GitHubSettings,
    HooksSettings,
    NotificationsSettings,
    PermissionMode,
    PollingSettings,
    ServerSettings,
    Settings,
    SlackSettings,
    WorkspaceSettings,
)
from issuebot.config.workflow import Workflow, load_workflow, parse_workflow_text

__all__ = [
    "AgentSettings",
    "ClaudeSettings",
    "ConfigError",
    "DatabaseSettings",
    "FrontMatterNotAMap",
    "GitHubLabels",
    "GitHubSettings",
    "HooksSettings",
    "MissingEnvironmentVariable",
    "MissingWorkflowFile",
    "NotificationsSettings",
    "PermissionMode",
    "PollingSettings",
    "ServerSettings",
    "Settings",
    "SettingsValidationError",
    "SlackSettings",
    "Workflow",
    "WorkflowParseError",
    "WorkspaceSettings",
    "load_workflow",
    "parse_workflow_text",
]
```

- [ ] **Step 4: Run the whole suite to verify it passes**

Run: `uv run pytest -v`
Expected: all tests pass (test_workflow.py now 21 tests).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/issuebot/config tests/test_workflow.py
git commit -m "feat: add load_workflow with resolution, validation and typed errors"
```

---

### Task 9: `validate` command, dogfood `WORKFLOW.md` and `.env.example`

**Files:**
- Modify: `src/issuebot/cli.py`, `tests/test_cli.py`
- Create: `tests/fixtures/workflows/good.md`, `tests/fixtures/workflows/invalid.md`, `WORKFLOW.md`, `.env.example`

**Interfaces:**
- Consumes: `load_workflow`, `Workflow`, `Settings`, `ConfigError` (Task 8); `ENV_REF` (Task 7); `configure_logging` (Task 2).
- Produces: `issuebot validate [--workflow PATH] [--show-config]` with exit codes 0/1/2; `issuebot.cli.Check` dataclass (`subject`, `status`, `detail`, `line()`); `run_checks(workflow: Workflow) -> list[Check]`; `render_config(settings: Settings) -> str`; module-level `_which = shutil.which` for tests to substitute; global `--log-level`, `--log-format`.

- [ ] **Step 1: Create the fixtures**

`tests/fixtures/workflows/good.md`:

```markdown
---
github:
  repo: example/repo
  token: $GH_TOKEN
polling:
  interval_ms: 5000
---

You are working on `{{ issue.identifier }}`.
```

`tests/fixtures/workflows/invalid.md`:

```markdown
---
github:
  repo: example/repo
polling:
  interval_ms: 5
agnet:
  max_turns: 1
---

Body
```

- [ ] **Step 2: Replace the CLI tests with the full set**

Replace `tests/test_cli.py` with:

```python
"""Tests for the command-line entry point."""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from issuebot import __version__
from issuebot.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "workflows"
GOOD = FIXTURES / "good.md"
INVALID = FIXTURES / "invalid.md"


@pytest.fixture
def executables(monkeypatch: pytest.MonkeyPatch) -> Callable[[set[str]], None]:
    """Pretend the given executable names exist on PATH and nothing else does."""

    def install(names: set[str]) -> None:
        monkeypatch.setattr(
            "issuebot.cli._which", lambda name: f"/usr/bin/{name}" if name in names else None
        )

    install({"claude", "gh"})
    return install


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(text, encoding="utf-8")
    return path


# --- top level -------------------------------------------------------------------


def test_version_flag_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"issuebot {__version__}"


def test_no_command_prints_help_and_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage: issuebot" in capsys.readouterr().out


def test_unknown_command_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2


def test_installed_script_runs_version() -> None:
    result = subprocess.run(["issuebot", "--version"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert result.stdout.strip() == f"issuebot {__version__}"


# --- validate --------------------------------------------------------------------


def test_validate_good_workflow_exits_zero(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, executables: object
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token-value")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert f"[ OK ] workflow: {GOOD.resolve()}" in out
    assert "[ OK ] github.repo: example/repo" in out
    assert "[ OK ] github.token: set (from $GH_TOKEN)" in out
    assert "[ OK ] workspace.root: /workspaces" in out
    assert "[ OK ] claude.command: /usr/bin/claude" in out
    assert "[ OK ] gh: /usr/bin/gh" in out
    assert "[ OK ] database.url: not configured (history and dashboard disabled)" in out
    assert "[ OK ] notifications.slack: not configured" in out
    assert "[ OK ] prompt: 44 characters" in out
    assert out.rstrip().endswith("9 checks: 0 failed, 0 warnings")
    assert "secret-token-value" not in out


def test_validate_token_from_fallback_variable(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(path)]) == 0
    assert "[ OK ] github.token: set (from GH_TOKEN)" in capsys.readouterr().out


def test_validate_missing_token_fails(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, executables: object
) -> None:
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] github.token: not set; export GH_TOKEN or set github.token: $VAR" in out
    assert "1 failed" in out


def test_validate_literal_token_warns(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, executables: object
) -> None:
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n  token: ghp_literal\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] github.token: literal value in WORKFLOW.md; prefer $VAR" in out
    assert "0 failed, 1 warnings" in out


def test_validate_missing_executables_fail(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: Callable[[set[str]], None],
) -> None:
    executables(set())
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] claude.command: 'claude' not found on PATH" in out
    assert "[FAIL] gh: 'gh' not found on PATH" in out
    assert "2 failed" in out


def test_validate_custom_claude_command_is_looked_up(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: Callable[[set[str]], None],
) -> None:
    executables({"my-claude", "gh"})
    monkeypatch.setenv("GH_TOKEN", "t")
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\nclaude:\n  command: my-claude\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 0
    assert "[ OK ] claude.command: /usr/bin/my-claude" in capsys.readouterr().out


def test_validate_workspace_root_parent_missing_warns(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    root = tmp_path / "missing-parent" / "ws"
    path = _write(tmp_path, f"---\ngithub:\n  repo: o/r\nworkspace:\n  root: {root}\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 0
    assert f"[WARN] workspace.root: {root} (parent directory does not exist)" in (
        capsys.readouterr().out
    )


def test_validate_empty_prompt_warns(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\n")
    assert main(["validate", "--workflow", str(path)]) == 0
    assert "[WARN] prompt: body is empty" in capsys.readouterr().out


def test_validate_configured_database_and_slack(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executables: object,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/x")
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    assert main(["validate", "--workflow", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] database.url: configured" in out
    assert "[ OK ] notifications.slack: configured" in out


def test_validate_unloadable_workflow_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", "--workflow", str(INVALID)]) == 2
    out = capsys.readouterr().out
    assert out.startswith("[FAIL] workflow: ")
    assert "polling.interval_ms" in out
    assert "agnet" in out
    assert "checks:" not in out


def test_validate_missing_file_exits_two(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert main(["validate", "--workflow", str(tmp_path / "nope.md")]) == 2
    assert "not found" in capsys.readouterr().out


def test_validate_uses_env_workflow_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, executables: object
) -> None:
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    monkeypatch.setenv("ISSUEBOT_WORKFLOW", str(path))
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate"]) == 0


def test_validate_defaults_to_cwd_workflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, executables: object
) -> None:
    _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate"]) == 0


def test_show_config_masks_secrets(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, executables: object
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token-value")
    assert main(["validate", "--workflow", str(GOOD), "--show-config"]) == 0
    out = capsys.readouterr().out
    assert "repo: example/repo" in out
    assert "interval_ms: 5000" in out
    assert "**********" in out
    assert "secret-token-value" not in out
    assert "root: /workspaces" in out


def test_log_flags_are_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, executables: object
) -> None:
    path = _write(tmp_path, "---\ngithub:\n  repo: o/r\n---\nBody")
    monkeypatch.setenv("GH_TOKEN", "t")
    flags = ["--log-level", "DEBUG", "--log-format", "console"]
    assert main([*flags, "validate", "--workflow", str(path)]) == 0
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: the four top-level tests pass; every `validate` test fails with `SystemExit: 2` (argparse rejects the unknown `validate` command) or an assertion on output.

- [ ] **Step 4: Implement the full CLI**

Replace `src/issuebot/cli.py` with:

```python
"""Command-line entry point for issuebot."""

import argparse
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from issuebot import __version__
from issuebot.config import ConfigError, Settings, Workflow, load_workflow
from issuebot.config.resolve import ENV_REF
from issuebot.log import configure_logging

DEFAULT_WORKFLOW = "WORKFLOW.md"

# Module-level reference so tests can substitute the executable lookup.
_which = shutil.which

CheckStatus = Literal["ok", "warn", "fail"]
_TAGS: dict[CheckStatus, str] = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}


@dataclass(frozen=True)
class Check:
    subject: str
    status: CheckStatus
    detail: str

    def line(self) -> str:
        return f"{_TAGS[self.status]} {self.subject}: {self.detail}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issuebot",
        description="Issue-to-PR agent orchestrator for GitHub and Claude.",
    )
    parser.add_argument("--version", action="version", version=f"issuebot {__version__}")
    parser.add_argument(
        "--log-level",
        default=None,
        help="DEBUG, INFO, WARNING or ERROR (default: $ISSUEBOT_LOG_LEVEL or INFO)",
    )
    parser.add_argument(
        "--log-format",
        choices=["json", "console"],
        default=None,
        help="log line format (default: $ISSUEBOT_LOG_FORMAT or json)",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    validate = subparsers.add_parser(
        "validate", help="load WORKFLOW.md and check the runtime environment"
    )
    validate.add_argument(
        "--workflow",
        type=Path,
        default=None,
        help="path to WORKFLOW.md (default: $ISSUEBOT_WORKFLOW or ./WORKFLOW.md)",
    )
    validate.add_argument(
        "--show-config", action="store_true", help="print the effective configuration as YAML"
    )
    validate.set_defaults(func=cmd_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(
        level=args.log_level or os.environ.get("ISSUEBOT_LOG_LEVEL", "INFO"),
        fmt=args.log_format or os.environ.get("ISSUEBOT_LOG_FORMAT", "json"),
    )
    if args.command is None:
        parser.print_help()
        return 2
    return int(args.func(args))


def workflow_path(explicit: Path | None, environ: Mapping[str, str]) -> Path:
    if explicit is not None:
        return explicit
    return Path(environ.get("ISSUEBOT_WORKFLOW") or DEFAULT_WORKFLOW)


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        workflow = load_workflow(workflow_path(args.workflow, os.environ))
    except ConfigError as exc:
        print(f"[FAIL] workflow: {exc}")
        return 2

    checks = run_checks(workflow)
    for check in checks:
        print(check.line())
    failed = sum(check.status == "fail" for check in checks)
    warned = sum(check.status == "warn" for check in checks)
    print(f"{len(checks)} checks: {failed} failed, {warned} warnings")
    if args.show_config:
        print(render_config(workflow.config), end="")
    return 1 if failed else 0


def run_checks(workflow: Workflow) -> list[Check]:
    cfg = workflow.config
    checks = [
        Check("workflow", "ok", str(workflow.path)),
        Check("github.repo", "ok", cfg.github.repo),
        _token_check(workflow),
        _workspace_check(cfg.workspace.root),
        _executable_check("claude.command", cfg.claude.command),
        _executable_check("gh", "gh"),
        Check(
            "database.url",
            "ok",
            "configured" if cfg.database.url else "not configured (history and dashboard disabled)",
        ),
        Check(
            "notifications.slack",
            "ok",
            "configured" if cfg.notifications.slack.webhook_url else "not configured",
        ),
    ]
    body = workflow.prompt_template
    if body:
        checks.append(Check("prompt", "ok", f"{len(body)} characters"))
    else:
        checks.append(Check("prompt", "warn", "body is empty"))
    return checks


def _token_check(workflow: Workflow) -> Check:
    if workflow.config.github.token is None:
        return Check("github.token", "fail", "not set; export GH_TOKEN or set github.token: $VAR")
    raw_github = workflow.raw_config.get("github")
    raw_token = raw_github.get("token") if isinstance(raw_github, dict) else None
    if raw_token is None:
        return Check("github.token", "ok", "set (from GH_TOKEN)")
    if isinstance(raw_token, str) and ENV_REF.match(raw_token):
        return Check("github.token", "ok", f"set (from {raw_token})")
    return Check("github.token", "warn", "literal value in WORKFLOW.md; prefer $VAR")


def _workspace_check(root: Path) -> Check:
    if root.parent.is_dir():
        return Check("workspace.root", "ok", str(root))
    return Check("workspace.root", "warn", f"{root} (parent directory does not exist)")


def _executable_check(subject: str, command: str) -> Check:
    found = _which(command)
    if found:
        return Check(subject, "ok", found)
    return Check(subject, "fail", f"{command!r} not found on PATH")


def render_config(settings: Settings) -> str:
    return yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 19 passed.

- [ ] **Step 6: Add the dogfood `WORKFLOW.md` and `.env.example`**

`WORKFLOW.md` (repository root):

```markdown
---
github:
  repo: jleavers/issuebot
  token: $GH_TOKEN
polling:
  interval_ms: 30000
workspace:
  root: /workspaces
agent:
  max_concurrent_agents: 2
  max_turns: 5
  max_attempts: 3
claude:
  permission_mode: auto
  max_budget_usd: 5.0
notifications:
  slack:
    events: [state_changed, blocked]
---

You are working on GitHub issue `{{ issue.identifier }}`: {{ issue.title }}.

This body is a placeholder. The full workflow prompt lands in Phase 3
(see docs/superpowers/specs/2026-09-02-issuebot-phased-design.md).
```

`.env.example`:

```bash
# Copy to .env (git-ignored) and fill in. compose.yaml loads it for the worker.

# Dedicated, repo-scoped GitHub token (contents, issues, pull requests: write).
GH_TOKEN=

# Claude auth: an API key, or leave empty and log in once inside the container
# (the /home/issuebot/.claude volume keeps the login).
ANTHROPIC_API_KEY=

# Optional: Slack incoming webhook for notifications (Phase 5).
SLACK_WEBHOOK_URL=
```

Run: `GH_TOKEN=x uv run issuebot validate`
Expected: `[ OK ]` for every line except possibly `claude.command`/`gh` on hosts without them; summary printed; exit 0 when both executables are present.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files
git add src/issuebot/cli.py tests/test_cli.py tests/fixtures WORKFLOW.md .env.example
git commit -m "feat: add issuebot validate with environment checks and --show-config"
```

---

### Task 10: Container image and compose stack

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `compose.yaml`

**Interfaces:**
- Produces: image with `git`, `gh`, `claude`, the app venv, non-root user `issuebot`, `ENTRYPOINT ["issuebot"]`, `CMD ["validate"]`; compose services `db` (postgres:18) and `worker`; named volumes `pgdata`, `workspaces`, `claude-home`.

- [ ] **Step 1: Write `.dockerignore`**

```
.git
.venv
.claude
.superpowers
.pytest_cache
.ruff_cache
**/__pycache__
*.pyc
docs
tests
.env
.env.*
!.env.example
```

- [ ] **Step 2: Write the `Dockerfile`**

```dockerfile
# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.14-slim

FROM ghcr.io/astral-sh/uv:0.11.17 AS uv

# ---------------------------------------------------------------- builder
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
ARG CLAUDE_CODE_VERSION=2.1.258
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /bin/bash issuebot \
 && install -d -o issuebot -g issuebot /workspaces /home/issuebot/.claude /app

COPY --from=builder --chown=issuebot:issuebot /app /app

USER issuebot
ENV HOME=/home/issuebot \
    PATH="/home/issuebot/.local/bin:/app/.venv/bin:${PATH}"

RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}" \
 && claude --version

WORKDIR /app
VOLUME ["/workspaces", "/home/issuebot/.claude"]

LABEL org.opencontainers.image.source="https://github.com/jleavers/issuebot" \
      org.opencontainers.image.description="issuebot: issue-to-PR agent orchestrator"

ENTRYPOINT ["issuebot"]
CMD ["validate"]
```

- [ ] **Step 3: Build and smoke-test the image**

Run: `docker build -t issuebot:dev .`
Expected: build succeeds; the `claude --version` layer prints `2.1.258 (Claude Code)`.

Run: `docker run --rm issuebot:dev --version`
Expected: `issuebot 0.1.0`.

Run: `docker run --rm -e GH_TOKEN=x -v "$PWD/WORKFLOW.md:/app/WORKFLOW.md:ro" issuebot:dev validate`
Expected: nine `[ OK ]` lines (both executables found inside the image) and `9 checks: 0 failed, 0 warnings`; exit 0.

Run: `docker run --rm --entrypoint id issuebot:dev`
Expected: `uid=1000(issuebot)`.

- [ ] **Step 4: Write `compose.yaml`**

```yaml
services:
  db:
    image: postgres:18
    environment:
      POSTGRES_USER: issuebot
      POSTGRES_PASSWORD: issuebot
      POSTGRES_DB: issuebot
    volumes:
      # PostgreSQL 18 moved PGDATA to /var/lib/postgresql/18/docker; mount the parent.
      - pgdata:/var/lib/postgresql
    ports:
      - "127.0.0.1:5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U issuebot -d issuebot"]
      interval: 5s
      timeout: 5s
      retries: 10

  worker:
    build: .
    # Phase 4 replaces this with ["worker"]. Until then `docker compose up` validates config.
    command: ["validate"]
    env_file:
      - path: .env
        required: false
    environment:
      DATABASE_URL: postgresql://issuebot:issuebot@db:5432/issuebot
    volumes:
      - ./WORKFLOW.md:/app/WORKFLOW.md:ro
      - workspaces:/workspaces
      - claude-home:/home/issuebot/.claude
    depends_on:
      db:
        condition: service_healthy

volumes:
  pgdata:
  workspaces:
  claude-home:
```

- [ ] **Step 5: Validate and run the stack**

Run: `docker compose config --quiet && echo OK`
Expected: `OK`.

Run: `docker compose up --build --abort-on-container-exit worker`
Expected: `db` reports healthy; `worker` prints the validate report (`[FAIL] github.token` unless `.env` provides `GH_TOKEN`, `[ OK ] database.url: configured`) and exits; compose stops. Then `docker compose down`.

- [ ] **Step 6: Commit**

```bash
uv run pre-commit run --all-files
git add Dockerfile .dockerignore compose.yaml
git commit -m "feat: add Docker image and compose stack with PostgreSQL 18"
```

---

### Task 11: CI workflow and Dependabot

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/dependabot.yml`

**Interfaces:**
- Produces: jobs `lint`, `test`, `docker` on pushes to `main` and on pull requests; weekly Dependabot for `uv`, `docker`, `github-actions`.

- [ ] **Step 1: Write `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10
        with:
          enable-cache: true
      - run: uv python install
      - run: uv sync --frozen
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run pre-commit run --all-files --show-diff-on-failure

  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:18
        env:
          POSTGRES_USER: issuebot
          POSTGRES_PASSWORD: issuebot
          POSTGRES_DB: issuebot
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U issuebot -d issuebot"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    env:
      DATABASE_URL: postgresql://issuebot:issuebot@localhost:5432/issuebot
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10
        with:
          enable-cache: true
      - run: uv python install
      - run: uv sync --frozen
      - run: uv run pytest

  docker:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: docker/setup-buildx-action@v4
      - uses: docker/build-push-action@v7
        with:
          context: .
          push: false
          load: true
          tags: issuebot:ci
          cache-from: type=gha
          cache-to: type=gha,mode=max
      - run: docker run --rm issuebot:ci --version
```

- [ ] **Step 2: Write `.github/dependabot.yml`**

```yaml
version: 2
updates:
  - package-ecosystem: uv
    directory: /
    schedule:
      interval: weekly
    groups:
      python-minor-and-patch:
        patterns: ["*"]
        update-types: ["minor", "patch"]

  - package-ecosystem: docker
    directory: /
    schedule:
      interval: weekly

  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: weekly
    groups:
      actions:
        patterns: ["*"]
```

- [ ] **Step 3: Check the YAML and mirror the lint job locally**

Run: `uv run pre-commit run check-yaml --all-files && uv run ruff check . && uv run ruff format --check . && uv run pytest -q`
Expected: hooks pass, lint clean, all tests pass. (The workflow itself runs on the PR opened in Task 13.)

- [ ] **Step 4: Commit**

```bash
git add .github
git commit -m "ci: add lint, test and docker build workflow plus Dependabot"
```

---

### Task 12: Repository documentation

**Files:**
- Modify: `CLAUDE.md`, `README.md`

- [ ] **Step 1: Replace the "Repository state" section of `CLAUDE.md`**

Replace the section starting at `## Repository state` and ending just before `## What issuebot is` with:

````markdown
## Commands

Python 3.14 with `uv`; `src` layout; package `issuebot`.

```bash
uv sync                              # create .venv and install (uses uv.lock)
uv run pytest                        # tests (hermetic; no network, no Docker)
uv run pytest tests/test_cli.py -k validate   # one file / one pattern
uv run ruff check . && uv run ruff format --check .
uv run pre-commit run --all-files    # whitespace, yaml, ruff (same as CI lint job)
uv run issuebot validate             # load ./WORKFLOW.md and check the environment
docker compose build                 # image: git, gh, claude, app venv
docker compose up                    # db (postgres:18) + worker (runs validate until Phase 4)
```

CI (`.github/workflows/ci.yml`) runs lint, tests (with a postgres:18 service) and
a Docker build on every PR. Dependabot covers uv, Docker and Actions weekly.

## Package layout

- `issuebot.config`: `load_workflow(path)` → `Workflow(config: Settings, prompt_template,
  raw_config, path, source_mtime_ns)`. Front matter → `$VAR`/`~`/relative-path
  resolution (`resolve.py`, designated fields only) → pydantic `Settings`
  (`settings.py`, `extra="forbid"`). Errors are `ConfigError` subclasses with a `code`.
- `issuebot.log`: `configure_logging()` (structlog, JSON to stderr by default),
  `get_logger()`, `bind_issue_context()`, `bind_session_context()`, `clear_context()`.
- `issuebot.events`: frozen dataclass events (`EVENT_KINDS`), `EventBus.publish()`
  (synchronous, sink failures isolated and counted), `LogSink`.
- `issuebot.cli`: argparse; `issuebot validate [--workflow PATH] [--show-config]`
  exits 0/1/2 (ok / failed checks / workflow unloadable).

Design documents: `docs/superpowers/specs/` (phased design and one spec per phase),
`docs/superpowers/plans/` (one implementation plan per phase).
````

- [ ] **Step 2: Add a Development section to `README.md`**

Append to `README.md`:

````markdown

## Development

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.14 for you) and,
for the container stack, Docker with Compose.

```bash
uv sync
uv run pytest
uv run issuebot validate          # checks ./WORKFLOW.md and the environment
cp .env.example .env              # then fill in GH_TOKEN and Claude auth
docker compose up --build         # postgres:18 + worker
```

The design lives in [`docs/superpowers/specs/`](docs/superpowers/specs/); start with
the phased design, then the per-phase specs and plans.
````

- [ ] **Step 3: Verify the documented commands work**

Run: `uv run pytest -q && uv run pre-commit run --all-files`
Expected: all pass; `end-of-file-fixer` and `trailing-whitespace` clean.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: document commands and package layout for Phase 1"
```

---

### Task 13: Push and open the pull request

**Files:** none.

- [ ] **Step 1: Final full check**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files && uv run pytest -q && docker compose config --quiet`
Expected: everything passes.

- [ ] **Step 2: Push the branch**

Run: `git push -u origin phase-1-foundations`

- [ ] **Step 3: Write the PR body to a temporary file (separate shell call from Step 4)**

Write `/tmp/issuebot-phase-1-pr.md` with the text below, appending any session link
the executing harness requires after the generated-with line:

```markdown
## Phase 1: Foundations

Implements `docs/superpowers/specs/2026-09-02-phase-1-foundations-design.md`.

- `uv`-managed Python 3.14 project (`src/issuebot`), ruff, pytest, pre-commit revs bumped
- `issuebot.config`: `WORKFLOW.md` parser, `$VAR`/`~`/relative-path resolution, pydantic `Settings` (unknown keys rejected), typed `ConfigError`s
- `issuebot.log`: structlog JSON to stderr with issue/session context
- `issuebot.events`: frozen event types, `EventBus` with sink failure isolation, `LogSink`
- `issuebot validate [--workflow] [--show-config]` with exit codes 0/1/2
- Dockerfile (git, gh, Claude Code 2.1.258, non-root), `compose.yaml` with postgres:18
- CI: lint, tests with a postgres:18 service, docker build; Dependabot for uv, docker, actions
- Dogfood `WORKFLOW.md` with a placeholder prompt (Phase 3 replaces it)

No call to `gh` or `claude` is made by the code in this phase.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

- [ ] **Step 4: Open the PR via the REST API (the CLI's `pr create` is blocked in this repo)**

```bash
gh api repos/jleavers/issuebot/pulls -X POST \
  -f title='Phase 1: Foundations' -f head='phase-1-foundations' -f base='main' \
  -F body=@/tmp/issuebot-phase-1-pr.md
```

Then confirm: `gh pr view --json title,body --jq '.title'` prints `Phase 1: Foundations`.
CI must be green before handing over for human review. Do not merge.
