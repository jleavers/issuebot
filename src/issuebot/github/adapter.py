"""The protocol every GitHub implementation satisfies (the gh-backed adapter and the fake)."""

from collections.abc import Iterable, Mapping
from typing import Protocol

from issuebot.config import GitHubLabels
from issuebot.github.models import (
    AuthStatus,
    Comment,
    Issue,
    LabelEnsured,
    RateLimit,
    RepoInfo,
    StateLabel,
)
from issuebot.github.state import LabelStyle


class GitHubAdapter(Protocol):
    """Reads and writes for one repository. Every method may raise GitHubError."""

    @property
    def repo(self) -> str: ...

    @property
    def labels(self) -> GitHubLabels: ...

    async def fetch_issues_by_states(self, states: Iterable[StateLabel]) -> list[Issue]:
        """Open issues carrying any of the given state labels; [] for an empty input."""
        ...

    async def fetch_issues_by_ids(self, ids: Iterable[str]) -> list[Issue]:
        """Current snapshots; ids that no longer resolve to an issue are omitted."""
        ...

    async def fetch_terminal_issues(self) -> list[Issue]:
        """Closed issues that still carry any state label."""
        ...

    async def set_state(self, number: int, state: StateLabel) -> None:
        """Add the target state label and remove every other state label."""
        ...

    async def clear_state(self, number: int) -> None:
        """Remove every state label."""
        ...

    async def comment(self, number: int, body: str) -> Comment: ...

    async def find_workpad_comment(self, number: int) -> Comment | None: ...

    async def update_comment(self, comment_id: int, body: str) -> Comment: ...

    async def ensure_labels(
        self, extra: Mapping[str, LabelStyle] | None = None
    ) -> list[LabelEnsured]:
        """Create or update the five state labels and any extra ones; idempotent."""
        ...

    async def missing_labels(self) -> list[str]:
        """Names of the configured state labels that do not exist in the repository."""
        ...

    async def rate_limit(self) -> RateLimit: ...

    async def auth_status(self) -> AuthStatus: ...

    async def repo_info(self) -> RepoInfo: ...
