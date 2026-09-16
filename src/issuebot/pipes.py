"""Reading a child process's pipes with a ceiling on what it may hand back.

A leaf module, like ``issuebot.dsn``, because two packages need the same primitive: the ``gh``
seam (``issuebot.github.runner``, #110) and the hook and clone seam
(``issuebot.agent.workspace``, #139) both spawn a process whose output an outside party can
grow, and ``communicate()`` buffers both pipes whole before anything looks at them. A timer
over the process bounds how long it may run, never how much it may write inside that time, so
the bytes are counted as they arrive and the caller is told at the first one past the cap --
while it can still kill the writer -- rather than after.
"""

import asyncio
from collections.abc import Callable

READ_CHUNK = 64 * 1024


async def read_capped(
    stream: asyncio.StreamReader | None, limit: int, on_overrun: Callable[[], None]
) -> bytes:
    """Read a stream to its end, keeping at most ``limit`` bytes.

    ``on_overrun`` fires once, at the first byte past the cap, so the caller can stop the
    writer; the rest is read and dropped rather than left in the pipe, since a full pipe is
    what would block the child from exiting.
    """
    if stream is None:
        return b""
    chunks: list[bytes] = []
    size = 0
    overrun = False
    while True:
        chunk = await stream.read(READ_CHUNK)
        if not chunk:
            return b"".join(chunks)
        if overrun:
            continue
        size += len(chunk)
        if size > limit:
            overrun = True
            on_overrun()
            continue
        chunks.append(chunk)
