"""The clone's instruction files reach the prompt as bounded data, never by following a link."""

import os
from pathlib import Path

import pytest

from issuebot.agent.boundary import INSTRUCTION_FILE, Boundary
from issuebot.agent.instructions import (
    INSTRUCTION_FILE_LIMIT,
    REPOSITORY_INSTRUCTION_FILES,
    RepositoryFile,
    read_repository_instructions,
)


def test_the_declared_list_is_the_two_root_files() -> None:
    assert REPOSITORY_INSTRUCTION_FILES == ("CLAUDE.md", "AGENTS.md")
    assert INSTRUCTION_FILE_LIMIT == 128 * 1024


def test_reads_the_named_files_in_order(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("claude\n", encoding="utf-8")
    (tmp_path / "CONTRIBUTING.md").write_text("not on the list\n", encoding="utf-8")
    assert read_repository_instructions(tmp_path) == (
        RepositoryFile(path="CLAUDE.md", text="claude\n", size=7, carried=7, truncated=False),
        RepositoryFile(path="AGENTS.md", text="agents\n", size=7, carried=7, truncated=False),
    )


def test_a_missing_file_is_the_normal_case(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    assert [file.path for file in read_repository_instructions(tmp_path)] == ["AGENTS.md"]
    assert read_repository_instructions(tmp_path / "absent") == ()


def test_a_symlink_is_skipped_without_being_followed(tmp_path: Path) -> None:
    """Under agent.run_as the clone is the session's and this read is the worker's: a link
    the clone ships must not put a file only the worker can read into the prompt."""
    secret = tmp_path / "secret.txt"
    secret.write_text("the worker's\n", encoding="utf-8")
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "CLAUDE.md").symlink_to(secret)
    (clone / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    found = read_repository_instructions(clone)
    assert [file.path for file in found] == ["AGENTS.md"]
    assert all("worker" not in file.text for file in found)


def test_a_directory_by_that_name_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").mkdir()
    assert read_repository_instructions(tmp_path) == ()


def test_a_fifo_by_that_name_is_skipped_without_blocking(tmp_path: Path) -> None:
    """The open runs before the kind of file is known; a FIFO with no writer would otherwise
    hold the worker's session task forever."""
    os.mkfifo(tmp_path / "CLAUDE.md")
    assert read_repository_instructions(tmp_path) == ()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_an_unreadable_file_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text("claude\n", encoding="utf-8")
    path.chmod(0)
    try:
        assert read_repository_instructions(tmp_path) == ()
    finally:
        path.chmod(0o644)


def test_a_large_file_is_cut_and_says_so(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_bytes(b"x" * 100 + b"tail")
    (file,) = read_repository_instructions(tmp_path, limit=100)
    assert file.text == "x" * 100
    assert file.size == 104
    assert file.carried == 100
    assert file.truncated is True
    (file,) = read_repository_instructions(tmp_path, limit=104)
    assert file.truncated is False
    assert file.carried == 104


def test_a_cut_through_a_multibyte_character_reports_the_bytes_carried(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_bytes("aé".encode() + b"b")
    (file,) = read_repository_instructions(tmp_path, limit=2)
    assert file.text == "a\ufffd"
    assert (file.carried, file.size, file.truncated) == (2, 4, True)


def test_undecodable_bytes_are_replaced_not_refused(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_bytes(b"ok \xff\xfe end\n")
    (file,) = read_repository_instructions(tmp_path)
    assert file.text == "ok �� end\n"


def test_the_read_is_the_boundarys(tmp_path: Path) -> None:
    """#104: the two names are the ``instructions`` artefact, and the read goes through the
    boundary it is given, so a workspace that is not the worker's own is refused whole."""
    (tmp_path / "CLAUDE.md").write_text("claude\n", encoding="utf-8")
    ours = Boundary(worker_uid=os.getuid())
    assert [f.path for f in read_repository_instructions(tmp_path, boundary=ours)] == ["CLAUDE.md"]
    theirs = Boundary(worker_uid=os.getuid() + 1)
    assert read_repository_instructions(tmp_path, boundary=theirs) == ()
    assert INSTRUCTION_FILE.writer == "session"
    assert INSTRUCTION_FILE.limit == INSTRUCTION_FILE_LIMIT
