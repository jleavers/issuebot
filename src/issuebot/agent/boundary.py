"""The session privilege domain: what crosses back out of it, and the one way it does (#104).

#75 put the session at a different uid from the worker and made the workspace directory and
its ``.issuebot`` the worker's, sticky, so the session can add what it likes inside them and
can neither unlink nor rename what the worker keeps there. This module is the other half of
that line. It declares every artefact the worker takes back out of a workspace after the
session has had its uid in it, and it is the one function every such read goes through.

**Invariant.** Anything a session can write stays inside that session -- it is never an input
to a worker decision, and never reachable by another session -- unless it crosses one guarded
seam that checks who wrote it, what kind of object it is, and how large it may be.

What crosses *into* the session needs no guard here: the prompt (claude's stdin), the
environment (a memory file, ``runas``), the clone (``gh repo clone`` as the session) and the
hook scripts are the worker's to give. What the session hands back over a pipe -- claude's
stdout and stderr, a hook's output -- is bounded where the pipe is read (``STREAM_LINE_LIMIT``,
the hook tails). What the session can leave *on disk* for the worker to find is ``ARTEFACTS``,
and nothing else: a worker-side read of any other path under a workspace is a bug. The
clone's own instruction files are on the list (#107): the clone is what the session was
given, but it is also what the session can rewrite, and the worker reads two names out of
it, once per run and after the ``before_run`` hook, for the first turn's prompt.

Every read goes through ``Boundary.read``: the path is opened one component at a time from a
directory the worker owns, never through a symbolic link (``O_NOFOLLOW`` at every step, so a
link planted at the name or above it is refused rather than followed to ``/dev/zero`` or the
operator's own files); the object is checked *before* a byte is read, on the descriptor, so it
cannot be swapped between check and use (a FIFO would block the event loop for good, a device
would allocate until the kernel intervened: ``O_NONBLOCK`` makes the open return either way,
and ``fstat`` refuses anything but a regular file); the owner must be one of the artefact's
declared writers (the worker, or the session for the file its hooks write and for the
clone's instruction files, since the clone is cloned as it);
and at most the artefact's ``limit`` is read, from the head or the tail, whatever the file's
size. A refusal is ``BoundaryError``, an ``OSError``, so every call site's existing ``except
OSError`` reports it the way it reports an unreadable file and never crashes a task on it.

The worker's own state is created through the same object: ``own_dir`` makes and verifies a
directory the worker owns and nobody else may write, which is what makes the turn files
inside it the worker's without a check per file, and ``create_marker`` is the exclusive
create ``#75`` gave the completion sentinel, so a session that pre-placed the name fails the
creation cleanly instead of being trusted.
"""

import contextlib
import errno
import os
import pwd
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Writer = Literal["worker", "session"]
Keep = Literal["head", "tail"]

KIB = 1024
MIB = 1024 * KIB


@dataclass(frozen=True, slots=True)
class Artefact:
    """One thing the worker reads back out of a workspace: who writes it and how much of it
    the worker will ever take."""

    name: str
    where: str
    writer: Writer
    limit: int


