"""The one UTC timestamp format the orchestrator writes for a person to read (#322).

Its own module because ``actions`` imports ``approval``, so ``approval`` cannot import the
helper from ``actions`` without a cycle, and a private copy in each can drift: the copy
``approval`` kept printed a non-UTC datetime's wall-clock time and still marked it ``Z``.
"""

from datetime import UTC, datetime


def utc_stamp(now: datetime) -> str:
    """``now`` as the UTC timestamp every ``### Issuebot ...`` heading carries."""
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
