"""Turn a stored stream-json capture into the blocks the turn page renders. No HTML here.

Every ``title`` and ``text`` is plain text; the template escapes them. Status lines the page
does not show (rate limits, task progress, thinking-token counters, ...) are counted, not lost.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal

from issuebot.db import OMITTED_TYPE

TOOL_INPUT_COLLAPSE = 2 * 1024
TOOL_RESULT_LIMIT = 4 * 1024
UNPARSEABLE_LIMIT = 200

BlockKind = Literal[
    "init", "text", "thinking", "tool_use", "tool_result", "result", "omitted", "unparseable"
]

_INIT_FIELDS: tuple[tuple[str, str], ...] = (
    ("model", "model"),
    ("claude_code_version", "claude code"),
    ("cwd", "cwd"),
    ("permissionMode", "permission mode"),
)


@dataclass(frozen=True, kw_only=True, slots=True)
class Block:
    kind: BlockKind
    title: str  # "assistant", "Bash", "tool result", "result: success", ...
    text: str  # the body, already cut
    cut: int = 0  # characters removed from the body; 0 when whole
    collapsed: bool = False  # rendered inside <details>


@dataclass(frozen=True, kw_only=True, slots=True)
class Transcript:
    blocks: list[Block]
    hidden: int  # status messages not rendered


def parse_transcript(stream: str) -> Transcript:
    """Blocks in stream order; unparseable lines and omitted stubs become placeholders."""
    blocks: list[Block] = []
    hidden = 0
    for line in stream.splitlines():
        if not line.strip():
            continue
        message = _message(line)
        if message is None:
            blocks.append(_cut("unparseable", "unparseable line", line, UNPARSEABLE_LIMIT))
            continue
        kind = message.get("type")
        if kind == "system" and message.get("subtype") == "init":
            blocks.append(_init(message))
        elif kind == "assistant":
            blocks.extend(_content(message, "assistant"))
        elif kind == "user":
            blocks.extend(_content(message, "user"))
        elif kind == "result":
            blocks.append(_result(message))
        elif kind == OMITTED_TYPE:
            blocks.append(_omitted(message))
        else:
            hidden += 1
    return Transcript(blocks=blocks, hidden=hidden)


def _message(line: str) -> dict[str, Any] | None:
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def _init(message: dict[str, Any]) -> Block:
    lines = [f"{label}: {message[key]}" for key, label in _INIT_FIELDS if _text(message.get(key))]
    tools = message.get("tools")
    if isinstance(tools, list):
        lines.append(f"tools: {len(tools)}")
    return Block(kind="init", title="session", text="\n".join(lines))


def _content(message: dict[str, Any], role: str) -> list[Block]:
    inner = message.get("message")
    content = inner.get("content") if isinstance(inner, dict) else None
    if isinstance(content, str):
        return [Block(kind="text", title=role, text=content)]
    if not isinstance(content, list):
        return []
    blocks: list[Block] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            blocks.append(Block(kind="text", title=role, text=_text(part.get("text")) or ""))
        elif kind == "thinking":
            text = _text(part.get("thinking")) or ""
            blocks.append(Block(kind="thinking", title="thinking", text=text, collapsed=True))
        elif kind == "tool_use":
            text = _json(part.get("input"))
            name = _text(part.get("name")) or "tool"
            collapsed = len(text) > TOOL_INPUT_COLLAPSE
            blocks.append(Block(kind="tool_use", title=name, text=text, collapsed=collapsed))
        elif kind == "tool_result":
            title = "tool result (error)" if part.get("is_error") else "tool result"
            body = _result_content(part.get("content"))
            blocks.append(_cut("tool_result", title, body, TOOL_RESULT_LIMIT, collapsed=True))
    return blocks


def _result_content(content: object) -> str:
    """A tool result's content: the string itself, or text parts and the JSON of the rest."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and _text(part.get("text")):
                parts.append(part["text"])
            else:
                parts.append(_json(part))
        return "\n".join(parts)
    return _json(content) if content is not None else ""


def _result(message: dict[str, Any]) -> Block:
    subtype = _text(message.get("subtype")) or "unknown"
    text = _text(message.get("result"))
    if not text:
        errors = message.get("errors")
        text = "\n".join(str(item) for item in errors) if isinstance(errors, list) else ""
    return Block(kind="result", title=f"result: {subtype}", text=text)


def _omitted(message: dict[str, Any]) -> Block:
    original = _text(message.get("original_type"))
    what = f"a {original} message" if original else "a message"
    size = message.get("bytes")
    text = f"{what} of {size} bytes was not stored"
    return Block(kind="omitted", title="omitted", text=text)


def _cut(kind: BlockKind, title: str, text: str, limit: int, *, collapsed: bool = False) -> Block:
    cut = max(len(text) - limit, 0)
    return Block(kind=kind, title=title, text=text[:limit], cut=cut, collapsed=collapsed)


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None
