"""A persistent MCP child, with its entire context lifetime in one owner task."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .errors import BackendTransportError, FusionError


def unpack_result(result):
    data = result.structuredContent
    if data is None:
        texts = [block.text for block in result.content if block.type == "text"]
        joined = "\n".join(texts)
        try:
            data = json.loads(joined)
        except (ValueError, TypeError):
            data = {"text": joined}
    if result.isError:
        raise FusionError("BACKEND_ERROR", "Headless backend rejected the operation", data)
    return data


class HeadlessBackend:
    name = "headless"

    def __init__(self, config, *, command=None, args=None):
        self.config = config
        self.command = command or str(config.engine_path)
        self.args = args or []
        self.tools = {}
        self.info = {}
        self.epoch = 0
        self.on_demand = True
        self.start_count = 0
        self.suspended = False
        self._task = None
        self._queue = None
        self._ready = None
        self._accepting = False
        self._connect_lock = asyncio.Lock()

    @property
    def available(self):
        return Path(self.command).is_file()

    @property
    def alive(self):
        return self._accepting and self._task is not None and not self._task.done() and self._ready.done() and self._ready.exception() is None

    async def connect(self):
        async with self._connect_lock:
            await self._connect()

    async def _connect(self):
        if self.alive:
            return
        # A failing owner can still be inside the SDK's process teardown.
        # Finish it before replacing queue/ready fields with a new generation.
        if self._task is not None:
            await self.close()
        if not self.available:
            raise FusionError("BACKEND_UNAVAILABLE", "Headless engine not found: " + self.command)
        self.suspended = False
        self._queue = asyncio.Queue()
        self._ready = asyncio.get_running_loop().create_future()
        self._ready.add_done_callback(lambda ready: ready.exception() if not ready.cancelled() else None)
        self.epoch += 1
        self.start_count += 1
        self._task = asyncio.create_task(self._run(), name="fusion-headless-owner")
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), timeout=self.config.timeout + 10)
        except BaseException:
            await self.close()
            raise

    async def _run(self):
        pending = None
        failure = BackendTransportError("BACKEND_CLOSED", "Headless session ended; open the capture again")
        try:
            params = StdioServerParameters(command=self.command, args=self.args,
                cwd=str(Path(self.command).parent), env=dict(os.environ))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=self.config.timeout)) as client:
                    initialized = await client.initialize()
                    self.info = initialized.serverInfo.model_dump()
                    listed = await client.list_tools()
                    self.tools = {t.name: t.model_dump(exclude_none=True) for t in listed.tools}
                    self._accepting = True
                    self._ready.set_result(True)
                    while True:
                        job = await self._queue.get()
                        if job is None:
                            break
                        name, arguments, pending = job
                        try:
                            result = await client.call_tool(name, arguments,
                                read_timeout_seconds=timedelta(seconds=self.config.timeout))
                            value = unpack_result(result)
                        except FusionError as exc:
                            if not pending.done():
                                pending.set_exception(exc)
                        except Exception as exc:
                            self._accepting = False
                            self.epoch += 1
                            failure = BackendTransportError("BACKEND_TRANSPORT", "Headless connection failed or timed out; session invalidated", str(exc))
                            if not pending.done():
                                pending.set_exception(failure)
                            break
                        else:
                            if not pending.done():
                                pending.set_result(value)
                        finally:
                            pending = None
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                failure = BackendTransportError("BACKEND_TRANSPORT", "Could not run headless backend", str(exc))
        finally:
            self._accepting = False
            self.epoch += 1
            if not self._ready.done():
                self._ready.set_exception(failure)
            if pending is not None and not pending.done():
                pending.set_exception(failure)
            while self._queue is not None and not self._queue.empty():
                job = self._queue.get_nowait()
                if job is not None and not job[2].done():
                    job[2].set_exception(failure)

    async def call(self, name, arguments):
        if not self.alive:
            raise BackendTransportError("SESSION_LOST", "Headless session is not running; open the capture again")
        if name not in self.tools:
            raise FusionError("UNSUPPORTED_TOOL", "Headless backend does not expose " + name)
        future = asyncio.get_running_loop().create_future()
        await self._queue.put((name, arguments, future))
        try:
            return await future
        except BackendTransportError:
            await asyncio.shield(self.close())
            raise
        except asyncio.CancelledError:
            await asyncio.shield(self.close())
            raise

    async def close(self):
        self.suspended = False
        task = self._task
        self._accepting = False
        if task is not None and not task.done():
            await self._queue.put(None)
            try:
                await asyncio.wait_for(asyncio.shield(task), 5)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self._task is task:
            self._task = None
