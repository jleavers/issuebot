"""GitHub integration: normalised issue model, the label state machine and adapters."""

from issuebot.github.adapter import GitHubAdapter
from issuebot.github.errors import ErrorCategory, GitHubError
from issuebot.github.fake import FakeGitHub
from issuebot.github.ghcli import GhCliAdapter
from issuebot.github.models import (
    WORKPAD_MARKER,
    AuthStatus,
    Comment,
    Issue,
    LabelEnsured,
    LinkedPr,
    RateLimit,
    RepoInfo,
    StateLabel,
)
from issuebot.github.normalise import issue_from_node, label_name, repo_short_name, role_for
from issuebot.github.runner import GhResult, GhRunner, GhRunnerLike
from issuebot.github.state import (
    ACTIVE_STATES,
    LABEL_STYLES,
    TERMINAL_STATES,
    TRANSITIONS,
    Actor,
    LabelStyle,
    classify_closed,
    is_active,
    is_allowed,
    is_terminal,
    next_state_for,
)

__all__ = [
    "ACTIVE_STATES",
    "LABEL_STYLES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "WORKPAD_MARKER",
    "Actor",
    "AuthStatus",
    "Comment",
    "ErrorCategory",
    "FakeGitHub",
    "GhCliAdapter",
    "GhResult",
    "GhRunner",
    "GhRunnerLike",
    "GitHubAdapter",
    "GitHubError",
    "Issue",
    "LabelEnsured",
    "LabelStyle",
    "LinkedPr",
    "RateLimit",
    "RepoInfo",
    "StateLabel",
    "classify_closed",
    "is_active",
    "is_allowed",
    "is_terminal",
    "issue_from_node",
    "label_name",
    "next_state_for",
    "repo_short_name",
    "role_for",
]
