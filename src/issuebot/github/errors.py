"""One error type for every GitHub failure, with a stable category for logs and retries."""

from typing import Literal

ErrorCategory = Literal[
    "auth", "not_found", "rate_limited", "transport", "status", "response", "config"
]

RETRYABLE_CATEGORIES: frozenset[str] = frozenset({"rate_limited", "transport"})
_STDERR_LIMIT = 500


class GitHubError(Exception):
    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        exit_code: int | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(f"{category}: {message}")
        self.category: ErrorCategory = category
        self.message = message
        self.exit_code = exit_code
        self.stderr = stderr[:_STDERR_LIMIT] if stderr else None

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES
