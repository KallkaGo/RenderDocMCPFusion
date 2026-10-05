import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import uuid
import time

MCP_IDLE_TIMEOUT_SECONDS = 600.0

from .artifacts import Artifacts
from .errors import BackendTransportError, FusionError, require
from .gui import GuiBackend
from .headless import HeadlessBackend
from .extended_api import PUBLIC_TO_NATIVE, dispatch_extended


def path_identity(path):
    return os.path.normcase(os.path.realpath(str(path)))


@dataclass
class CaptureSession:
    id: str
    backend: str
    epoch: int
    path: str
    size: int
    modified_ns: int


HEADLESS_BLOCKED = {
    "open_capture", "capture_frame", "export_render_target", "export_texture", "export_buffer", "export_snapshot",
    "shader_build", "shader_replace", "shader_restore", "shader_restore_all",
    "load_capture_b", "open_capture_b", "close_capture", "close_capture_b",
}


class Router:
    def __init__(self, config, headless=None, gui=None):
        self.config = config
        self.backends = {"headless": headless or HeadlessBackend(config), "gui": gui or GuiBackend(config)}
        self.session = None
        self.artifacts = Artifacts(config.output_dir, config.inline_bytes)
        self.lock = asyncio.Lock()
        # A client may initialize this server without ever opening a capture.
        self._idle_since = time.monotonic()
        self._headless_idle_start_count = 0
        self._caps = json.loads((config.root / "runtime/headless/capabilities.json").read_text(encoding="utf-8-sig"))

    def capabilities(self, backend):
        if backend == "headless":
            return self._caps
        return {
            "engine": "fusion-gui-bridge", "renderdocVersion": "host RenderDoc (tested 1.46)",
            "capabilities": {"constantBufferValues": True, "actualDescriptorResourceBindings": True,
                "pipelineState": True, "shaderReflection": True, "shaderRawBytes": True,
                "bufferExport": True, "textureExport": True, "postVSRawExport": True},
            "limitations": ["Requires the Fusion bridge in qrenderdoc; the existing legacy MCP bridge is not used",
                "Shader values may have anonymous names when capture debug information is absent",
                "Post-VS export is transformed GPU data, not the original rigged model"],
        }

    def idle_remaining(self):
        if self.backends["headless"].alive or self.backends["gui"]._watch_active:
            self._idle_since = None
            return None
        if self._idle_since is None:
            self._idle_since = time.monotonic()
        return max(0.0, MCP_IDLE_TIMEOUT_SECONDS - (time.monotonic() - self._idle_since))

    def idle_expired(self):
        remaining = self.idle_remaining()
        # Loading/replaying/exporting must finish before idle shutdown is allowed.
        return remaining is not None and remaining <= 0 and not self.lock.locked()

    def headless_idle_remaining(self):
        headless = self.backends["headless"]
        if not headless.on_demand or headless.start_count == 0:
            return None
        return self.idle_remaining()

    def lifecycle_status(self):
        remaining = self.idle_remaining()
        gui = self.backends["gui"].lifecycle_status()
        state = ("gui_bound" if gui["monitored_pid"] is not None else
                 "headless_running" if self.backends["headless"].alive else "idle")
        return {"process_id": os.getpid(), "state": state,
                "bound_gui_pid": gui["monitored_pid"],
                "idle_timeout_seconds": MCP_IDLE_TIMEOUT_SECONDS,
                "idle_remaining_seconds": remaining}

    def status(self):
        return {"session": vars(self.session) if self.session else None,
            "backends": {name: {"available": backend.available, "connected": backend.alive,
                **self.capabilities(name)} for name, backend in self.backends.items()},
            "gui_windows": self.backends["gui"].windows(),
            "gui_startup": {
                "enabled": self.config.gui_autostart,
                "can_auto_start": getattr(self.backends["gui"], "can_autostart", False),
                "renderdoc_path": str(self.config.gui_executable) if self.config.gui_executable else None,
                "visible": self.config.gui_visible,
                "last_launch": getattr(self.backends["gui"], "launch_status", None),
            },
            "gui_lifecycle": self.backends["gui"].lifecycle_status(),
            "mcp_lifecycle": self.lifecycle_status(),
            "headless_runtime": {"mode": "on_demand" if self.backends["headless"].on_demand else "persistent",
                "state": "running" if self.backends["headless"].alive else "idle",
                "engine_running": self.backends["headless"].alive,
                "capture_remembered": bool(self.session and self.session.backend == "headless"),
                "idle_timeout_seconds": MCP_IDLE_TIMEOUT_SECONDS,
                "idle_remaining_seconds": self.headless_idle_remaining()},
            "output_directory": str(self.config.output_dir),
            "selection_policy": "Automatic GUI windows are private to this MCP. External manual windows require an explicit window_id. Headless on_demand releases its engine after each call and reopens the pinned file for the next query."}

    async def open_capture(self, capture_path, backend="auto", window_id=None, headless_mode="on_demand", allow_capture_switch=True):
        path = Path(capture_path).expanduser().resolve()
        require(path.is_file() and path.suffix.lower() == ".rdc", "CAPTURE_NOT_FOUND", "Expected an existing .rdc file")
        require(backend in ("auto", "headless", "gui"), "INVALID_BACKEND", "backend must be auto, headless, or gui")
        if backend == "auto":
            gui = self.backends["gui"]
            backend = "gui" if gui.available or getattr(gui, "can_autostart", False) or window_id else "headless"
        require(not window_id or backend == "gui", "INVALID_ARGUMENT", "window_id is only valid for the GUI backend")
        require(headless_mode in ("on_demand", "persistent"), "INVALID_ARGUMENT", "headless_mode must be on_demand or persistent")
        if backend == "headless":
            self.backends["gui"].deactivate_monitor()
            self.backends["headless"].on_demand = headless_mode == "on_demand"
        # Keep the previous GUI binding until connect successfully selects its
        # replacement. A failed GUI reconnect must not discard the exit watcher.
        # A failed open must not leave an apparently valid previous capture.
        self.session = None
        other = "gui" if backend == "headless" else "headless"
        if other == "headless":
            await self.backends[other].close()
        selected = self.backends[backend]
        if backend == "gui":
            await selected.connect(window_id)
            self._idle_since = None
            status = await selected.call("get_capture_status", {})
            current = status["data"]
            if current.get("loaded") and path_identity(current.get("path", "")) == path_identity(path):
                result = status
            else:
                require(allow_capture_switch, "CAPTURE_MISMATCH", "The selected external GUI must already hold the requested capture; Fusion will not change its file")
                result = await selected.call("open_capture", {"path": str(path), "wait": int(self.config.timeout)})
                status = await selected.call("get_capture_status", {})
            body = status["data"]
            require(body.get("loaded") and path_identity(body.get("path", "")) == path_identity(path), "CAPTURE_MISMATCH", "GUI did not load the requested capture")
        else:
            await selected.connect()
            result = await selected.call("open_capture", {"path": str(path)})
            # Check the open result rather than assuming a successful transport is a successful replay.
            require(isinstance(result, dict) and not result.get("error"), "OPEN_FAILED", "Headless did not confirm opening the capture")
        stat = path.stat()
        self.session = CaptureSession(uuid.uuid4().hex, backend, selected.epoch, str(path), stat.st_size, stat.st_mtime_ns)
        return {"session": vars(self.session), "capabilities": self.capabilities(backend), "result": result}

    async def require_session(self, *, reopen_headless=True):
        require(self.session is not None, "NO_CAPTURE", "Open a capture first")
        session = self.session
        selected = self.backends[session.backend]
        path = Path(session.path)
        try:
            stat = path.stat()
        except OSError:
            self.session = None
            raise FusionError("CAPTURE_CHANGED", "The capture file is no longer accessible")
        if (stat.st_size, stat.st_mtime_ns) != (session.size, session.modified_ns):
            self.session = None
            raise FusionError("CAPTURE_CHANGED", "The capture file changed on disk; open it again")
        if session.backend == "headless" and selected.suspended and selected.epoch == session.epoch:
            if not reopen_headless:
                return selected
            try:
                await selected.connect()
                opened = await selected.call("open_capture", {"path": session.path})
                require(isinstance(opened, dict) and not opened.get("error"), "OPEN_FAILED", "Headless replay did not reopen the pinned capture")
                session.epoch = selected.epoch
            except BaseException:
                self.session = None
                raise
        if not selected.alive or selected.epoch != session.epoch:
            self.session = None
            raise FusionError("SESSION_LOST", "Backend session changed; open the capture again")
        if session.backend == "gui":
            status = await selected.call("get_capture_status", {})
            body = status["data"]
            if not body.get("loaded") or path_identity(body.get("path", "")) != path_identity(session.path):
                self.session = None
                raise FusionError("CAPTURE_CHANGED", "The GUI changed capture; open the intended capture again")
        return selected

    def export_path(self, filename, suffix, output_path=None, directory=False):
        if output_path:
            path = Path(output_path).expanduser().resolve()
        else:
            path = self.config.output_dir / (filename + "-" + uuid.uuid4().hex[:10] + suffix)
        require(not path.exists(), "OUTPUT_EXISTS", "Refusing to overwrite an existing output: " + str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def raw_tools(self, backend=None, *, reopen_headless=True):
        if backend:
            require(backend in self.backends, "INVALID_BACKEND", "Choose gui or headless")
            selected = self.backends[backend]
            if backend == "headless" and not selected.tools:
                require(reopen_headless, "BACKEND_UNAVAILABLE", "The remembered backend catalog is unavailable")
                await selected.connect()
        else:
            selected = await self.require_session(reopen_headless=reopen_headless)
            backend = self.session.backend
        tools = selected.tools
        if backend == "headless":
            tools = {name: spec for name, spec in tools.items() if name not in HEADLESS_BLOCKED and not name.startswith("diff_")}
        return {"backend": backend, "tools": list(tools.values()), "blocked_tools": sorted(HEADLESS_BLOCKED) if backend == "headless" else [],
            "note": "Native parameters and native result shapes; capture lifetime is managed by Fusion open_capture."}

    async def dispatch(self, name, args):
        if name == "get_backend_status":
            return self.status()
        if name == "open_capture":
            return await self.open_capture(**args)
        if name == "list_backend_tools":
            return await self.raw_tools(**args)
        if name == "get_capture_status" and self.session is None:
            return {"loaded": False, "session": None}
        if name == "get_capture_status" and self.session.backend == "headless" and self.backends["headless"].suspended:
            session = self.session
            try:
                stat = Path(session.path).stat()
            except OSError:
                self.session = None
                raise FusionError("CAPTURE_CHANGED", "The capture file is no longer accessible")
            if (stat.st_size, stat.st_mtime_ns) != (session.size, session.modified_ns):
                self.session = None
                raise FusionError("CAPTURE_CHANGED", "The capture file changed on disk; open it again")
            return {"loaded": False, "session": vars(session), "replay_state": "idle",
                "engine_running": False, "will_reopen_on_query": True}
        selected = await self.require_session()
        backend = self.session.backend
        eid = args.get("event_id")
        if name in PUBLIC_TO_NATIVE:
            return await dispatch_extended(self, selected, name, args)
        if name == "get_capture_status":
            return {"session": vars(self.session), "capture": await selected.call("get_capture_status" if backend == "gui" else "get_capture_info", {})}
        if name == "list_draws":
            limit, offset = args.get("limit", 100), args.get("offset", 0)
            if backend == "gui":
                return await selected.call("find_events", {"q": args.get("filter", ""), "limit": limit, **({"eid_min": args["after_event_id"] + 1} if args.get("after_event_id") else {})})
            result = await selected.call("list_draws", {"filter": args.get("filter", ""), "limit": limit})
            return {"result": result, "limit": limit, "truncation": "upstream does not report total; a full page may have more results", "known_issue": "v0.3.0 draw names/drawIndex can be empty/zero; eventId is authoritative"}
        if name == "get_pipeline_state":
            return await selected.call("inspect_pipeline_state" if backend == "gui" else name, {"eid" if backend == "gui" else "eventId": eid})
        if name == "get_shader_info":
            mode = args.get("mode", "reflect")
            if backend == "gui":
                method = {"reflect": "inspect_shader", "disasm": "get_shader_disasm", "source": "get_shader_source", "code": "get_shader_code"}.get(mode)
                require(method is not None, "INVALID_ARGUMENT", "Unknown shader mode")
                params = {"eid": eid, "stage": args.get("stage", "ps")}
                if mode != "reflect":
                    params.update({key: args[key] for key in ("offset", "max_lines") if key in args})
                    if mode in {"source", "code"} and "file_index" in args:
                        params["file_index"] = args["file_index"]
                return await selected.call(method, params)
            require(mode in {"reflect", "disasm"}, "UNSUPPORTED_OPERATION", "Headless shader queries support reflect/disasm only")
            require(not any(key in args for key in ("offset", "max_lines", "file_index")), "UNSUPPORTED_OPERATION", "Headless shader queries do not support text pagination or source selection")
            return await selected.call("get_shader", {"eventId": eid, "stage": args.get("stage", "ps"), "mode": args.get("mode", "reflect")})
        if name == "get_bindings":
            result = await selected.call("get_draw_packet" if backend == "gui" else name, {"eid" if backend == "gui" else "eventId": eid})
            return {"binding_kind": "actual_resources" if backend == "gui" else "declarations_only", "result": result}
        if name == "get_cbuffer_data":
            require(backend == "gui", "UNSUPPORTED_OPERATION", "The bundled headless v0.3.0 cannot read constant values; explicitly reopen with backend='gui'")
            return await selected.call("inspect_cbuffer_values", {"eid": eid, "stage": args.get("stage", "ps"), **({"slot": args["slot"]} if "slot" in args else {})})
        if name == "export_buffer":
            require(backend == "gui", "UNSUPPORTED_OPERATION", "Buffer export requires GUI; headless native export writes beside the source capture and is disabled")
            path = self.export_path("buffer", ".bin", args.get("output_path"))
            return await selected.call("export_buffer", {"eid": eid, "rid": args["resource_id"], "dest": str(path), "offset": args.get("offset", 0), "length": args.get("length", 0), "overwrite": False})
        if name == "export_texture":
            require(backend == "gui", "UNSUPPORTED_OPERATION", "Texture export requires GUI; use export_render_target for headless render targets")
            fmt = args.get("format", "PNG")
            path = self.export_path("texture", "." + fmt.lower(), args.get("output_path"))
            return await selected.call("debug_save_texture", {"eid": eid, "rid": args["resource_id"], "dest": str(path), "format": fmt, "overwrite": False})
        if name == "export_mesh":
            if backend == "gui":
                path = self.export_path("postvs", "", args.get("output_path"), directory=True)
                return await selected.call("export_postvs", {"eid": eid, "dest": str(path)})
            path = self.export_path("postvs", ".obj", args.get("output_path"))
            result = await selected.call("export_mesh", {"eventId": eid, "stage": "vs-out", "format": "obj", "outputPath": str(path)})
            return {"result": result, "semantics": "post-transform xyz/triangles only; no UV, normal, skeleton or original model coordinates"}
        if name == "export_render_target":
            require(backend == "headless", "UNSUPPORTED_OPERATION", "GUI: use get_bindings then export_texture with the desired output resource ID")
            return await self.export_render_target(eid, args.get("target", 0), args.get("output_path"))
        if name == "call_backend_tool":
            native, params = args["tool_name"], dict(args.get("arguments", {}))
            catalog = (await self.raw_tools())["tools"]
            allowed = {tool["name"]: tool for tool in catalog}
            require(native in allowed, "UNSUPPORTED_TOOL", "Native tool is not in this session's allowed catalog")
            import jsonschema
            try:
                jsonschema.validate(params, allowed[native]["inputSchema"])
            except jsonschema.ValidationError as exc:
                raise FusionError("INVALID_ARGUMENT", exc.message) from exc
            if "eventId" in allowed[native]["inputSchema"].get("properties", {}):
                require("eventId" in params, "EVENT_REQUIRED", "Pass eventId explicitly; Fusion does not use implicit GUI/current-event state")
            if backend == "gui" and "dest" in params:
                params["dest"] = str(self.export_path("native", "", params["dest"]))
                if "overwrite" in params:
                    params["overwrite"] = False
            if native == "export_mesh" and backend == "headless":
                path = self.export_path("postvs", ".obj", params.get("outputPath"))
                params["outputPath"] = str(path)
            if native == "assert_image" and backend == "headless":
                if params.get("diffOutputPath"):
                    params["diffOutputPath"] = str(self.export_path("diff", ".png", params["diffOutputPath"]))
                for key in ("expectedPath", "actualPath"):
                    params[key] = str(Path(params[key]).expanduser().resolve())
            return await selected.call(native, params)
        raise FusionError("UNKNOWN_TOOL", name)

    async def export_render_target(self, event_id, target, output_path):
        require(self.config.cli_path.is_file(), "BACKEND_UNAVAILABLE", "Headless CLI not found")
        directory = self.export_path("rt", "", output_path, directory=True)
        directory.mkdir()
        process = await asyncio.create_subprocess_exec(str(self.config.cli_path), self.session.path,
            "export-rt", str(target), "-o", str(directory), "-e", str(event_id),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(self.config.cli_path.parent))
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.config.timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise FusionError("EXPORT_TIMEOUT", "One-shot CLI export did not complete")
        require(process.returncode == 0, "EXPORT_FAILED", stderr.decode("utf-8", "replace")[-4000:])
        files = [file for file in directory.iterdir() if file.is_file()]
        require(bool(files), "EXPORT_FAILED", "CLI reported success without creating a file")
        return {"files": [{"path": str(file), "byte_count": file.stat().st_size, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()} for file in files],
            "note": "CLI reopens the capture; timing includes replay startup. Byte counts were read from actual files."}

    async def release_headless_after_call(self):
        selected = self.backends["headless"]
        if not selected.on_demand:
            return
        session = self.session
        resumable = (session is not None and session.backend == "headless"
                     and session.epoch == selected.epoch and (selected.alive or selected.suspended))
        started = selected.start_count
        await selected.close()
        if started != self._headless_idle_start_count:
            self._headless_idle_start_count = started
            self._idle_since = time.monotonic()
        if resumable and self.session is session:
            selected.suspended = True
            session.epoch = selected.epoch

    async def execute(self, name, arguments):
        async with self.lock:
            try:
                value = await self.dispatch(name, arguments)
                await self.release_headless_after_call()
                bounded, meta = self.artifacts.bound_result(value)
                if self.session and self.session.backend == "headless":
                    meta["headless_runtime"] = {"mode": "on_demand" if self.backends["headless"].on_demand else "persistent",
                        "state": "running" if self.backends["headless"].alive else "idle",
                        "engine_running": self.backends["headless"].alive,
                        "idle_remaining_seconds": self.headless_idle_remaining()}
                return {"ok": True, "backend": self.session.backend if self.session else None,
                    "session_id": self.session.id if self.session else None, "data": bounded, "meta": meta}
            except BackendTransportError:
                self.session = None
                await self.release_headless_after_call()
                raise
            except Exception:
                await self.release_headless_after_call()
                raise
            except asyncio.CancelledError:
                self.session = None
                await asyncio.shield(self.backends["headless"].close())
                raise
            finally:
                # Arm a fresh idle period after a backend is released or lost;
                # status/catalog calls alone never extend an existing deadline.
                self.idle_remaining()

    async def close(self):
        await self.backends["headless"].close()
        await self.backends["gui"].close()
