"""Shared pytest fixtures."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from issuebot.github.models import Issue, StateLabel

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


@pytest.fixture
def make_issue() -> Callable[..., Issue]:
    """Build a consistent Issue; pass field overrides as keyword arguments."""

    def factory(**overrides: Any) -> Issue:
        fields: dict[str, Any] = {
            "id": "42",
            "identifier": "repo-42",
            "number": 42,
            "title": "Add retry backoff",
            "body": None,
            "github_state": "open",
            "state": StateLabel.TODO,
            "state_labels": ("issuebot/todo",),
            "labels": ("issuebot/todo",),
            "url": "https://github.com/example/repo/issues/42",
            "assignees": (),
            "created_at": datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
            "updated_at": datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            "closed_at": None,
            "linked_pr": None,
            "dispatchable": True,
        }
        fields.update(overrides)
        return Issue(**fields)

    return factory
