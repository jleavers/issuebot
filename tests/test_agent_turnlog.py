"""Tests for the turn-file capture (hermetic: the real sample fixture and synthetic files)."""

import json
from pathlib import Path

import pytest

from issuebot.agent import turnlog
from issuebot.agent.turnlog import OMITTED_TYPE, TurnCapture, capture_turns

SAMPLE = Path(__file__).parent / "fixtures" / "runs" / "20260904T202535Z-0964cd"

INIT = json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5"})
RESULT = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 3,
        "duration_ms": 1234,
        "total_cost_usd": 0.25,
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 30,
            "output_tokens": 40,
        },
        "result": "done",
    }
)


def assistant(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def write_turn(log_dir: Path, number: int, lines: list[str], **files: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"turn-{number}.jsonl").write_text("".join(f"{line}\n" for line in lines))
    for suffix, text in files.items():
        (log_dir / f"turn-{number}.{suffix}").write_text(text)


def only(captures: list[TurnCapture]) -> TurnCapture:
    assert len(captures) == 1
    return captures[0]


# --- the real sample -----------------------------------------------------------------------


def test_the_sample_turn_is_captured_whole() -> None:
    capture = only(capture_turns(SAMPLE))
    assert capture.turn_number == 1
    assert (capture.stream_lines, capture.stream_bytes) == (95, 115429)
    assert (capture.omitted_lines, capture.truncated) == (0, False)
    assert capture.stream == (SAMPLE / "turn-1.jsonl").read_text(encoding="utf-8")
    assert all(json.loads(line) for line in capture.stream.splitlines())
    assert (capture.model, capture.subtype, capture.is_error) == ("claude-opus-5", "success", False)
    assert (capture.num_turns, capture.duration_ms) == (19, 201719)
    assert (capture.input_tokens, capture.cache_creation_input_tokens) == (38, 23100)
    assert (capture.cache_read_input_tokens, capture.output_tokens) == (490200, 8425)
    assert capture.cost_usd == pytest.approx(0.89759225)
    assert capture.result_text is not None and capture.result_text.startswith("Done. Issue #7")
    assert capture.prompt_bytes == 10106
    assert capture.prompt.startswith("You are working on GitHub issue `issuebot-scratch-7`")
    assert (capture.stderr, capture.stderr_bytes) == ("", 0)


# --- caps ------------------------------------------------------------------------------------


def test_an_oversized_line_becomes_a_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turnlog, "LINE_LIMIT", len(RESULT))  # INIT and RESULT fit, big does not
    big = json.dumps(
        {"type": "user", "message": {"content": [{"type": "text", "text": "x" * len(RESULT)}]}}
    )
    write_turn(tmp_path, 1, [INIT, big, RESULT])
    capture = only(capture_turns(tmp_path))
    lines = capture.stream.splitlines()
    assert (capture.stream_lines, capture.omitted_lines, capture.truncated) == (3, 1, False)
    assert json.loads(lines[0])["subtype"] == "init"
    stub = json.loads(lines[1])
    assert stub == {"type": OMITTED_TYPE, "original_type": "user", "bytes": len(big)}
    assert json.loads(lines[2])["type"] == "result"
    assert capture.stream_bytes == len(INIT) + len(big) + len(RESULT) + 3


def test_an_oversized_unparseable_line_has_no_original_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turnlog, "LINE_LIMIT", 20)
    write_turn(tmp_path, 1, ["not json " * 10])
    capture = only(capture_turns(tmp_path))
    stub = json.loads(capture.stream)
    assert (stub["type"], stub["original_type"], capture.omitted_lines) == (OMITTED_TYPE, None, 1)


def test_the_head_cap_keeps_the_result_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turnlog, "STREAM_LIMIT", len(INIT) + 1 + 2 * (len(assistant("a" * 50)) + 1))
    chatter = [assistant("a" * 50) for _ in range(10)]
    write_turn(tmp_path, 1, [INIT, *chatter, RESULT])
    capture = only(capture_turns(tmp_path))
    lines = capture.stream.splitlines()
    assert capture.truncated is True
    assert (capture.stream_lines, len(lines)) == (12, 4)
    assert json.loads(lines[0])["subtype"] == "init"
    assert json.loads(lines[-1])["type"] == "result"
    assert capture.num_turns == 3  # the summary reads the file, not the capped stream


def test_a_kept_result_line_is_not_appended_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turnlog, "STREAM_LIMIT", len(INIT) + 1 + len(RESULT) + 1)
    write_turn(tmp_path, 1, [INIT, RESULT, assistant("trailing " * 20)])
    capture = only(capture_turns(tmp_path))
    lines = capture.stream.splitlines()
    assert capture.truncated is True
    assert [json.loads(line)["type"] for line in lines] == ["system", "result"]


def test_prompt_head_and_stderr_tail_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turnlog, "PROMPT_LIMIT", 16)
    monkeypatch.setattr(turnlog, "STDERR_LIMIT", 16)
    write_turn(tmp_path, 1, [INIT], **{"prompt.md": "p" * 40, "stderr.log": "x" * 20 + "TAIL"})
    capture = only(capture_turns(tmp_path))
    assert (capture.prompt, capture.prompt_bytes) == ("p" * 16, 40)
    assert (capture.stderr, capture.stderr_bytes) == ("x" * 12 + "TAIL", 24)


