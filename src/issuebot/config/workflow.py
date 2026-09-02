"""WORKFLOW.md: YAML front matter plus a Markdown prompt body."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from issuebot.config.errors import (
    ConfigError,
    FrontMatterNotAMap,
    MissingWorkflowFile,
    SettingsValidationError,
    WorkflowParseError,
)
from issuebot.config.resolve import resolve_config
from issuebot.config.settings import Settings

FRONT_MATTER_DELIMITER = "---"


@dataclass(frozen=True)
class Workflow:
    """A loaded WORKFLOW.md: typed settings plus the prompt template."""

    path: Path
    config: Settings
    prompt_template: str
    raw_config: dict[str, Any]
    source_mtime_ns: int


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


def load_workflow(path: Path | str, *, environ: Mapping[str, str] | None = None) -> Workflow:
    """Read, parse, resolve and validate a WORKFLOW.md.

    Every failure is a ``ConfigError`` subclass carrying the absolute path.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    resolved_path = Path(path).expanduser().resolve()
    try:
        text = resolved_path.read_text(encoding="utf-8")
        mtime_ns = resolved_path.stat().st_mtime_ns
    except FileNotFoundError:
        raise MissingWorkflowFile(
            f"workflow file not found: {resolved_path}", path=resolved_path
        ) from None
    except OSError as exc:
        raise MissingWorkflowFile(f"workflow file unreadable: {exc}", path=resolved_path) from exc

    try:
        raw, body = parse_workflow_text(text)
        resolved = resolve_config(raw, environ=env, base_dir=resolved_path.parent)
    except ConfigError as exc:
        exc.path = resolved_path
        raise

    try:
        settings = Settings.model_validate(resolved)
    except ValidationError as exc:
        raise SettingsValidationError(_format_errors(exc), path=resolved_path) from exc

    return Workflow(
        path=resolved_path,
        config=settings,
        prompt_template=body,
        raw_config=raw,
        source_mtime_ns=mtime_ns,
    )


def _format_errors(exc: ValidationError) -> list[tuple[str, str]]:
    return [
        (".".join(str(part) for part in error["loc"]) or "<root>", error["msg"])
        for error in exc.errors()
    ]
