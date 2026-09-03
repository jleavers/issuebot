"""Tests for agent error categories."""

import pytest

from issuebot.agent.errors import AgentError, outcome_for


def test_agent_error_carries_category_and_message() -> None:
    error = AgentError("turn_failed", "boom")
    assert error.category == "turn_failed"
    assert error.message == "boom"
    assert str(error) == "turn_failed: boom"


@pytest.mark.parametrize(
    ("category", "outcome"),
    [
        ("turn_timeout", "timed_out"),
        ("cancelled", "cancelled"),
        ("process_exit", "failed"),
        ("workspace_error", "failed"),
        ("budget_exceeded", "failed"),
    ],
)
def test_outcome_for(category: str, outcome: str) -> None:
    assert outcome_for(category) == outcome  # type: ignore[arg-type]
