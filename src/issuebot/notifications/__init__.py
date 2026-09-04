"""Notification sinks: Slack incoming webhook (Phase 5)."""

from issuebot.notifications.messages import format_duration, format_event, issue_link, pr_link
from issuebot.notifications.slack import (
    DRAIN_TIMEOUT_S,
    MAX_ATTEMPTS,
    POST_TIMEOUT_S,
    QUEUE_LIMIT,
    REDACTED,
    RETRY_AFTER_CAP_S,
    RETRY_DELAYS_S,
    Poster,
    PostResult,
    SlackSink,
    redact,
    slack_payload,
    subscribed_kinds,
    urllib_post,
)

__all__ = [
    "DRAIN_TIMEOUT_S",
    "MAX_ATTEMPTS",
    "POST_TIMEOUT_S",
    "QUEUE_LIMIT",
    "REDACTED",
    "RETRY_AFTER_CAP_S",
    "RETRY_DELAYS_S",
    "PostResult",
    "Poster",
    "SlackSink",
    "format_duration",
    "format_event",
    "issue_link",
    "pr_link",
    "redact",
    "slack_payload",
    "subscribed_kinds",
    "urllib_post",
]
