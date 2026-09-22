"""Tests for the session boundary: the declared read-backs and the one guarded read (#104)."""

import os
import stat
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from issuebot.agent import boundary as boundary_module
from issuebot.agent.boundary import (
    ARTEFACTS,
    CREATED_MARKER,
    ENV_FILE,
    FINISHED_MARKER,
    INSTRUCTION_FILE,
    SESSION_FILE,
    TURN_PROMPT,
    TURN_STDERR,
    TURN_STREAM,
    Artefact,
    Boundary,
    BoundaryError,
    ReadBack,
    split_parts,
)

posix = pytest.mark.skipif(sys.platform == "win32", reason="fifos, devices and uids are POSIX")
ME = os.getuid()
SMALL = Artefact("small", "x", "worker", 16)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    path = tmp_path / "ws"
    (path / ".issuebot").mkdir(parents=True)
    return path


def env_file(ws: Path, text: str) -> Path:
    path = ws / ".issuebot" / "env"
    path.write_text(text)
    return path


# --- the declaration -------------------------------------------------------------------------


def test_every_artefact_names_its_writer_and_its_bound() -> None:
    assert ARTEFACTS == (
        ENV_FILE,
        SESSION_FILE,
        CREATED_MARKER,
        FINISHED_MARKER,
        TURN_STREAM,
        TURN_PROMPT,
        TURN_STDERR,
        INSTRUCTION_FILE,
    )
    assert len({artefact.name for artefact in ARTEFACTS}) == len(ARTEFACTS)
    # The session's side writes two of them: its hooks' env file, and the clone's own
    # instruction files (#107), since the clone is the session's; the rest is the worker's own.
    assert [a.name for a in ARTEFACTS if a.writer == "session"] == ["env", "instructions"]
    markers = (CREATED_MARKER, FINISHED_MARKER)
    assert all(a.limit > 0 for a in ARTEFACTS if a not in markers)
    # Both sentinels are read for their existence, never for their contents.
    assert [a.limit for a in markers] == [0, 0]


def test_the_boundary_knows_who_may_write_what() -> None:
    same = Boundary(worker_uid=1000)
    assert not same.split
    assert same.writers(ENV_FILE) == same.writers(SESSION_FILE) == frozenset({1000})
    split = Boundary(worker_uid=1000, session_uid=1001)
    assert split.split
    assert split.writers(ENV_FILE) == frozenset({1000, 1001})
    assert split.writers(SESSION_FILE) == frozenset({1000})
    # The clone is `gh repo clone`d as the session (#107), so its instruction files are its.
    assert split.writers(INSTRUCTION_FILE) == frozenset({1000, 1001})
    assert same.writers(INSTRUCTION_FILE) == frozenset({1000})


def test_current_resolves_the_account_or_leaves_the_session_unset() -> None:
    import pwd

    assert Boundary.current() == Boundary(worker_uid=ME, session_uid=None)
    me = pwd.getpwuid(ME).pw_name
    assert Boundary.current(me) == Boundary(worker_uid=ME, session_uid=ME)
    # An account that does not exist is the spawn's failure to report (#75), not this one's.
    assert Boundary.current("no-such-account-issuebot") == Boundary(worker_uid=ME)


# --- reading: what is accepted ---------------------------------------------------------------


def test_a_regular_file_is_read_whole_when_under_the_limit(ws: Path) -> None:
    env_file(ws, "FOO=bar\n")
    read = Boundary.current().read(ws, (".issuebot", "env"), ENV_FILE)
    assert read == ReadBack(data=b"FOO=bar\n", size=8)
    assert not read.truncated


def test_a_missing_file_is_a_file_not_found_error(ws: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Boundary.current().read(ws, (".issuebot", "env"), ENV_FILE)
    with pytest.raises(FileNotFoundError):
        Boundary.current().read(ws, (".issuebot", "nope", "env"), ENV_FILE)


def test_the_limit_bounds_the_bytes_read_from_the_head_or_the_tail(ws: Path) -> None:
    env_file(ws, "0123456789" * 4)
    b = Boundary.current()
    head = b.read(ws, (".issuebot", "env"), SMALL)
    assert head == ReadBack(data=b"0123456789012345", size=40)
    assert head.truncated
    tail = b.read(ws, (".issuebot", "env"), SMALL, keep="tail")
    assert tail == ReadBack(data=b"4567890123456789", size=40)
    # A caller may take less than the artefact declares, never more.
    assert b.read(ws, (".issuebot", "env"), SMALL, limit=4).data == b"0123"
    assert b.read(ws, (".issuebot", "env"), SMALL, limit=4, keep="tail").data == b"6789"
    assert len(b.read(ws, (".issuebot", "env"), SMALL, limit=1000).data) == 16


def test_the_read_reads_in_chunks_up_to_the_limit(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(boundary_module, "_READ_CHUNK", 3)
    env_file(ws, "abcdefgh")
    assert Boundary.current().read(ws, (".issuebot", "env"), SMALL, limit=7).data == b"abcdefg"


# --- reading: what is refused, before a byte is read ----------------------------------------


@posix
def test_a_fifo_at_the_name_is_refused_without_blocking(ws: Path) -> None:
    os.mkfifo(ws / ".issuebot" / "env")
    outcome: list[BaseException | ReadBack] = []

    def attempt() -> None:
        try:
            outcome.append(Boundary.current().read(ws, (".issuebot", "env"), ENV_FILE))
        except BaseException as exc:  # reported through `outcome`
            outcome.append(exc)

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "the read blocked on the fifo"
    (result,) = outcome
    assert isinstance(result, BoundaryError)
    assert result.reason == "not a regular file (a fifo)"
    assert result.filename == str(ws / ".issuebot" / "env")


@posix
def test_a_symbolic_link_at_the_name_is_refused_not_followed(ws: Path, tmp_path: Path) -> None:
    outside = tmp_path / "operator.env"
    outside.write_text("ISSUEBOT_DB_PASSWORD=hunter2\n")
    os.symlink(outside, ws / ".issuebot" / "env")
    with pytest.raises(BoundaryError) as exc:
        Boundary.current().read(ws, (".issuebot", "env"), ENV_FILE)
    assert exc.value.reason == "a symbolic link"
    os.unlink(ws / ".issuebot" / "env")
    os.symlink("/dev/zero", ws / ".issuebot" / "env")
    with pytest.raises(BoundaryError, match="a symbolic link"):
        Boundary.current().read(ws, (".issuebot", "env"), ENV_FILE)


@posix
def test_a_symbolic_link_above_the_name_is_refused(ws: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "env").write_text("X=1\n")
    linked = ws / "linked"
    os.symlink(elsewhere, linked)
    with pytest.raises(BoundaryError) as exc:
        Boundary.current().read(ws, ("linked", "env"), ENV_FILE)
    assert exc.value.reason == "a symbolic link"
    assert exc.value.filename == str(linked)


@posix
def test_a_device_node_is_refused() -> None:
    dev = Path("/dev")
    if not (dev / "zero").exists():
        pytest.skip("no /dev/zero here")
    with pytest.raises(BoundaryError, match="not a regular file"):
        # A boundary whose worker owns /dev, so the walk passes and the device itself is
        # what is refused, on the descriptor, before a byte of it is read.
        Boundary(worker_uid=os.stat(dev).st_uid).read(dev, ("zero",), ENV_FILE)


def test_a_directory_at_the_name_is_refused(ws: Path) -> None:
    (ws / ".issuebot" / "env").mkdir()
    with pytest.raises(BoundaryError) as exc:
        Boundary.current().read(ws, (".issuebot", "env"), ENV_FILE)
    assert exc.value.reason == "not a regular file (a directory)"


def test_a_file_where_a_directory_was_expected_is_refused(ws: Path) -> None:
    (ws / "file").write_text("x")
    with pytest.raises(BoundaryError) as exc:
        Boundary.current().read(ws, ("file", "env"), ENV_FILE)
    assert exc.value.reason == "not a directory (mode 0o100000)"


def test_a_file_owned_by_nobody_declared_is_refused(ws: Path) -> None:
    env_file(ws, "FOO=bar\n")
    stranger = Boundary(worker_uid=ME + 1, session_uid=ME + 2)
    # The walk refuses the workspace itself first: it is not that worker's directory.
    with pytest.raises(BoundaryError) as exc:
        stranger.read(ws, (".issuebot", "env"), ENV_FILE)
    assert exc.value.reason == f"owned by uid {ME}, not by the worker"
    assert exc.value.filename == str(ws)


def test_the_final_component_must_be_one_of_the_artefacts_writers(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directories are the worker's; the file may be the session's for the env file only."""
    env_file(ws, "FOO=bar\n")
    real_fstat = os.fstat

    def fstat_as_session(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        if stat.S_ISREG(st.st_mode):
            return os.stat_result((*st[:4], ME + 1, *st[5:]))
        return st

    monkeypatch.setattr(boundary_module.os, "fstat", fstat_as_session)
    split = Boundary(worker_uid=ME, session_uid=ME + 1)
    assert split.read(ws, (".issuebot", "env"), ENV_FILE).data == b"FOO=bar\n"
    with pytest.raises(BoundaryError) as exc:
        split.read(ws, (".issuebot", "env"), SESSION_FILE)
    assert exc.value.reason == f"owned by uid {ME + 1}, not by {ME}"
    with pytest.raises(BoundaryError) as exc:
        Boundary(worker_uid=ME).read(ws, (".issuebot", "env"), ENV_FILE)
    assert exc.value.reason == f"owned by uid {ME + 1}, not by {ME}"


def test_a_refusal_is_an_oserror_naming_the_path_and_the_reason(ws: Path) -> None:
    (ws / ".issuebot" / "env").mkdir()
    with pytest.raises(OSError) as exc:
        Boundary.current().read(ws, (".issuebot", "env"), ENV_FILE)
    assert isinstance(exc.value, BoundaryError)
    assert str(exc.value) == (
        f"[Errno 1] refused: not a regular file (a directory): '{ws / '.issuebot' / 'env'}'"
    )


def test_a_read_back_names_at_least_one_component(ws: Path) -> None:
    with pytest.raises(ValueError):
        Boundary.current().read(ws, (), ENV_FILE)


# --- inspecting -----------------------------------------------------------------------------


@posix
def test_inspect_and_the_ownership_helpers_do_not_follow_links(ws: Path, tmp_path: Path) -> None:
    b = Boundary.current()
    (ws / ".issuebot" / "created").touch()
    assert b.is_own_file(ws, (".issuebot", "created"))
    assert b.is_own_dir(ws, (".issuebot",))
    assert not b.is_own_dir(ws, (".issuebot", "created"))
    assert not b.is_own_file(ws, (".issuebot",))
    assert not b.is_own_file(ws, (".issuebot", "absent"))
    assert b.inspect(ws, (".issuebot", "absent")) is None
    real = tmp_path / "real"
    real.touch()
    os.symlink(real, ws / ".issuebot" / "link")
    st = b.inspect(ws, (".issuebot", "link"))
    assert st is not None and stat.S_ISLNK(st.st_mode)
    assert not b.is_own_file(ws, (".issuebot", "link"))
    assert not Boundary(worker_uid=ME + 1).is_own_file(ws, (".issuebot", "created"))


# --- the worker's own state ---------------------------------------------------------------


def test_own_dir_creates_the_path_and_verifies_it(ws: Path) -> None:
    b = Boundary.current()
    b.own_dir(ws, (".issuebot", "runs", "r1"))
    st = (ws / ".issuebot" / "runs" / "r1").lstat()
    assert stat.S_ISDIR(st.st_mode) and st.st_uid == ME
    assert not st.st_mode & 0o022
    b.own_dir(ws, (".issuebot", "runs", "r1"))  # idempotent


@posix
def test_own_dir_refuses_a_link_a_file_or_a_shared_directory_at_the_end(
    ws: Path, tmp_path: Path
) -> None:
    b = Boundary.current()
    runs = ws / ".issuebot" / "runs"
    runs.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, runs / "linked")
    with pytest.raises(BoundaryError, match="a symbolic link"):
        b.own_dir(ws, (".issuebot", "runs", "linked"))
    (runs / "file").write_text("x")
    with pytest.raises(BoundaryError, match=r"not a directory \(mode 0o100000\)"):
        b.own_dir(ws, (".issuebot", "runs", "file"))
    (runs / "shared").mkdir()
    os.chmod(runs / "shared", 0o1777)
    with pytest.raises(BoundaryError, match=r"writable by others \(mode 1777\)"):
        b.own_dir(ws, (".issuebot", "runs", "shared"))
    # A shared directory *above* the end is fine: `.issuebot` is sticky under agent.run_as.
    os.chmod(ws / ".issuebot", 0o1777)
    b.own_dir(ws, (".issuebot", "runs", "r1"))


def test_own_dir_refuses_a_directory_that_is_not_the_workers(ws: Path) -> None:
    with pytest.raises(BoundaryError, match="not by the worker"):
        Boundary(worker_uid=ME + 1).own_dir(ws, (".issuebot", "runs"))


@posix
def test_create_marker_is_exclusive(ws: Path, tmp_path: Path) -> None:
    b = Boundary.current()
    b.create_marker(ws, (".issuebot", "created"))
    assert (ws / ".issuebot" / "created").is_file()
    with pytest.raises(FileExistsError):
        b.create_marker(ws, (".issuebot", "created"))
    os.symlink(tmp_path / "nowhere", ws / ".issuebot" / "planted")
    with pytest.raises(FileExistsError):
        b.create_marker(ws, (".issuebot", "planted"))
    assert not (tmp_path / "nowhere").exists()


@posix
def test_remove_marker_unlinks_the_name_and_never_what_it_points_at(
    ws: Path, tmp_path: Path
) -> None:
    """The removal mark is cleared when a workspace is reused (#149), in a `.issuebot` the
    session shares under `agent.run_as`: so the unlink takes the name and follows nothing."""
    b = Boundary.current()
    b.create_marker(ws, (".issuebot", "finished"))
    b.remove_marker(ws, (".issuebot", "finished"))
    assert not (ws / ".issuebot" / "finished").exists()
    # Absent is the goal state, not an error: a mark the sweep already cleared is cleared.
    b.remove_marker(ws, (".issuebot", "finished"))

    target = tmp_path / "elsewhere"
    target.write_text("keep me", encoding="utf-8")
    os.symlink(target, ws / ".issuebot" / "finished")
    b.remove_marker(ws, (".issuebot", "finished"))
    assert not (ws / ".issuebot" / "finished").exists()
    assert target.read_text(encoding="utf-8") == "keep me"

    b.create_marker(ws, (".issuebot", "finished"))
    with pytest.raises(BoundaryError, match="not by the worker"):
        Boundary(worker_uid=ME + 1).remove_marker(ws, (".issuebot", "finished"))
    assert (ws / ".issuebot" / "finished").is_file()


# --- helpers --------------------------------------------------------------------------------


def test_split_parts_names_a_path_inside_its_base(tmp_path: Path) -> None:
    assert split_parts(tmp_path, tmp_path / ".issuebot" / "runs" / "r1") == (
        ".issuebot",
        "runs",
        "r1",
    )
    with pytest.raises(ValueError):
        split_parts(tmp_path, tmp_path)
    with pytest.raises(ValueError):
        split_parts(tmp_path, tmp_path.parent / "other")


def test_artefacts_are_frozen_values() -> None:
    smaller = replace(TURN_STREAM, limit=10)
    assert smaller.limit == 10 and TURN_STREAM.limit == 64 * 1024 * 1024
    with pytest.raises(AttributeError):
        TURN_STREAM.limit = 1  # type: ignore[misc]
