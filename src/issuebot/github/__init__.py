"""GitHub integration: normalised issue model, the label state machine and adapters."""

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
    "next_state_for",
]
