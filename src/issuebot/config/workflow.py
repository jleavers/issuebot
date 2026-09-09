"""WORKFLOW.md: YAML front matter plus a Markdown prompt body, and its local overlay."""

import copy
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    """A loaded WORKFLOW.md: typed settings plus the prompt template.

    ``raw_config`` is the front matter the settings were validated from: the base file's
    with the overlay's merged over it when there is one, since that is the mapping that
    explains ``config``. ``overlay_config`` is the overlay's own front matter, ``{}`` without
    one; ``overlay_path`` is the file it came from, ``None`` without one.
    """

    path: Path
    config: Settings
    prompt_template: str
    raw_config: dict[str, Any]
    source_mtime_ns: int
    # The device and inode the settings were read from. A watcher that keys only on the
    # mtime misses a file replaced by an atomic save (write a temporary file, rename it
    # over the original) that carries an mtime it already had -- a restore from an archive
    # or a checkout that preserves timestamps. Default 0 so a hand-built Workflow in a test
    # need not invent one; 0 for both is "unknown", and never equal to a real stat.
    source_dev: int = 0
    source_ino: int = 0
    # The overlay's identity, the same shape for the same reasons; all zero when there is
    # no overlay, and never equal to a real stat.
    overlay_path: Path | None = None
    overlay_mtime_ns: int = 0
    overlay_dev: int = 0
    overlay_ino: int = 0
    overlay_config: dict[str, Any] = field(default_factory=dict)

    @property
    def source_identity(self) -> tuple[int, int, int]:
        """``(dev, ino, mtime_ns)`` of the file this was read from.

        The whole triple, not the mtime alone: the file the path names now is the same
        file only if all three match.
        """
        return (self.source_dev, self.source_ino, self.source_mtime_ns)

    @property
    def overlay_identity(self) -> tuple[int, int, int]:
        """``(dev, ino, mtime_ns)`` of the overlay, or all zero when there was none."""
        return (self.overlay_dev, self.overlay_ino, self.overlay_mtime_ns)


def overlay_path_for(path: Path) -> Path:
    """The local overlay's path: ``WORKFLOW.md`` -> ``WORKFLOW.local.md``, beside it.

    Derived, never configured. A sibling of a file inside a mounted directory is reached
    through the same directory entry, so it live-reloads for the same reason the base does
    (#46); an overlay that could be pointed anywhere could be pinned to an inode again.
    There is no chaining: an overlay's own overlay would be ``WORKFLOW.local.local.md``.
    """
    return path.with_name(f"{path.stem}.local{path.suffix}")


def merge_front_matter(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """The overlay merged over the base, as a new mapping; neither argument is touched.

    Three rules. Two mappings merge key by key, recursively. Anything else replaces: a
    scalar, a list as a whole (an event allow-list is a choice, not an accumulation), and
    a mapping on one side where the other holds a scalar or a list. An explicit ``null`` in
    the overlay deletes the key, so the setting falls back to its ``Settings`` default; a
    ``null`` naming a key the base does not set is a no-op, at any depth, so a mapping that
    replaces a scalar or fills a section the base lacks still sheds its ``null`` leaves.
    """
    merged: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, Mapping):
            current = merged.get(key)
            merged[key] = merge_front_matter(current if isinstance(current, Mapping) else {}, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def count_overrides(overlay: Mapping[str, Any]) -> int:
    """How many leaf keypaths the overlay sets, a ``null`` (a delete) counting as one."""
    return sum(
        count_overrides(value) if isinstance(value, Mapping) else 1 for value in overlay.values()
    )


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


def load_workflow(
    path: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
    overlay: bool = True,
) -> Workflow:
    """Read, parse, resolve and validate a WORKFLOW.md, with its local overlay if one exists.

    The overlay is the sibling ``overlay_path_for`` names. Its front matter is merged over
    the base's (``merge_front_matter``) before resolution, so a fallback ``resolve_config``
    fills in for an absent field can never clobber a value the other file set, and the
    merged mapping is validated once, so a typo in either file fails the same way. Its
    body replaces the base's only when it has one. ``overlay=False`` ignores it: for a test
    of the repository's own ``configs/WORKFLOW.md``, which is exactly where a developer
    keeps their overlay.

    Every failure is a ``ConfigError`` subclass carrying the absolute path of the file it
    is about, or of the base with the overlay named beside it when it is about the merge.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    resolved_path = Path(path).expanduser().resolve()
    try:
        # Stat before read: a racing rewrite then yields content at least as new as
        # the recorded mtime, so the next reload sees the change.
        source = resolved_path.stat()
        text = resolved_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise MissingWorkflowFile(
            f"workflow file not found: {resolved_path}", path=resolved_path
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise MissingWorkflowFile(f"workflow file unreadable: {exc}", path=resolved_path) from exc

    try:
        raw, body = parse_workflow_text(text)
    except ConfigError as exc:
        exc.path = resolved_path
        raise

    local_path: Path | None = None
    local: os.stat_result | None = None
    local_raw: dict[str, Any] = {}
    if overlay:
        local_path, local, local_raw, local_body = _read_overlay(
            overlay_path_for(resolved_path), base=resolved_path
        )
        if local_body:
            body = local_body
    merged = merge_front_matter(raw, local_raw) if local_path is not None else raw

    try:
        resolved = resolve_config(merged, environ=env, base_dir=resolved_path.parent)
    except ConfigError as exc:
        exc.path = resolved_path
        exc.overlay = local_path
        raise

    try:
        settings = Settings.model_validate(resolved)
    except ValidationError as exc:
        raise SettingsValidationError(
            _format_errors(exc), path=resolved_path, overlay=local_path
        ) from exc

    return Workflow(
        path=resolved_path,
        config=settings,
        prompt_template=body,
        raw_config=merged,
        source_mtime_ns=source.st_mtime_ns,
        source_dev=source.st_dev,
        source_ino=source.st_ino,
        overlay_path=local_path,
        overlay_mtime_ns=local.st_mtime_ns if local is not None else 0,
        overlay_dev=local.st_dev if local is not None else 0,
        overlay_ino=local.st_ino if local is not None else 0,
        overlay_config=local_raw,
    )


def _read_overlay(
    overlay_path: Path, *, base: Path
) -> tuple[Path | None, os.stat_result | None, dict[str, Any], str]:
    """``(path, stat, front_matter, body)`` of the overlay; ``(None, None, {}, "")`` without one.

    A missing overlay is the normal case. One that exists but is not a regular file is an
    error naming it, rather than a "workflow file unreadable" from reading a directory.
    """
    try:
        local = overlay_path.stat()
    except FileNotFoundError:
        return None, None, {}, ""
    except OSError as exc:
        raise MissingWorkflowFile(f"workflow overlay unreadable: {exc}", path=base) from exc
    if not stat.S_ISREG(local.st_mode):
        raise MissingWorkflowFile(
            f"workflow overlay is not a regular file: {overlay_path}", path=base
        )
    try:
        text = overlay_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MissingWorkflowFile(f"workflow overlay unreadable: {exc}", path=base) from exc
    try:
        raw, body = parse_workflow_text(text)
    except ConfigError as exc:
        exc.path = overlay_path
        raise
    return overlay_path, local, raw, body


def _format_errors(exc: ValidationError) -> list[tuple[str, str]]:
    return [
        (".".join(str(part) for part in error["loc"]) or "<root>", error["msg"])
        for error in exc.errors()
    ]
