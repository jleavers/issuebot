"""One error type for every GitHub failure, with a stable category for logs and retries."""

import re
from typing import Literal

ErrorCategory = Literal[
    "auth", "not_found", "rate_limited", "transport", "status", "response", "config"
]

RETRYABLE_CATEGORIES: frozenset[str] = frozenset({"rate_limited", "transport"})
_STDERR_LIMIT = 500

# How a failed `gh` invocation says what went wrong: exit code 4 is gh's own "authentication
# required", and otherwise its stderr decides, first rule to match. Shared by the adapter and
# the dashboard's billing reads, so the two cannot drift on what a 404 is.
RATE_LIMITED = re.compile(r"http 429|rate limit|secondary rate")
ERROR_RULES: tuple[tuple[ErrorCategory, re.Pattern[str]], ...] = (
    ("auth", re.compile(r"http 401|bad credentials|authentication|gh auth login")),
    ("not_found", re.compile(r"http 404|could not resolve to|\bnot found\b")),
    ("rate_limited", RATE_LIMITED),
    (
        "transport",
        re.compile(r"http 5\d\d|connection|could not resolve host|timeout|\btls\b|dial tcp"),
    ),
    ("auth", re.compile(r"http 403")),
)


def categorise(returncode: int, stderr: str) -> ErrorCategory:
    """The category of a failed ``gh`` invocation; ``status`` when nothing more is known."""
    if returncode == 4:
        return "auth"
    lowered = stderr.lower()
    for candidate, pattern in ERROR_RULES:
        if pattern.search(lowered):
            return candidate
    return "status"


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
    every other refused response can tell them apart. Two do. ``GhCliAdapter._collect``:
    ``response`` also covers a GraphQL errors payload, which is what a server-side query
    timeout arrives as, and catching the category there would have isolated far more than the
    cap it means to. And the orchestrator's approval check (GHSA-jm8h-q3j6-p8xp), which hands
    an issue whose history runs past the ceiling back to a human rather than failing closed
    and reading the same pages again on every tick.
    """

    def __init__(self, message: str) -> None:
        super().__init__("response", message)
