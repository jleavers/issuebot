"""Agent-side failure categories shared by the runner, the workspace manager and the session."""

from typing import Literal

from issuebot.events.types import RunOutcome

AgentErrorCategory = Literal[
    "claude_not_found",
    "invalid_workspace_cwd",
    "turn_timeout",
    "process_exit",
    "turn_failed",
    "budget_exceeded",
    "prompt_error",
    "workspace_error",
    "hook_error",
    "github_error",
    "cancelled",
]

_OUTCOMES: dict[str, RunOutcome] = {"turn_timeout": "timed_out", "cancelled": "cancelled"}


class AgentError(Exception):
    def __init__(self, category: AgentErrorCategory, message: str) -> None:
        super().__init__(f"{category}: {message}")
        self.category: AgentErrorCategory = category
        self.message = message


def outcome_for(category: AgentErrorCategory) -> RunOutcome:
    """The RunOutcome a failed run reports for this category."""
    return _OUTCOMES.get(category, "failed")
