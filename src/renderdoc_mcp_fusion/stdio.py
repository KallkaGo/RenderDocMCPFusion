"""MCP stdio transport whose idle stdin read does not delay cancellation."""

import asyncio
from contextlib import asynccontextmanager
import os
import sys
import threading

from mcp.server.stdio import stdio_server as _stdio_server


__all__ = ["cancellable_stdio_server"]


class _CancellableStdin:
    """Read stdin on a daemon without holding Python's buffered-stream lock."""

    def __init__(self):
        self._loop = asyncio.get_running_loop()
        self._fd = sys.stdin.fileno()
        self._queue = asyncio.Queue()
        self._stopped = threading.Event()
        # Keep at most one queued line, plus the current os.read() chunk. A slow
        # MCP consumer should still apply backpressure to its client.
        self._delivery_slot = threading.Semaphore(1)
        self._thread = threading.Thread(
            target=self._read, name="fusion-stdio-reader", daemon=True
        )
        self._thread.start()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._stopped.is_set():
            raise StopAsyncIteration
        item = await self._queue.get()
        self._delivery_slot.release()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item

    def _enqueue(self, item):
        # This callback runs on the event loop. It must not schedule coroutines:
        # shutdown may have cancelled all consumers or already closed the loop.
        if self._stopped.is_set():
            self._delivery_slot.release()
            return
        self._queue.put_nowait(item)

    def _deliver(self, item):
        self._delivery_slot.acquire()
        if self._stopped.is_set():
            self._delivery_slot.release()
            return False
        try:
            self._loop.call_soon_threadsafe(self._enqueue, item)
        except RuntimeError:  # The event loop has already closed.
            self._delivery_slot.release()
            return False
        return True

    def _read(self):
        pending = b""
        try:
            while not self._stopped.is_set():
                # Do not use sys.stdin.buffer here. A daemon blocked while
                # holding its BufferedReader lock can abort interpreter exit.
                chunk = os.read(self._fd, 65536)
                if self._stopped.is_set():
                    return
                if not chunk:
                    if pending and not self._deliver(
                        pending.decode("utf-8", errors="replace")
                    ):
                        return
                    self._deliver(None)
                    return
                lines = (pending + chunk).split(b"\n")
                pending = lines.pop()
                for line in lines:
                    if not self._deliver(
                        (line + b"\n").decode("utf-8", errors="replace")
                    ):
                        return
        except Exception as exc:
            self._deliver(exc)

    def close(self):
        if self._stopped.is_set():
            return
        self._stopped.set()
        # Wake a producer waiting to deliver, and a consumer waiting for a line.
        # A producer blocked in os.read remains a daemon until data or exit.
        self._delivery_slot.release()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)
        # Deliberately neither close process stdin nor join the reader thread.


@asynccontextmanager
async def cancellable_stdio_server():
    """Use the SDK's MCP framing with a cancellable, asyncio-backed stdin."""
    stdin = _CancellableStdin()
    try:
        async with _stdio_server(stdin=stdin) as streams:
            try:
                yield streams
            finally:
                stdin.close()
    finally:
        stdin.close()