# `.issuebot/env`: the one file the session's side writes for the worker to read. A hook
# (which runs as the session, #75) hands the agent variables through it, and the session can
# write it too (#67). Its *keys* are filtered by the runner (`PROTECTED_ENV_NAMES`); its type,
# its owner and its size are checked here. The limit is the runner's `WORKSPACE_ENV_LIMIT`.
ENV_FILE = Artefact("env", ".issuebot/env", "session", 64 * KIB)
# `.issuebot/session.json`: the worker's record of the session's last turn, which configures
# the next one. Trusted only when the worker wrote it (#75).
SESSION_FILE = Artefact("session", ".issuebot/session.json", "worker", 64 * KIB)
# `.issuebot/created`: the completion sentinel, created exclusively and read for its existence.
CREATED_MARKER = Artefact("created", ".issuebot/created", "worker", 0)
# `.issuebot/finished`: the removal sentinel (#149). Written before the unlink that removes a
# workspace, so a removal that failed leaves a workspace that says so and the next terminal
# sweep retries it. That retry used to be a side effect of re-reading every issue issuebot had
# ever completed; here it is a property of the workspace, and so bounded by what is on disk.
# Created exclusively and read for its existence, exactly as `created` is.
FINISHED_MARKER = Artefact("finished", ".issuebot/finished", "worker", 0)
# `.issuebot/runs/<run_id>/turn-N.*`: the runner's tee of claude's stdout, the prompt it fed
# and the stderr it captured, all opened by the worker inside a directory `own_dir` verified.
# The stream is read from the head (the capture keeps a head of lines and the last `result`
# line, and a file past this limit is a session that printed more than any transcript holds);
# stderr from the tail, which is where claude says why it stopped.
TURN_STREAM = Artefact("turn stream", ".issuebot/runs/<run_id>/turn-N.jsonl", "worker", 64 * MIB)
TURN_PROMPT = Artefact("turn prompt", ".issuebot/runs/<run_id>/turn-N.prompt.md", "worker", 4 * MIB)
TURN_STDERR = Artefact(
    "turn stderr", ".issuebot/runs/<run_id>/turn-N.stderr.log", "worker", 16 * MIB
)

# `CLAUDE.md` and `AGENTS.md` at the clone's root: the committers' instruction files, which
# the prompt carries as enveloped data now that `claude` no longer loads them itself (#107).
# The clone is the session's (`gh repo clone` runs as it under `agent.run_as`), so the session
# is a declared writer; a link, a FIFO or a directory by either name is refused here, and a
# file past the limit is cut, which the envelope's source says.
INSTRUCTION_FILE = Artefact(
    "instructions", "<CLAUDE.md|AGENTS.md> at the clone's root", "session", 128 * KIB
)

ARTEFACTS: tuple[Artefact, ...] = (
    ENV_FILE,
    SESSION_FILE,
    CREATED_MARKER,
    FINISHED_MARKER,
    TURN_STREAM,
    TURN_PROMPT,
    TURN_STDERR,
    INSTRUCTION_FILE,
)

_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOCTTY", 0)
_OPEN_FILE = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOCTTY", 0)
_READ_CHUNK = 1 * MIB
# What `own_dir` refuses on a directory the worker made: anyone but the owner writing into it
# is anyone able to plant a name the worker will open next.
_OTHERS_WRITE = stat.S_IWGRP | stat.S_IWOTH


class BoundaryError(OSError):
    """A read-back the boundary would not perform: what was found is not what was declared.

    An ``OSError`` so the callers' existing handling reports it -- a warning naming the path and
    the reason, never the contents -- and the read stays exactly what it was: nothing.
    """

    def __init__(self, path: os.PathLike[str] | str, reason: str) -> None:
        super().__init__(errno.EPERM, f"refused: {reason}", os.fspath(path))
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ReadBack:
    """What one read returned: at most the artefact's limit, and the size the file had."""

    data: bytes
    size: int

    @property
    def truncated(self) -> bool:
        return self.size > len(self.data)


