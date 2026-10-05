"""One backend per capture recipe, with caller-owned explicit handles."""
import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
import time
import uuid

from .client_context import current_client
from .errors import BackendTransportError, FusionError, require
from .router import Router, path_identity
from .extended_api import PUBLIC_TO_NATIVE, list_capture_files


CAPTURE_TOOLS = {
    "get_backend_status", "get_capture_status", "list_backend_tools", "list_draws",
    "get_pipeline_state", "get_shader_info", "get_bindings", "get_cbuffer_data",
    "export_buffer", "export_texture", "export_mesh", "export_render_target",
    "call_backend_tool", "release_capture",
}
CAPTURE_TOOLS.update(PUBLIC_TO_NATIVE)
METADATA_TOOLS = {"get_backend_status", "get_capture_status", "list_backend_tools"}
INVALID_SESSION = {"NO_CAPTURE", "SESSION_LOST", "CAPTURE_CHANGED", "BACKEND_CLOSED"}


@dataclass(eq=False)
class _Entry:
    key: tuple
    recipe: dict
    fingerprint: tuple
    last_used: float
    router: object = None
    opened: dict = None
    pending: int = 0
    retired: bool = False
    failure: str = None
    handles: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SharedRouter:
    def __init__(self, config, *, router_factory=Router, idle_timeout=300,
                 clock=time.monotonic, max_captures=32, keep_alive=False):
        self.config = config
        self.router_factory = router_factory
        self.idle_timeout = idle_timeout
        self.clock = clock
        self.max_captures = max_captures
        self.keep_alive = keep_alive
        self.entries = {}
        self.handles = {}
        self._invalid_handles = {}
        self._windows = {}
        self._last_work = clock()
        self._closed = False
        self.should_exit = False

    @staticmethod
    def _caller():
        caller = current_client.get()
        require(isinstance(caller, str) and bool(caller), "CLIENT_REQUIRED",
                "Capture operations require a client connection identity")
        return caller

    @staticmethod
    def _fingerprint(path):
        try:
            stat = Path(path).stat()
        except OSError as exc:
            raise FusionError("CAPTURE_CHANGED", "The capture file is no longer accessible") from exc
        return stat.st_size, stat.st_mtime_ns

    def _recipe(self, args):
        path = Path(args.get("capture_path", "")).expanduser().resolve()
        require(path.is_file() and path.suffix.lower() == ".rdc", "CAPTURE_NOT_FOUND", "Expected an existing .rdc file")
        backend = args.get("backend", "auto")
        require(backend in {"auto", "gui", "headless"}, "INVALID_BACKEND", "Choose auto, gui, or headless")
        window = args.get("window_id")
        require(window is None or isinstance(window, str) and bool(window), "INVALID_ARGUMENT", "window_id must be a nonempty string")
        if backend == "auto":
            executable = self.config.gui_executable
            autostart = (self.config.gui_autostart and os.name == "nt" and
                         sys.maxsize > 2**32 and executable is not None and executable.is_file())
            backend = "gui" if window or autostart else "headless"
        require(not window or backend == "gui", "INVALID_ARGUMENT", "window_id requires GUI")
        mode = args.get("headless_mode", "persistent")
        require(mode in {"persistent", "on_demand"}, "INVALID_ARGUMENT", "Choose persistent or on_demand")
        recipe = {"capture_path": str(path), "backend": backend, "headless_mode": mode}
        if window:
            recipe.update(window_id=window, allow_capture_switch=False)
        fingerprint = self._fingerprint(path)
        key = (path_identity(path), fingerprint, backend, mode if backend == "headless" else None, window)
        return key, recipe, fingerprint

    def _remember_invalid(self, capture_id, owner, code):
        self._invalid_handles[capture_id] = (owner, code)
        while len(self._invalid_handles) > 256:
            self._invalid_handles.pop(next(iter(self._invalid_handles)))

    async def _drain_close(self, router):
        # Cleanup owns the backend even if the caller cancels repeatedly.
        task = asyncio.create_task(router.close())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _retire(self, entry, code="CAPTURE_NOT_FOUND"):
        entry.retired = True
        if self.entries.get(entry.key) is entry:
            self.entries.pop(entry.key)
        for window, claimed in list(self._windows.items()):
            if claimed is entry:
                self._windows.pop(window)
        for owner, capture_id in entry.handles.items():
            self.handles.pop(capture_id, None)
            self._remember_invalid(capture_id, owner, code)
        entry.handles.clear()
        router = entry.router
        if router is not None:
            try:
                await self._drain_close(router)
            finally:
                entry.router = None

    async def _invalidate(self, entry, code="CAPTURE_CHANGED"):
        if entry.recipe["backend"] != "gui" or entry.router is None:
            await self._retire(entry, code)
            return
        gui = entry.router.backends["gui"]
        if code in {"GUI_WINDOW_NOT_FOUND", "GUI_AUTOSTART_DISABLED", "GUI_EXECUTABLE_NOT_FOUND",
                    "GUI_PLATFORM_UNSUPPORTED"} and not getattr(gui, "_launch_records", {}):
            # These failures occur before spawning. An empty failed entry must
            # not occupy the pool or prevent the service's idle exit forever.
            await self._retire(entry, code)
            return
        # A GUI launch may still be alive after failure/cancellation. Keep its
        # owner and launch records for explicit retry rather than orphaning it.
        for owner, capture_id in entry.handles.items():
            self.handles.pop(capture_id, None)
            self._remember_invalid(capture_id, owner, code)
        entry.handles.clear()
        entry.opened = None
        entry.failure = code
        entry.router.session = None

    def _lookup(self, capture_id, caller):
        require(isinstance(capture_id, str) and bool(capture_id), "CAPTURE_REQUIRED", "Pass capture_id returned by open_capture")
        found = self.handles.get(capture_id)
        if found:
            owner, entry = found
            require(owner == caller, "CAPTURE_NOT_FOUND", "This connection does not own the capture handle")
            return entry
        invalid = self._invalid_handles.get(capture_id)
        if invalid and invalid[0] == caller:
            raise FusionError(invalid[1], "Capture handle expired or changed; explicitly open the capture again")
        raise FusionError("CAPTURE_NOT_FOUND", "Unknown capture handle for this connection")

    async def _validate(self, entry):
        require(not entry.retired, "CAPTURE_NOT_FOUND", "Capture was released; open it again")
        require(self._fingerprint(entry.recipe["capture_path"]) == entry.fingerprint,
                "CAPTURE_CHANGED", "The capture file changed; explicitly open it again")
        await entry.router.require_session(reopen_headless=False)

    @staticmethod
    def _response(data, capture_id=None):
        result = {"ok": True, "data": data, "meta": {}}
        if capture_id is not None:
            result["capture_id"] = capture_id
        return result

    def _connector_status(self):
        leases = getattr(self, "connector_leases", None)
        if leases is not None:
            status = leases.status()
            return {**status, "keep_alive": status["connector_count"] > 0}
        return {"lifetime": "manual_stop"}

    def status(self):
        caller = current_client.get()
        rows = []
        for entry in self.entries.values():
            row = {"capture_path": entry.recipe["capture_path"], "backend": entry.recipe["backend"],
                   "headless_mode": entry.recipe["headless_mode"], "busy": entry.pending > 0,
                   "handle_count": len(entry.handles), "idle_seconds": max(0, self.clock() - entry.last_used)}
            if caller in entry.handles:
                row["capture_id"] = entry.handles[caller]
            if entry.recipe.get("window_id"):
                row["window_id"] = entry.recipe["window_id"]
            if entry.router is not None and entry.recipe["backend"] == "gui":
                gui = entry.router.backends["gui"]
                row["window_id"] = getattr(gui, "window_id", None) or row.get("window_id")
                lifecycle = getattr(gui, "lifecycle_status", lambda: {})()
                launch = getattr(gui, "launch_status", None)
                row["gui_pid"] = lifecycle.get("monitored_pid") or (launch or {}).get("process_id")
                row["gui_startup"] = launch
            if entry.failure:
                row["failure"] = entry.failure
            rows.append(row)
        return {"service_pid": os.getpid(), "process_id": os.getpid(), "instances": rows,
                "idle_timeout_seconds": None, "headless_idle_timeout_seconds": self.idle_timeout,
                "automatic_idle_cleanup": "headless_only",
                "should_exit": self.should_exit,
                "keep_alive": self.keep_alive,
                **self._connector_status()}

    def _decorate(self, result, entry, capture_id):
        result = dict(result)
        result["capture_id"] = capture_id
        result["meta"] = dict(result.get("meta") or {})
        runtime = result["meta"].get("headless_runtime")
        if runtime is not None:
            result["meta"]["headless_runtime"] = {**runtime,
                "idle_timeout_seconds": self.idle_timeout,
                "idle_remaining_seconds": max(0, self.idle_timeout - (self.clock() - entry.last_used))}
        return result

    def _target_status(self, entry):
        data = dict(entry.router.status())
        data["service_pid"] = os.getpid()
        data["keep_alive"] = self.keep_alive
        data.update(self._connector_status())
        data["mcp_lifecycle"] = {"process_id": os.getpid(), "state": "shared",
            "idle_timeout_seconds": None, "idle_remaining_seconds": None,
            "automatic_idle_cleanup": False}
        if "headless_runtime" in data:
            data["headless_runtime"] = {**data["headless_runtime"],
                "idle_timeout_seconds": self.idle_timeout,
                "idle_remaining_seconds": max(0, self.idle_timeout - (self.clock() - entry.last_used))
                    if entry.recipe["backend"] == "headless" else None}
        return data

    async def _open(self, args, caller):
        key, recipe, fingerprint = self._recipe(args)
        entry = self.entries.get(key)
        window = recipe.get("window_id")
        claimed = self._windows.get(window) if window else None
        if claimed is not None and claimed.key[:3] == key[:3] and not claimed.retired:
            # The public window_id can select an automatically opened private
            # GUI without creating a second Router with a different IPC root.
            entry = claimed
        if claimed is not None and claimed is not entry:
            # A live external window can never be switched by another recipe.
            claimed.pending += 1
            try:
                async with claimed.lock:
                    if not claimed.retired:
                        try:
                            await self._validate(claimed)
                        except FusionError as exc:
                            if not isinstance(exc, BackendTransportError) and exc.code not in INVALID_SESSION:
                                raise
                            await self._retire(claimed, "CAPTURE_CHANGED")
                        else:
                            raise FusionError("WINDOW_IN_USE", "The selected GUI window already belongs to another capture")
            finally:
                claimed.pending -= 1
            # Another waiter may have already created this requested entry.
            entry = self.entries.get(key)
        if entry is None:
            current_claim = self._windows.get(window) if window else None
            require(current_claim is None or current_claim.retired, "WINDOW_IN_USE",
                    "The selected GUI window already belongs to another capture")
            require(len(self.entries) < self.max_captures, "CAPACITY_LIMIT", "Capture pool is full; release captures, close GUI windows, or wait for headless idle expiry")
            entry = _Entry(key, recipe, fingerprint, self.clock())
            self.entries[key] = entry
            if window:
                self._windows[window] = entry
        entry.pending += 1
        try:
            async with entry.lock:
                require(not entry.retired, "CAPTURE_NOT_FOUND", "Capture was released while opening; try again")
                try:
                    if entry.router is None:
                        entry.router = self.router_factory(self.config)
                    if entry.opened is None:
                        entry.opened = await entry.router.execute("open_capture", recipe)
                        require(entry.opened.get("ok"), "OPEN_FAILED", "Backend did not open the capture")
                        entry.failure = None
                    else:
                        await self._validate(entry)
                except FusionError as exc:
                    was_opened = entry.opened is not None
                    await self._invalidate(entry, exc.code)
                    if was_opened and (isinstance(exc, BackendTransportError) or exc.code in INVALID_SESSION):
                        # This explicit open authorizes rebuilding a lost backend.
                        # Queries with an old handle never take this path.
                        if entry.retired:
                            return await self._open(args, caller)
                        try:
                            entry.opened = await entry.router.execute("open_capture", recipe)
                            require(entry.opened.get("ok"), "OPEN_FAILED", "Backend did not open the capture")
                            entry.failure = None
                        except BaseException:
                            await self._invalidate(entry)
                            raise
                    else:
                        raise
                except BaseException:
                    await self._invalidate(entry, "CAPTURE_CHANGED")
                    raise
                capture_id = entry.handles.get(caller)
                if capture_id is None:
                    capture_id = uuid.uuid4().hex
                    entry.handles[caller] = capture_id
                    self.handles[capture_id] = (caller, entry)
                if entry.recipe["backend"] == "gui":
                    selected_window = getattr(entry.router.backends["gui"], "window_id", None)
                    if selected_window:
                        self._windows[selected_window] = entry
                entry.last_used = self._last_work = self.clock()
                return self._decorate(entry.opened, entry, capture_id)
        finally:
            entry.pending -= 1

    async def execute(self, name, args):
        require(not self._closed and not self.should_exit, "SERVICE_CLOSED", "The shared service is shutting down")
        args = dict(args or {})
        if name == "list_instances" or name == "get_backend_status" and not args.get("capture_id"):
            return self._response(self.status())
        if name == "list_captures":
            self._caller()
            return self._response(await asyncio.to_thread(list_capture_files, **args))
        require(name == "open_capture" or name in CAPTURE_TOOLS, "UNKNOWN_TOOL", name)
        caller = self._caller()
        if name == "open_capture":
            return await self._open(args, caller)
        capture_id = args.pop("capture_id", None)
        entry = self._lookup(capture_id, caller)
        entry.pending += 1
        try:
            async with entry.lock:
                # Recheck ownership after an earlier queued release or invalidation.
                self._lookup(capture_id, caller)
                if name == "release_capture":
                    entry.handles.pop(caller)
                    self.handles.pop(capture_id)
                    self._remember_invalid(capture_id, caller, "CAPTURE_NOT_FOUND")
                    if not entry.handles and entry.recipe["backend"] == "headless":
                        await self._retire(entry)
                    return self._response({"released": True, "remaining_handles": len(entry.handles)}, capture_id)
                try:
                    await self._validate(entry)
                    if name == "get_backend_status":
                        result = self._response(self._target_status(entry))
                    elif name == "list_backend_tools":
                        require(args.get("backend", entry.recipe["backend"]) == entry.recipe["backend"],
                                "INVALID_BACKEND", "A capture handle cannot inspect another backend")
                        result = self._response(await entry.router.raw_tools(reopen_headless=False))
                    else:
                        result = await entry.router.execute(name, args)
                except asyncio.CancelledError:
                    await self._invalidate(entry, "CAPTURE_CHANGED")
                    raise
                except FusionError as exc:
                    if isinstance(exc, BackendTransportError) or exc.code in INVALID_SESSION:
                        await self._invalidate(entry, "CAPTURE_CHANGED")
                    raise
                finally:
                    if name not in METADATA_TOOLS:
                        entry.last_used = self._last_work = self.clock()
                return self._decorate(result, entry, capture_id)
        finally:
            entry.pending -= 1

    async def reap_idle(self):
        if self._closed:
            return
        for entry in list(self.entries.values()):
            if entry.pending:
                continue
            headless_idle = (entry.recipe["backend"] == "headless" and self.clock() - entry.last_used >= self.idle_timeout)
            gui = entry.router.backends["gui"] if entry.router is not None and entry.recipe["backend"] == "gui" else None
            gui_exited = bool(gui is not None and getattr(gui, "process_has_exited", lambda: False)())
            if not headless_idle and not gui_exited:
                continue
            entry.pending += 1
            try:
                async with entry.lock:
                    await self._retire(entry, "CAPTURE_CHANGED" if gui_exited else "CAPTURE_NOT_FOUND")
            finally:
                entry.pending -= 1

    async def close(self):
        self._closed = True
        async def retire(entry):
            entry.pending += 1
            try:
                async with entry.lock:
                    await self._retire(entry)
            finally:
                entry.pending -= 1
        tasks = [asyncio.create_task(retire(entry)) for entry in list(self.entries.values())]
        if tasks:
            cleanup = asyncio.gather(*tasks)
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError
        self.should_exit = True
