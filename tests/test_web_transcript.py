"""Tests for the transcript parser (hermetic: the real sample fixture and synthetic lines)."""

import json
from collections import Counter
from pathlib import Path

import pytest

from issuebot.web import transcript as transcript_module
from issuebot.web.transcript import (
    TOOL_INPUT_COLLAPSE,
    TOOL_RESULT_LIMIT,
    UNPARSEABLE_LIMIT,
    Block,
    parse_transcript,
)

SAMPLE = Path(__file__).parent / "fixtures" / "runs" / "20260904T202535Z-0964cd" / "turn-1.jsonl"


def line(**message: object) -> str:
    return json.dumps(message)


def assistant(*blocks: dict[str, object]) -> str:
    return line(type="assistant", message={"role": "assistant", "content": list(blocks)})


def user(*blocks: dict[str, object]) -> str:
    return line(type="user", message={"role": "user", "content": list(blocks)})


def parse(*lines: str) -> list[Block]:
    return parse_transcript("".join(f"{text}\n" for text in lines)).blocks


# --- the real sample -----------------------------------------------------------------------


def test_the_sample_parses_into_the_expected_blocks() -> None:
    result = parse_transcript(SAMPLE.read_text(encoding="utf-8"))
    kinds = Counter(block.kind for block in result.blocks)
    assert kinds == {
        "init": 1,
        "text": 11,
        "thinking": 4,
        "tool_use": 24,
        "tool_result": 24,
        "result": 1,
    }
    assert result.hidden == 30
    first = result.blocks[0]
    assert first.kind == "init" and first.title == "session"
    assert "model: claude-opus-5" in first.text and "claude code: 2.1.261" in first.text
    assert {block.title for block in result.blocks if block.kind == "tool_use"} >= {"Bash", "Agent"}
    titles = [block.title for block in result.blocks if block.kind == "text"]
    assert titles.count("assistant") == 10 and titles.count("user") == 1
    last = result.blocks[-1]
    assert (last.kind, last.title) == ("result", "result: success")
    assert last.text.startswith("Done. Issue #7 is on `issuebot/review`.")
    assert all(block.collapsed for block in result.blocks if block.kind == "thinking")
    assert all(block.collapsed for block in result.blocks if block.kind == "tool_result")


# --- assistant and user blocks ---------------------------------------------------------------


def test_text_thinking_and_tool_use_blocks() -> None:
    blocks = parse(
        assistant(
            {"type": "text", "text": "Looking."},
            {"type": "thinking", "thinking": "hmm"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls", "b": 1}},
        )
    )
    assert [block.kind for block in blocks] == ["text", "thinking", "tool_use"]
    assert (blocks[0].title, blocks[0].text, blocks[0].collapsed) == (
        "assistant",
        "Looking.",
        False,
    )
    assert (blocks[1].title, blocks[1].text, blocks[1].collapsed) == ("thinking", "hmm", True)
    assert blocks[2].title == "Bash"
    assert blocks[2].text == '{\n  "b": 1,\n  "command": "ls"\n}'
    assert blocks[2].collapsed is False


def test_a_long_tool_input_is_collapsed_but_whole() -> None:
    text = "x" * (TOOL_INPUT_COLLAPSE + 10)
    (block,) = parse(assistant({"type": "tool_use", "name": "Write", "input": {"content": text}}))
    assert block.collapsed is True and block.cut == 0
    assert text in block.text


def test_tool_results_are_collapsed_and_cut() -> None:
    long = "y" * (TOOL_RESULT_LIMIT + 100)
    blocks = parse(
        user(
            {"type": "tool_result", "tool_use_id": "t1", "content": "short"},
            {"type": "tool_result", "tool_use_id": "t2", "content": long, "is_error": True},
        )
    )
    assert [block.kind for block in blocks] == ["tool_result", "tool_result"]
    assert (blocks[0].title, blocks[0].text, blocks[0].cut, blocks[0].collapsed) == (
        "tool result",
        "short",
        0,
        True,
    )
    assert (blocks[1].title, blocks[1].cut) == ("tool result (error)", 100)
    assert blocks[1].text == "y" * TOOL_RESULT_LIMIT


def test_a_non_text_tool_result_is_rendered_as_text_parts_and_json() -> None:
    content = [
        {"type": "text", "text": "first"},
        {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
        {"type": "text", "text": "second"},
    ]
    (block,) = parse(user({"type": "tool_result", "tool_use_id": "t1", "content": content}))
    image = (
        '{\n  "source": {\n    "data": "AAAA",\n    "type": "base64"\n  },\n  "type": "image"\n}'
    )
    assert block.text == f"first\n{image}\nsecond"


def test_a_line_separator_inside_a_tool_result_does_not_split_the_line() -> None:
    """Node's ``JSON.stringify`` (claude's stream-json) leaves U+2028/U+0085 raw inside JSON
    strings; the capture splits bytes on ``\\n`` only, so the parser must too."""
    for char in (chr(0x2028), chr(0x0085)):
        message = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": f"before{char}after"}
                ],
            },
        }
        raw = json.dumps(message, ensure_ascii=False) + "\n"
        (block,) = parse_transcript(raw).blocks
        assert block.kind == "tool_result"
        assert char in block.text