def test_a_nul_byte_in_captured_text_is_replaced(tmp_path: Path) -> None:
    """PostgreSQL rejects ``\\x00`` in ``text``; ``decode(errors="replace")`` alone lets it
    through since it is valid UTF-8, so a NUL must not survive into any of the three texts."""
    prompt_bytes = b"prompt \x00 text"
    stderr_bytes = b"stderr \x00 text"
    write_turn(tmp_path, 1, [INIT, RESULT, "not json \x00 here"])
    (tmp_path / "turn-1.prompt.md").write_bytes(prompt_bytes)
    (tmp_path / "turn-1.stderr.log").write_bytes(stderr_bytes)
    capture = only(capture_turns(tmp_path))
    assert "\x00" not in capture.prompt
    assert "\x00" not in capture.stderr
    assert "\x00" not in capture.stream
    assert (capture.prompt_bytes, capture.stderr_bytes) == (len(prompt_bytes), len(stderr_bytes))


def test_result_text_is_cut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turnlog, "RESULT_TEXT_LIMIT", 5)
    write_turn(tmp_path, 1, [json.dumps({"type": "result", "result": "abcdefgh"})])
    assert only(capture_turns(tmp_path)).result_text == "abcde"


# --- files and directories -------------------------------------------------------------------


def test_missing_prompt_and_stderr_files_are_empty(tmp_path: Path) -> None:
    write_turn(tmp_path, 1, [INIT, RESULT])
    capture = only(capture_turns(tmp_path))
    assert (capture.prompt, capture.prompt_bytes, capture.stderr, capture.stderr_bytes) == (
        "",
        0,
        "",
        0,
    )


def test_a_missing_directory_gives_no_captures(tmp_path: Path) -> None:
    assert capture_turns(tmp_path / "nope") == []


def test_an_unreadable_stream_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "turn-1.jsonl").mkdir(parents=True)  # a directory: read_bytes raises OSError
    write_turn(tmp_path, 2, [INIT, RESULT])
    assert [capture.turn_number for capture in capture_turns(tmp_path)] == [2]


def test_turns_sort_numerically_and_other_files_are_ignored(tmp_path: Path) -> None:
    write_turn(tmp_path, 10, [INIT])
    write_turn(tmp_path, 2, [INIT])
    (tmp_path / "notes.txt").write_text("ignored")
    (tmp_path / "turn-x.jsonl").write_text("ignored")
    assert [capture.turn_number for capture in capture_turns(tmp_path)] == [2, 10]


def test_an_empty_stream_file(tmp_path: Path) -> None:
    write_turn(tmp_path, 1, [])
    capture = only(capture_turns(tmp_path))
    assert (capture.stream, capture.stream_lines, capture.stream_bytes) == ("", 0, 0)
    assert (capture.truncated, capture.model, capture.subtype) == (False, None, None)


# --- the summary -----------------------------------------------------------------------------


def test_unparseable_lines_are_kept_and_counted(tmp_path: Path) -> None:
    write_turn(tmp_path, 1, [INIT, "not json", RESULT])
    capture = only(capture_turns(tmp_path))
    assert capture.stream.splitlines()[1] == "not json"
    assert (capture.stream_lines, capture.omitted_lines, capture.subtype) == (3, 0, "success")


def test_no_result_line_gives_null_summary_columns(tmp_path: Path) -> None:
    write_turn(tmp_path, 1, [INIT, assistant("hello")])
    capture = only(capture_turns(tmp_path))
    assert capture.model == "claude-opus-5"
    assert (capture.subtype, capture.is_error, capture.num_turns) == (None, None, None)
    assert (capture.input_tokens, capture.output_tokens, capture.cost_usd) == (None, None, None)
    assert (capture.duration_ms, capture.result_text) == (None, None)


def test_values_of_the_wrong_type_are_ignored(tmp_path: Path) -> None:
    bad = json.dumps(
        {
            "type": "result",
            "subtype": 7,
            "is_error": "no",
            "num_turns": "19",
            "duration_ms": 1.5,
            "total_cost_usd": True,
            "usage": "lots",
            "result": ["a"],
        }
    )
    write_turn(tmp_path, 1, [bad])
    capture = only(capture_turns(tmp_path))
    assert (capture.subtype, capture.is_error, capture.num_turns) == (None, None, None)
    assert (capture.duration_ms, capture.cost_usd, capture.result_text) == (None, None, None)
    assert (capture.input_tokens, capture.output_tokens) == (None, None)


def test_the_last_result_and_init_lines_win(tmp_path: Path) -> None:
    second_init = json.dumps({"type": "system", "subtype": "init", "model": "claude-sonnet-5"})
    second_result = json.dumps({"type": "result", "subtype": "error_during_execution"})
    write_turn(tmp_path, 1, [INIT, RESULT, second_init, second_result])
    capture = only(capture_turns(tmp_path))
    assert (capture.model, capture.subtype) == ("claude-sonnet-5", "error_during_execution")
