"""The clone's instruction files, read by issuebot and handed to the prompt as data (#107).

``claude -p`` reads a working tree's ``CLAUDE.md``, ``.claude/`` and ``.mcp.json`` as its own
configuration when its settings sources include the project, and issuebot no longer lets it
(``claude.setting_sources`` defaults to ``user``). What the session still needs from those
files -- how the repository runs its tests, commits and opens pull requests -- reaches it
through the prompt instead, inside the same ``<github-text>`` envelope as the issue, so it is
that repository's committers' text under the ground rules and never instruction the session
inherits by virtue of a file's location. This module is the read: a declared list of names,
regular files only, bounded, and never a failure.
"""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from issuebot.log import get_logger

REPOSITORY_INSTRUCTION_FILES: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")
"""The files at the clone's root the prompt carries, in this order. A declared list, not a
search: whatever else the tree holds is data the session reads itself."""

INSTRUCTION_FILE_LIMIT = 128 * 1024
"""Bytes of each file the prompt carries; the rest is cut and the envelope's source says so.

Twice what this repository's own ``CLAUDE.md`` weighs: a cut loses the file's end, which is
where a "how to open pull requests" section tends to sit, and two whole files still fit the
turn log's 256 KiB prompt head.
"""


@dataclass(frozen=True, kw_only=True, slots=True)
class RepositoryFile:
    path: str
    text: str
    size: int
    truncated: bool


def read_repository_instructions(
    workspace: Path,
    *,
    names: tuple[str, ...] = REPOSITORY_INSTRUCTION_FILES,
    limit: int = INSTRUCTION_FILE_LIMIT,
) -> tuple[RepositoryFile, ...]:
    """The named files at the workspace's root, as far as each exists and is a regular file.

    A symlink is skipped without being followed (``O_NOFOLLOW``): under ``agent.run_as`` the
    clone is the session's and this read is the worker's, so a link the clone ships could
    otherwise put a file only the worker can read into a prompt the session sees. A
    directory, an unreadable file or a name that is not there is skipped too, the first two
    with a warning; a missing file is the normal case and says nothing. The text is cut at
    ``limit`` bytes before decoding, and undecodable bytes are replaced rather than refused.
    """
    log = get_logger(__name__)
    found: list[RepositoryFile] = []
    for name in names:
        path = workspace / name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            continue
        except OSError as exc:
            reason = "symlink" if _is_symlink(path) else str(exc)
            log.warning("repository_instructions_skipped", path=str(path), reason=reason)
            continue
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                log.warning(
                    "repository_instructions_skipped", path=str(path), reason="not a regular file"
                )
                continue
            raw = _read_up_to(fd, limit + 1)
        except OSError as exc:
            log.warning("repository_instructions_skipped", path=str(path), reason=str(exc))
            continue
        finally:
            os.close(fd)
        truncated = len(raw) > limit
        text = raw[:limit].decode("utf-8", errors="replace")
        found.append(RepositoryFile(path=name, text=text, size=info.st_size, truncated=truncated))
    return tuple(found)


def _read_up_to(fd: int, count: int) -> bytes:
    """Up to ``count`` bytes: one ``read`` may return fewer than asked without being at EOF."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return False
