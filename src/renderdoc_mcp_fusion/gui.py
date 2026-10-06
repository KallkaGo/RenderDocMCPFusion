import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import importlib.util
import os
import sys
import uuid

from .errors import BackendTransportError, FusionError


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


def field(kind, **kwargs):
    return {"type": kind, **kwargs}


EID = field("integer", minimum=1)
STAGE = field("string", enum=["vs", "hs", "ds", "gs", "ps", "cs"])
# This catalog deliberately exposes supported analysis operations, not every
# editor mutation or optional decompiler bundled by the upstream repository.
GUI_SCHEMAS = {
    "get_capture_status": schema(),
    "find_events": schema({"q": field("string"), "marker": field("string"), "eid_min": EID, "eid_max": EID, "limit": field("integer", minimum=1, maximum=10000)}),
    "inspect_pipeline_state": schema({"eid": EID}, ["eid"]),
    "get_draw_packet": schema({"eid": EID}, ["eid"]),
    "inspect_shader": schema({"eid": EID, "stage": STAGE}, ["eid", "stage"]),
    "inspect_cbuffer_values": schema({"eid": EID, "stage": STAGE, "slot": field("integer", minimum=0), "raw": field("boolean")}, ["eid", "stage"]),
    "export_shader_raw_bytes": schema({"eid": EID, "stage": STAGE, "dest": field("string")}, ["eid", "stage", "dest"]),
    "export_buffer": schema({"eid": EID, "rid": field("string"), "dest": field("string"), "offset": field("integer", minimum=0), "length": field("integer", minimum=0), "overwrite": field("boolean")}, ["eid", "rid", "dest"]),
    "debug_save_texture": schema({"eid": EID, "rid": field("string"), "dest": field("string"), "format": field("string", enum=["PNG", "HDR", "DDS"]), "overwrite": field("boolean")}, ["eid", "rid", "dest"]),
    "export_postvs": schema({"eid": EID, "dest": field("string"), "first_instance": field("integer", minimum=0), "instance_count": field("integer", minimum=1), "view": field("integer", minimum=0)}, ["eid", "dest"]),
}
from .extended_api import GUI_EXTRA_SCHEMAS
GUI_SCHEMAS.update(GUI_EXTRA_SCHEMAS)