def test_a_user_text_block_is_titled_user() -> None:
    (block,) = parse(user({"type": "text", "text": "Review the diff."}))
    assert (block.kind, block.title, block.text) == ("text", "user", "Review the diff.")


def test_string_content_is_one_text_block() -> None:
    blocks = parse(
        line(type="assistant", message={"content": "plain"}),
        line(type="user", message={"content": "reply"}),
    )
    assert [(block.title, block.text) for block in blocks] == [
        ("assistant", "plain"),
        ("user", "reply"),
    ]


def test_unknown_content_blocks_are_ignored() -> None:
    assert parse(assistant({"type": "server_tool_use", "name": "web_search"})) == []


# --- init, result, omitted, unparseable ------------------------------------------------------


def test_the_init_block_lists_what_it_knows() -> None:
    (block,) = parse(
        line(
            type="system",
            subtype="init",
            model="claude-opus-5",
            claude_code_version="2.1.261",
            cwd="/w",
            permissionMode="auto",
            tools=["Bash", "Read"],
        )
    )
    assert block.kind == "init"
    assert block.text == (
        "model: claude-opus-5\nclaude code: 2.1.261\ncwd: /w\npermission mode: auto\ntools: 2"
    )
    assert parse(line(type="system", subtype="init"))[0].text == ""


def test_the_result_block_carries_subtype_and_text() -> None:
    (ok,) = parse(line(type="result", subtype="success", result="All done."))
    assert (ok.title, ok.text, ok.collapsed) == ("result: success", "All done.", False)
    (failed,) = parse(
        line(type="result", subtype="error_during_execution", is_error=True, errors=["a", "b"])
    )
    assert (failed.title, failed.text) == ("result: error_during_execution", "a\nb")
    (bare,) = parse(line(type="result"))
    assert (bare.title, bare.text) == ("result: unknown", "")


def test_an_omitted_stub_becomes_a_placeholder() -> None:
    (block,) = parse(line(type="issuebot_omitted", original_type="user", bytes=70000))
    assert (block.kind, block.title) == ("omitted", "omitted")
    assert block.text == "a user message of 70000 bytes was not stored"
    (unknown,) = parse(line(type="issuebot_omitted", original_type=None, bytes=5))
    assert unknown.text == "a message of 5 bytes was not stored"


def test_an_unparseable_line_is_shown_cut() -> None:
    junk = "not json " * 50
    (block, array) = parse(junk, "[1, 2]")
    assert (block.kind, block.title) == ("unparseable", "unparseable line")
    assert (block.text, block.cut) == (junk[:UNPARSEABLE_LIMIT], len(junk) - UNPARSEABLE_LIMIT)
    assert (array.kind, array.text, array.cut) == ("unparseable", "[1, 2]", 0)


def test_status_lines_are_counted_not_rendered() -> None:
    result = parse_transcript(
        "\n".join(
            [
                line(type="rate_limit_event"),
                line(type="system", subtype="task_started"),
                line(type="tool_progress"),
                "",
                line(type="result", subtype="success", result="x"),
            ]
        )
    )
    assert [block.kind for block in result.blocks] == ["result"]
    assert result.hidden == 3


def test_an_empty_stream_has_no_blocks() -> None:
    result = parse_transcript("")
    assert (result.blocks, result.hidden) == ([], 0)


@pytest.mark.parametrize("name", ["TOOL_INPUT_COLLAPSE", "TOOL_RESULT_LIMIT", "UNPARSEABLE_LIMIT"])
def test_the_limits_are_module_constants(name: str) -> None:
    assert isinstance(getattr(transcript_module, name), int)
