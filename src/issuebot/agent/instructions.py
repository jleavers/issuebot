"""The clone's instruction files, read by issuebot and handed to the prompt as data (#107).

``claude -p`` reads a working tree's ``CLAUDE.md``, ``.claude/`` and ``.mcp.json`` as its own
configuration when its settings sources include the project, and issuebot no longer lets it
(``claude.setting_sources`` defaults to ``user``). What the session still needs from those
files -- how the repository runs its tests, commits and opens pull requests -- reaches it
through the prompt instead, inside the same ``<github-text>`` envelope as the issue, so it is
that repository's committers' text under the ground rules and never instruction the session
inherits by virtue of a file's location. This module is the read: a declared list of names,
each taken back across the session boundary (#104) as the ``instructions`` artefact --
regular files only, never through a link, bounded -- and never a failure.
"""

from dataclasses import dataclass
from pathlib import Path

from issuebot.agent.boundary import INSTRUCTION_FILE, Boundary, BoundaryError
from issuebot.log import get_logger

REPOSITORY_INSTRUCTION_FILES: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")
"""The files at the clone's root the prompt carries, in this order. A declared list, not a
search: whatever else the tree holds is data the session reads itself."""

INSTRUCTION_FILE_LIMIT = INSTRUCTION_FILE.limit
"""Bytes of each file the prompt carries; the rest is cut and the envelope's source says so.

Twice what this repository's own ``CLAUDE.md`` weighs: a cut loses the file's end, which is
where a "how to open pull requests" section tends to sit. Two files at the cap are the whole
of the turn log's 256 KiB prompt head on their own, so a capture of such a prompt is itself
cut; one whole file and most of another still fit beside the template.
"""


@dataclass(frozen=True, kw_only=True, slots=True)
class RepositoryFile:
    path: str
    text: str
    size: int
    carried: int
    """Bytes of the file ``text`` was decoded from: ``size`` unless ``truncated``."""
    truncated: bool


def read_repository_instructions(
    workspace: Path,
    *,
    boundary: Boundary | None = None,
    names: tuple[str, ...] = REPOSITORY_INSTRUCTION_FILES,
    limit: int = INSTRUCTION_FILE_LIMIT,
) -> tuple[RepositoryFile, ...]:
    """The named files at the workspace's root, as far as each exists and the boundary
    hands it over.

    Every read goes through ``Boundary.read`` (#104): under ``agent.run_as`` the clone is the
    session's and this read is the worker's, so a link the clone ships is refused rather than
    followed to a file only the worker can read, a FIFO by that name (the session can make one
    in its own clone) cannot block the open, since it sits on the worker's session task, and
    a directory, a device or a file owned by neither account is refused before a byte is
    read. Every refusal, and a file the worker cannot read, is skipped with a warning; a name
    that is not there is the normal case and says nothing. The text is cut at ``limit`` bytes
    -- at most the artefact's own, which the boundary clamps to -- before decoding, and
    undecodable bytes are replaced rather than refused.

    ``boundary`` is the workspace manager's, which knows the session's uid; the default is
    this process's alone, right where the session is the worker.
    """
    log = get_logger(__name__)
    boundary = boundary or Boundary.current()
    found: list[RepositoryFile] = []
    for name in names:
        path = workspace / name
        try:
            read = boundary.read(workspace, (name,), INSTRUCTION_FILE, limit=limit)
        except FileNotFoundError:
            continue
        except BoundaryError as exc:
            log.warning("repository_instructions_skipped", path=str(path), reason=exc.reason)
            continue
        except OSError as exc:
            log.warning("repository_instructions_skipped", path=str(path), reason=str(exc))
            continue
        found.append(
            RepositoryFile(
                path=name,
                text=read.data.decode("utf-8", errors="replace"),
                size=read.size,
                carried=len(read.data),
                truncated=read.truncated,
            )
        )
    return tuple(found)
