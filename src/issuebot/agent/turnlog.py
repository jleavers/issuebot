"""Capture a run's turn files (stream-json, prompt, stderr) for the database, capped.

The runner writes ``turn-N.jsonl``, ``turn-N.prompt.md`` and ``turn-N.stderr.log`` under a run's
log directory. ``capture_turns`` reads them once, applies the size caps and parses the summary
the dashboard shows. It never raises: an unreadable directory yields nothing, an unreadable
stream file skips its turn, a missing prompt or stderr file is empty.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROMPT_LIMIT = 256 * 1024
LINE_LIMIT = 64 * 1024
STREAM_LIMIT = 2 * 1024 * 1024
STDERR_LIMIT = 64 * 1024
RESULT_TEXT_LIMIT = 4 * 1024
OMITTED_TYPE = "issuebot_omitted"
TURN_FILE = re.compile(r"^turn-(\d+)\.jsonl$")


@dataclass(frozen=True, kw_only=True, slots=True)
class TurnCapture:
    """One turn's files as stored in ``run_turns``: the capped texts, their sizes, the summary."""

    turn_number: int
    model: str | None
    subtype: str | None
    is_error: bool | None
    num_turns: int | None
    input_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    duration_ms: int | None
    result_text: str | None
    prompt: str
    prompt_bytes: int
    stream: str
    stream_bytes: int
    stream_lines: int
    omitted_lines: int
    stderr: str
    stderr_bytes: int
    truncated: bool


def capture_turns(log_dir: Path) -> list[TurnCapture]:
    """Every turn-N.jsonl under ``log_dir`` with its prompt and stderr, capped; never raises."""
    try:
        entries = list(log_dir.iterdir())
    except OSError:
        return []
    numbered: list[tuple[int, Path]] = []
    for entry in entries:
        match = TURN_FILE.match(entry.name)
        if match is not None:
            numbered.append((int(match.group(1)), entry))
    captures: list[TurnCapture] = []
    for number, path in sorted(numbered):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        prompt = _read(log_dir / f"turn-{number}.prompt.md")
        stderr = _read(log_dir / f"turn-{number}.stderr.log")
        captures.append(_capture(number, raw, prompt, stderr))
    return captures


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _capture(turn_number: int, raw: bytes, prompt: bytes, stderr: bytes) -> TurnCapture:
    lines = [line for line in raw.splitlines() if line.strip()]
    messages = [_message(line) for line in lines]
    stored: list[bytes] = []
    omitted = 0
    for line, message in zip(lines, messages, strict=True):
        if len(line) > LINE_LIMIT:
            omitted += 1
            line = _stub(message, len(line))
        stored.append(line)
    kept: list[bytes] = []
    total = 0
    truncated = False
    for line in stored:
        if total + len(line) + 1 > STREAM_LIMIT:
            truncated = True
            break
        kept.append(line)
        total += len(line) + 1
    result_index = _last_index(messages, "result")
    if truncated and result_index is not None and result_index >= len(kept):
        kept.append(stored[result_index])
    init_index = _last_index(messages, "system", subtype="init")
    init = messages[init_index] if init_index is not None else {}
    result = messages[result_index] if result_index is not None else {}
    usage = result.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    result_text = _string(result.get("result"))
    return TurnCapture(
        turn_number=turn_number,
        model=_string(init.get("model")),
        subtype=_string(result.get("subtype")),
        is_error=_bool(result.get("is_error")),
        num_turns=_int(result.get("num_turns")),
        input_tokens=_int(usage.get("input_tokens")),
        cache_creation_input_tokens=_int(usage.get("cache_creation_input_tokens")),
        cache_read_input_tokens=_int(usage.get("cache_read_input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cost_usd=_float(result.get("total_cost_usd")),
        duration_ms=_int(result.get("duration_ms")),
        result_text=result_text[:RESULT_TEXT_LIMIT] if result_text is not None else None,
        prompt=prompt[:PROMPT_LIMIT].decode("utf-8", errors="replace"),
        prompt_bytes=len(prompt),
        stream=(b"\n".join(kept) + b"\n").decode("utf-8", errors="replace") if kept else "",
        stream_bytes=len(raw),
        stream_lines=len(lines),
        omitted_lines=omitted,
        stderr=stderr[-STDERR_LIMIT:].decode("utf-8", errors="replace"),
        stderr_bytes=len(stderr),
        truncated=truncated,
    )


def _message(line: bytes) -> dict[str, Any] | None:
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def _stub(message: dict[str, Any] | None, size: int) -> bytes:
    original = _string(message.get("type")) if message is not None else None
    return json.dumps({"type": OMITTED_TYPE, "original_type": original, "bytes": size}).encode()


def _last_index(
    messages: list[dict[str, Any] | None], kind: str, *, subtype: str | None = None
) -> int | None:
    found = None
    for index, message in enumerate(messages):
        if message is None or message.get("type") != kind:
            continue
        if subtype is not None and message.get("subtype") != subtype:
            continue
        found = index
    return found


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
