"""WORKFLOW.md: YAML front matter plus a Markdown prompt body."""

from typing import Any

import yaml

from issuebot.config.errors import FrontMatterNotAMap, WorkflowParseError

FRONT_MATTER_DELIMITER = "---"


def parse_workflow_text(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into ``(front_matter_mapping, stripped_body)``.

    CRLF is normalised, a leading BOM is dropped, and a file without a leading ``---``
    line is treated as body only with an empty mapping.
    """
    text = text.lstrip("﻿").replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != FRONT_MATTER_DELIMITER:
        return {}, text.strip()

    end = next(
        (i for i in range(1, len(lines)) if lines[i].rstrip() == FRONT_MATTER_DELIMITER),
        None,
    )
    if end is None:
        raise WorkflowParseError("front matter opened with '---' but never closed")

    front_matter = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        raw = yaml.safe_load(front_matter)
    except yaml.YAMLError as exc:
        raise WorkflowParseError(f"invalid YAML front matter: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise FrontMatterNotAMap(f"front matter must be a mapping, got {type(raw).__name__}")
    return raw, body.strip()