class GuiBackend:
    name = "gui"

    def __init__(self, config):
        self.config = config
        if str(config.root) not in sys.path:
            sys.path.insert(0, str(config.root))
        from vendor.gui_client import LiveBridgeClient, LiveBridgeError
        self._error_type = LiveBridgeError
        # Each capture entry discovers its own launched window by default.
        # The GUI itself survives hub/client shutdown.
        self.private_ipc_dir = config.ipc_dir / "sessions" / uuid.uuid4().hex
        def client_at(directory):
            client = LiveBridgeClient(timeout=config.timeout)
            client.ipc_dir = directory
            client.requests_dir = directory / "requests"
            client.responses_dir = directory / "responses"
            client.heartbeat_file = directory / "heartbeat"
            return client
        self._private_client = client_at(self.private_ipc_dir)
        self._external_client = client_at(config.ipc_dir)
        self.client = self._private_client
        self._process_ref = None
        self._process_refs = []
        self._watch_active = False
        self._process_helpers = None
        self.window_id = None
        self.epoch = 0
        self.launch_status = None
        self._start_task = None
        self._launch_records = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fusion-gui")
        self.tools = {name: {"name": name, "description": "Fusion GUI bridge: " + name, "inputSchema": spec} for name, spec in GUI_SCHEMAS.items()}

    @property
    def available(self):
        return self._private_client.available()

    @property
    def can_autostart(self):
        executable = self.config.gui_executable
        return (self.config.gui_autostart and os.name == "nt"
                and sys.maxsize > 2**32 and executable is not None
                and executable.is_file())

    @property
    def alive(self):
        return (self.window_id is not None and not self.process_has_exited()
                and self.client.available(self.window_id))

    def windows(self):
        windows = []
        for client, owned in ((self._private_client, True), (self._external_client, False)):
            for window in client.list_windows()["data"]["windows"]:
                windows.append({**window, "owned_by_this_mcp": owned,
                    "requires_explicit_window_id": not owned})
        return windows

    def _helpers(self):
        if self._process_helpers is None:
            path = self.config.root / "bridge_extension" / "process_lifecycle.py"
            spec = importlib.util.spec_from_file_location("fusion_process_lifecycle", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._process_helpers = module
        return self._process_helpers

    def deactivate_monitor(self):
        self._watch_active = False

    def process_has_exited(self):
        try:
            if self._process_ref is not None:
                return self._process_ref.exited() if self._watch_active else False
            records = {id(record): record for record in self._launch_records.values()}.values()
            records = list(records)
            # Popen owns a stable Windows process handle even when bootstrap
            # failed before discovery could establish a ProcessRef.
            return bool(records) and all(record["task"].done() and (
                record["process"].poll() is not None if record.get("process") is not None
                else record.get("status", {}).get("state") == "failed") for record in records)
        except OSError:
            # Failure to inspect a process is never proof that it exited.
            return False

    def lifecycle_status(self):
        return {"synchronization": "observe_gui_exit",
            "monitored_pid": self._process_ref.pid if self._watch_active and self._process_ref else None,
            "auto_started_windows_are_private": True,
            "auto_started_windows_closed_on_mcp_exit": False,
            "borrowed_windows_closed_on_mcp_exit": False}

    def _matches(self, window_id):
        instances = self._private_client.list_instances()
        if window_id is not None:
            instances += self._external_client.list_instances()
            return [i for i in instances if window_id in (
                i.bridge_id, str(i.info.get("window_id")), str(i.info.get("bridge_id")))]
        return instances

    def _select(self, matches):
        if len(matches) > 1:
            raise FusionError("GUI_SELECTION_REQUIRED", "Select one GUI window_id", self.windows())
        if matches:
            instance = matches[0]
            try:
                process = self._helpers().ProcessRef.open(
                    int(instance.info["pid"]), expected_executable=str(self.config.gui_executable),
                    started_before=instance.info.get("started_at"))
            except (OSError, ValueError, KeyError) as exc:
                raise FusionError("GUI_PROCESS_UNVERIFIED", "Cannot verify the GUI process identity", str(exc)) from exc
            self._watch_active = False
            if self._process_ref is not None:
                self._process_ref.close()
                self._process_refs.remove(self._process_ref)
            self._process_refs.append(process)
            self._process_ref = process
            self._watch_active = True
            self.client = (self._private_client if instance.ipc_dir.is_relative_to(self.private_ipc_dir)
                           else self._external_client)
            self.window_id = instance.bridge_id
            self.epoch += 1
            return True
        return False

    def _launch_gui(self, window_id, on_spawn):
        # The launcher already owns path validation, environment isolation and
        # bounded readiness polling. Load the copy belonging to this project.
        path = self.config.root / "scripts" / "launch_gui.py"
        spec = importlib.util.spec_from_file_location("fusion_gui_launcher", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.launch_gui(
            renderdoc=str(self.config.gui_executable), window_id=window_id,
            visible=self.config.gui_visible, timeout=self.config.gui_start_timeout,
            ipc_dir=self.private_ipc_dir, on_spawn=on_spawn, state_dir=self.private_ipc_dir / "startup")

    async def _start_gui(self, window_id, record):
        loop = asyncio.get_running_loop()
        status = record["status"]

        def on_spawn(process):
            record["process"] = process
            status["process_id"] = process.pid

        def launch():
            # Start the budget when this worker begins, excluding time queued
            # behind another target's launch or bridge call.
            record["deadline"] = loop.time() + self.config.gui_start_timeout
            return self._launch_gui(window_id, on_spawn)

        try:
            payload = await loop.run_in_executor(self._executor, launch)
            status.update(payload)
            if payload.get("error") or not (payload.get("result") or {}).get("ready"):
                status["state"] = "failed"
                code = "GUI_START_TIMEOUT" if payload.get("error_code") == "timeout" else "GUI_START_FAILED"
                raise FusionError(code, payload.get("error") or "GUI bootstrap did not confirm readiness", status)
            # Bootstrap readiness and the first heartbeat are separate writes.
            # Discovery requires readable process identity as well as heartbeat.
            # Wait for the launched ID itself, then let connect apply selection.
            launched_id = payload["window_id"]
            while not self._matches(launched_id):
                remaining = record["deadline"] - loop.time()
                if remaining <= 0:
                    status.update(state="failed", error_code="discovery_timeout",
                                              error="GUI bootstrap is ready but its bridge is not discoverable")
                    raise FusionError("GUI_START_TIMEOUT", status["error"], status)
                await asyncio.sleep(min(0.1, remaining))
            status["state"] = "ready"
        except FusionError:
            raise
        except Exception as exc:
            status.update(state="failed", error_code="launch_failed", error=str(exc))
            raise FusionError("GUI_START_FAILED", "Could not start the Fusion GUI bridge", status) from exc

    def _check_autostart(self):
        if not self.config.gui_autostart:
            raise FusionError("GUI_AUTOSTART_DISABLED", "No matching GUI bridge is available and GUI auto-start is disabled")
        if os.name != "nt" or sys.maxsize <= 2**32:
            raise FusionError("GUI_PLATFORM_UNSUPPORTED", "GUI auto-start requires Windows x64")
        executable = self.config.gui_executable
        if executable is None or not executable.is_file():
            raise FusionError("GUI_EXECUTABLE_NOT_FOUND", "Set RENDERDOC_FUSION_RENDERDOC to an existing qrenderdoc.exe",
                              {"renderdoc_path": str(executable) if executable is not None else None})

    async def connect(self, window_id=None):
        # Discovery always comes first, including after a failed or timed-out
        # launch: a process left running may have become ready in the meantime.
        if self._select(self._matches(window_id)):
            return
        def confirmed_exit(record):
            process = record["process"]
            return record["task"].done() and (
                process.poll() is not None if process is not None
                else record.get("status", {}).get("state") == "failed")

        if window_id is None:
            # A default request shares an existing owned launch even when it
            # began with an explicit ID, or its first heartbeat is not ready.
            records = {id(record): record for record in self._launch_records.values()}
            candidates = []
            for previous in records.values():
                if confirmed_exit(previous):
                    for key in [key for key, value in self._launch_records.items() if value is previous]:
                        del self._launch_records[key]
                else:
                    candidates.append(previous)
            if len(candidates) > 1:
                raise FusionError("GUI_SELECTION_REQUIRED", "Select one GUI window_id",
                                  {"windows": self.windows(), "launches": [r["status"] for r in candidates]})
            record = candidates[0] if candidates else None
            if record is not None:
                self._launch_records[None] = record
        else:
            record = self._launch_records.get(window_id)
            if record is not None and confirmed_exit(record):
                # Drop every alias of the dead attempt before replacing it.
                # In particular, None must not retain a previous default PID.
                for key in [key for key, value in self._launch_records.items() if value is record]:
                    del self._launch_records[key]
                record = None
        if record is None:
            if window_id is not None:
                raise FusionError("GUI_WINDOW_NOT_FOUND", "The selected GUI window is not available",
                                  {"requested_window_id": window_id, "windows": self.windows()})
            self._check_autostart()
            target = "fusion-" + uuid.uuid4().hex[:12]
            record = {"process": None, "status": {
                "window_id": target, "process_id": None, "status_file": None,
                "result": None, "state": "starting"}}
            self.launch_status = record["status"]
            self._start_task = asyncio.create_task(self._start_gui(target, record))
            record["task"] = self._start_task
            self._launch_records[window_id] = record
            self._launch_records[target] = record
            # A canceled caller leaves the bounded launch running for reuse.
            # Retrieve exceptions even if no later caller waits on this task.
            self._start_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.shield(record["task"])
        if not self._select(self._matches(window_id)):
            raise FusionError("GUI_WINDOW_NOT_FOUND", "No matching GUI window is available after auto-start",
                              {"requested_window_id": window_id, "launch": record["status"], "windows": self.windows()})

    async def call(self, name, arguments):
        if self.window_id is None:
            raise BackendTransportError("SESSION_LOST", "No Fusion GUI window is selected")
        window_id = self.window_id
        client, process = self.client, self._process_ref
        pending = asyncio.get_running_loop().run_in_executor(self._executor,
            lambda: client.call(name, arguments, window_id=window_id,
                                process_exited=process.exited if process else None))
        try:
            result = await asyncio.shield(pending)
        except asyncio.CancelledError:
            self.epoch += 1
            self.window_id = None
            # A GUI replay call cannot be forcibly canceled. Drain the bounded
            # bridge call before releasing the gateway's serialization lock.
            try:
                await asyncio.shield(pending)
            except Exception:
                pass
            raise
        except self._error_type as exc:
            self.epoch += 1
            self.window_id = None
            raise BackendTransportError("GUI_TRANSPORT", "GUI bridge connection failed; open the capture again", str(exc)) from exc
        if not isinstance(result, dict) or "ok" not in result:
            self.epoch += 1
            self.window_id = None
            raise BackendTransportError("GUI_PROTOCOL", "GUI returned an invalid result envelope")
        if not result["ok"]:
            if (result.get("err") or {}).get("code") == "replay_fatal_error":
                self.epoch += 1
                self.window_id = None
                raise FusionError("SESSION_LOST", "Native replay failed; open the capture again in a new GUI window", result.get("err"))
            raise FusionError("BACKEND_ERROR", "GUI bridge rejected the operation", result.get("err"))
        # Preserve upstream cap/truncation and state-limit metadata.
        return result

    async def close(self):
        tasks = {record["task"] for record in self._launch_records.values()}
        if tasks:
            await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        # Release our connection and process references. RenderDoc remains under
        # the user's control even after the last MCP client or hub has exited.
        self._executor.shutdown(wait=True)
        self._watch_active = False
        for process in self._process_refs:
            process.close()
        self._process_refs.clear()
        self._process_ref = None
        self._launch_records.clear()
        self._start_task = None
        self.window_id = None
        self.epoch += 1