@dataclass(frozen=True, slots=True)
class Boundary:
    """The worker's side of the line: its uid, and the session's when the two differ.

    ``session_uid`` is ``None`` on the host route (no ``agent.run_as``), where the session runs
    as the worker and every writer is the worker.
    """

    worker_uid: int
    session_uid: int | None = None

    @classmethod
    def current(cls, run_as: str | None = None) -> Boundary:
        """The boundary of this process, with ``run_as`` resolved to its uid when it names an
        account that exists; one that does not is reported where the spawn fails (#75)."""
        session: int | None = None
        if run_as is not None:
            try:
                session = pwd.getpwnam(run_as).pw_uid
            except KeyError:
                session = None
        return cls(worker_uid=os.getuid(), session_uid=session)

    @property
    def split(self) -> bool:
        """Whether the session runs at a uid of its own."""
        return self.session_uid is not None and self.session_uid != self.worker_uid

    def writers(self, artefact: Artefact) -> frozenset[int]:
        """The uids that may own ``artefact``."""
        if artefact.writer == "session" and self.session_uid is not None:
            return frozenset({self.worker_uid, self.session_uid})
        return frozenset({self.worker_uid})

    # --- reading ------------------------------------------------------------------

    def read(
        self,
        base: Path,
        parts: Sequence[str],
        artefact: Artefact,
        *,
        keep: Keep = "head",
        limit: int | None = None,
    ) -> ReadBack:
        """Read ``base/parts...`` as ``artefact``: at most its limit, from the head or the tail.

        ``limit`` may take less than the artefact declares, never more.

        ``base`` is a directory the worker owns (a workspace, or a run's log directory) and is
        opened by its path, so it must itself be reached through names the session cannot
        rename or replace: the workspace root is the worker's, and under it the workspace
        and ``.issuebot`` are the worker's and sticky, so a run's log directory under
        ``.issuebot/runs`` (0755, the worker's) qualifies as a base too. Every component
        *under* it is opened without following links and must be the worker's, and the
        final one must be a regular file owned by one of the artefact's writers. A refusal
        is ``BoundaryError``; a file that is simply absent is ``FileNotFoundError``, as before,
        since no file is the normal case for most of these.
        """
        bound = artefact.limit if limit is None else min(limit, artefact.limit)
        fd = self._open(base, parts, _OPEN_FILE)
        try:
            st = os.fstat(fd)
            self._check(base, parts, st, artefact)
            size = st.st_size
            if keep == "tail" and size > bound:
                os.lseek(fd, size - bound, os.SEEK_SET)
            data = _read_up_to(fd, bound)
        finally:
            os.close(fd)
        return ReadBack(data=data, size=max(size, len(data)))

    def inspect(self, base: Path, parts: Sequence[str]) -> os.stat_result | None:
        """The final component's ``lstat`` reached the same way ``read`` reaches it, or
        ``None`` when the name is absent or the walk is refused."""
        try:
            fd = self._open_parent(base, parts)
        except OSError:
            return None
        try:
            return os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except OSError:
            return None
        finally:
            os.close(fd)

    def is_own_file(self, base: Path, parts: Sequence[str]) -> bool:
        """A regular file, the worker's, reached without following a link."""
        st = self.inspect(base, parts)
        return st is not None and stat.S_ISREG(st.st_mode) and st.st_uid == self.worker_uid

    def is_own_dir(self, base: Path, parts: Sequence[str]) -> bool:
        """A directory, the worker's, reached without following a link."""
        st = self.inspect(base, parts)
        return st is not None and stat.S_ISDIR(st.st_mode) and st.st_uid == self.worker_uid

    # --- the worker's own state -----------------------------------------------------

    def own_dir(self, base: Path, parts: Sequence[str], *, mode: int = 0o755) -> None:
        """Create ``base/parts...``, every missing component of it, and verify the last is the
        worker's and closed to everyone else's writes, so the names inside it are the
        worker's to open.

        The components above it are created the same way and must be the worker's too, but
        may be shared (``.issuebot`` is sticky under ``agent.run_as``): what the session can
        do there is add names of its own, never replace the worker's. ``BoundaryError`` when what
        is found is not that: a session that placed a directory of its own at a run's log
        path would otherwise have the worker writing through its names.
        """
        fd = os.open(base, _OPEN_DIR)
        try:
            self._require_own_dir(fd, base)
            for index, part in enumerate(parts):
                where = base.joinpath(*parts[: index + 1])
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, mode, dir_fd=fd)
                child = _open_dir_component(fd, part, where)
                os.close(fd)
                fd = child
                self._require_own_dir(fd, where)
            st = os.fstat(fd)
            if st.st_mode & _OTHERS_WRITE:
                raise BoundaryError(
                    base.joinpath(*parts),
                    f"writable by others (mode {stat.S_IMODE(st.st_mode):04o})",
                )
        finally:
            os.close(fd)

    def create_marker(self, base: Path, parts: Sequence[str], *, mode: int = 0o644) -> None:
        """Create an empty file exclusively: a name already there, whoever's, is an error."""
        fd = self._open_parent(base, parts)
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            os.close(os.open(parts[-1], flags, mode, dir_fd=fd))
        finally:
            os.close(fd)

    def remove_marker(self, base: Path, parts: Sequence[str]) -> None:
        """Unlink a marker this class created; a name that is not there is already gone.

        The walk to the parent is `create_marker`'s, so the directory holding the name is the
        worker's own and reached through no symbolic link -- which is what makes the unlink
        safe in a `.issuebot` shared with the session under `agent.run_as` (#75). Nothing
        follows the final component either: `unlinkat` never does.
        """
        fd = self._open_parent(base, parts)
        try:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(parts[-1], dir_fd=fd)
        finally:
            os.close(fd)

    # --- the walk ---------------------------------------------------------------------

    def _open(self, base: Path, parts: Sequence[str], flags: int) -> int:
        fd = self._open_parent(base, parts)
        try:
            try:
                return os.open(parts[-1], flags | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise BoundaryError(base.joinpath(*parts), "a symbolic link") from None
                raise
        finally:
            os.close(fd)

    def _open_parent(self, base: Path, parts: Sequence[str]) -> int:
        """A descriptor on the directory holding the final component, every step checked."""
        if not parts:
            raise ValueError("a read-back names at least one component under its base")
        fd = os.open(base, _OPEN_DIR)
        try:
            self._require_own_dir(fd, base)
            for index, part in enumerate(parts[:-1]):
                where = base.joinpath(*parts[: index + 1])
                child = _open_dir_component(fd, part, where)
                os.close(fd)
                fd = child
                self._require_own_dir(fd, where)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _require_own_dir(self, fd: int, path: Path) -> None:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise BoundaryError(path, f"not a directory ({_kind(st.st_mode)})")
        if st.st_uid != self.worker_uid:
            raise BoundaryError(path, f"owned by uid {st.st_uid}, not by the worker")

    def _check(
        self, base: Path, parts: Sequence[str], st: os.stat_result, artefact: Artefact
    ) -> None:
        path = base.joinpath(*parts)
        if not stat.S_ISREG(st.st_mode):
            raise BoundaryError(path, f"not a regular file ({_kind(st.st_mode)})")
        writers = self.writers(artefact)
        if st.st_uid not in writers:
            allowed = ", ".join(str(uid) for uid in sorted(writers))
            raise BoundaryError(path, f"owned by uid {st.st_uid}, not by {allowed}")


def _open_dir_component(dir_fd: int, name: str, where: Path) -> int:
    """Open ``name`` under ``dir_fd`` as a directory, following no link.

    Linux answers ``ENOTDIR`` rather than ``ELOOP`` for a link opened with ``O_DIRECTORY``
    and ``O_NOFOLLOW`` both set, so the name is looked at to say which it was.
    """
    try:
        return os.open(name, _OPEN_DIR | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            raise BoundaryError(where, "not a directory") from None
        if stat.S_ISLNK(st.st_mode):
            raise BoundaryError(where, "a symbolic link") from None
        raise BoundaryError(where, f"not a directory ({_kind(st.st_mode)})") from None


def _read_up_to(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, min(_READ_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "a directory"
    if stat.S_ISFIFO(mode):
        return "a fifo"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISCHR(mode):
        return "a character device"
    if stat.S_ISBLK(mode):
        return "a block device"
    if stat.S_ISLNK(mode):
        return "a symbolic link"
    return f"mode {stat.S_IFMT(mode):#o}"


def split_parts(base: Path, path: Path) -> tuple[str, ...]:
    """``path`` as components under ``base``; ``ValueError`` when it does not lie inside."""
    relative = path.relative_to(base)
    parts = tuple(relative.parts)
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{path} is not a name inside {base}")
    return parts
