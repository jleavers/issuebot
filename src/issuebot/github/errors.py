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


class PageCeilingError(GitHubError):
    """A read that stopped at its page ceiling rather than at the end of the resource (#139).

    A ``response`` error like any other -- GitHub answered, and the answer is one issuebot
    refuses -- but a *named* one, so a caller that treats reaching a ceiling differently from
    every other refused response can tell them apart. Only ``GhCliAdapter._collect`` does:
    ``response`` also covers a GraphQL errors payload, which is what a server-side query
    timeout arrives as, and catching the category there would have isolated far more than the
    cap it means to.
    """

    def __init__(self, message: str) -> None:
        super().__init__("response", message)
