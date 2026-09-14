"""Capture a run's turn files (stream-json, prompt, stderr) for the database, scrubbed and capped.

The runner writes ``turn-N.jsonl``, ``turn-N.prompt.md`` and ``turn-N.stderr.log`` under a run's
log directory. ``capture_turns`` reads them once, passes every text through the ``Scrubber``,
applies the size caps and parses the summary the dashboard shows. It never raises: an
unreadable directory yields nothing, an unreadable stream file skips its turn, a missing
prompt or stderr file is empty.

It is the one scrubbing step for these files. They are the agent's stdout tee'd byte for
byte, and issuebot put its own ``GH_TOKEN`` into that process's environment, so nothing
downstream -- the ``run_turns`` rows, the dashboard's raw views, the committed fixture --
may take a file as it is: every persisted copy is this function's output. The prompt,
stderr and result-text caps run after scrubbing, so none can leave the head or tail of a
credential at its edge; the stream's two caps are whole-line (a stub for an oversized line,
a head of lines) and run before it, on the raw bytes, which is also why a mask that grows
a value can leave the stored stream a few bytes over ``STREAM_LIMIT``. The byte counts
report the files as they are on disk.

Every file is read through the boundary (#104): the run directory is the worker's own and the
files in it are the worker's tee, but the directory sits inside a workspace the session has
had its uid in, so each name is opened without following a link, refused unless it is a
regular file the worker owns, and read to the artefact's limit and no further -- the stream
from its head, stderr from its tail. A stream past the head limit is a session that printed
more than any transcript holds, and the session decides how much claude prints, so the
summary is not left to that: the last ``STREAM_TAIL_LIMIT`` bytes are read as well and the
last ``result`` line in them is the one the summary (cost, tokens, subtype) is parsed from
and kept beside the head, exactly as a result past ``STREAM_LIMIT`` was already kept.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from issuebot.agent.boundary import TURN_PROMPT, TURN_STDERR, TURN_STREAM, Boundary, ReadBack
from issuebot.agent.scrub import DEFAULT_SCRUBBER, Scrubber

PROMPT_LIMIT = 256 * 1024
LINE_LIMIT = 64 * 1024
STREAM_LIMIT = 2 * 1024 * 1024
STDERR_LIMIT = 64 * 1024
RESULT_TEXT_LIMIT = 4 * 1024
# The tail read behind a stream past `TURN_STREAM.limit`: room for the result line and the
# lines claude prints before it (a result line over LINE_LIMIT is stubbed but still parsed).
STREAM_TAIL_LIMIT = 1024 * 1024
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


def capture_turns(
    log_dir: Path,
    *,
    scrubber: Scrubber = DEFAULT_SCRUBBER,
    boundary: Boundary | None = None,
) -> list[TurnCapture]:
    """Every turn-N.jsonl under ``log_dir`` with its prompt and stderr, scrubbed and capped;
    never raises."""
    boundary = boundary or Boundary.current()
    try:
        names = [entry.name for entry in log_dir.iterdir()]
    except OSError:
        return []
    numbered: list[tuple[int, str]] = []
    for name in names:
        match = TURN_FILE.match(name)
        if match is not None:
            numbered.append((int(match.group(1)), name))
    captures: list[TurnCapture] = []
    for number, name in sorted(numbered):
        try:
            stream = boundary.read(log_dir, (name,), TURN_STREAM)
            tail = None
            if stream.truncated:
                tail = boundary.read(
                    log_dir, (name,), TURN_STREAM, keep="tail", limit=STREAM_TAIL_LIMIT
                ).data
        except OSError:
            continue
        prompt = _read(boundary, log_dir, f"turn-{number}.prompt.md")
        stderr = _read(boundary, log_dir, f"turn-{number}.stderr.log")
        captures.append(_capture(number, stream, prompt, stderr, scrubber, tail=tail))
    return captures


def _read(boundary: Boundary, log_dir: Path, name: str) -> ReadBack:
    artefact = TURN_STDERR if name.endswith(".stderr.log") else TURN_PROMPT
    keep = "tail" if artefact is TURN_STDERR else "head"
    try:
        return boundary.read(log_dir, (name,), artefact, keep=keep)
    except OSError:
        return ReadBack(data=b"", size=0)


def _capture(
    turn_number: int,
    stream: ReadBack,
    prompt: ReadBack,
    stderr: ReadBack,
    scrubber: Scrubber,
    *,
    tail: bytes | None = None,
) -> TurnCapture:
    raw = stream.data
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
    if tail is not None:
        # The head read was cut: the result line, if there is one, lies in the tail. Its
        # first line may be a fragment, which parses as nothing and is passed over.
        tail_lines = [line for line in tail.splitlines() if line.strip()]
        tail_messages = [_message(line) for line in tail_lines]
        tail_index = _last_index(tail_messages, "result")
        if tail_index is not None:
            line = tail_lines[tail_index]
            if len(line) > LINE_LIMIT:
                omitted += 1
                line = _stub(tail_messages[tail_index], len(line))
            lines.append(tail_lines[tail_index])
            messages.append(tail_messages[tail_index])
            stored.append(line)
            result_index = len(stored) - 1
            truncated = True
    if truncated and result_index is not None and result_index >= len(kept):
        kept.append(stored[result_index])
    init_index = _last_index(messages, "system", subtype="init")
    init = messages[init_index] if init_index is not None else {}
    result = messages[result_index] if result_index is not None else {}
    usage = result.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    result_text = _string(result.get("result"))
    if result_text is not None:
        result_text = scrubber.scrub(result_text)[:RESULT_TEXT_LIMIT]
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
        result_text=result_text,
        prompt=_head(scrubber.scrub(_text(prompt.data)), PROMPT_LIMIT),
        prompt_bytes=prompt.size,
        stream=scrubber.scrub(_text(b"\n".join(kept) + b"\n")) if kept else "",
        stream_bytes=stream.size,
        stream_lines=len(lines),
        omitted_lines=omitted,
        stderr=_tail(scrubber.scrub(_text(stderr.data)), STDERR_LIMIT),
        stderr_bytes=stderr.size,
        truncated=truncated or stream.truncated,
    )


def _text(data: bytes) -> str:
    """Decode with replacement, then swap out NUL: PostgreSQL rejects ``\\x00`` in ``text``,
    and it is valid UTF-8 so ``errors="replace"`` alone would let it through."""
    return data.decode("utf-8", errors="replace").replace("\x00", "�")


def _head(text: str, limit: int) -> str:
    """The first ``limit`` bytes of ``text`` as UTF-8, a character split by the cut dropped."""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _tail(text: str, limit: int) -> str:
    """The last ``limit`` bytes of ``text`` as UTF-8, a character split by the cut dropped."""
    return text.encode("utf-8")[-limit:].decode("utf-8", errors="ignore")


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
