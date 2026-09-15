"""Tests for the capped pipe reader shared by the two subprocess seams (#110, #139)."""

import asyncio

from issuebot.pipes import READ_CHUNK, read_capped


def _stream(*chunks: bytes) -> asyncio.StreamReader:
    stream = asyncio.StreamReader()
    for chunk in chunks:
        stream.feed_data(chunk)
    stream.feed_eof()
    return stream


async def test_output_inside_the_cap_is_returned_whole() -> None:
    calls = 0

    def on_overrun() -> None:
        nonlocal calls
        calls += 1

    data = await read_capped(_stream(b"a" * 10, b"b" * 10), 20, on_overrun)
    assert data == b"a" * 10 + b"b" * 10
    assert calls == 0


async def test_overrun_keeps_the_cap_and_fires_once() -> None:
    """The point of the cap: what is kept is bounded however much the writer sends, and the
    caller hears about it at the first byte past it -- while it can still stop the writer."""
    calls = 0

    def on_overrun() -> None:
        nonlocal calls
        calls += 1

    chunks = [b"x" * READ_CHUNK] * 40
    data = await read_capped(_stream(*chunks), 4 * READ_CHUNK, on_overrun)
    assert len(data) == 4 * READ_CHUNK
    assert calls == 1


async def test_the_stream_is_drained_past_the_cap() -> None:
    """The excess is read and dropped rather than left in the pipe: a full pipe is what would
    keep a killed child's group from exiting.

    The read that crosses the cap is dropped whole, so what comes back is at most the limit
    rather than exactly it -- the callers keep a tail of diagnostics or refuse the response
    outright, and neither cares where inside a 64 KiB read the boundary fell.
    """
    stream = _stream(b"y" * (3 * READ_CHUNK))
    assert await read_capped(stream, 2 * READ_CHUNK, lambda: None) == b"y" * (2 * READ_CHUNK)
    assert stream.at_eof()


async def test_a_missing_stream_reads_as_nothing() -> None:
    assert await read_capped(None, 10, lambda: None) == b""
