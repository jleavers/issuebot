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
