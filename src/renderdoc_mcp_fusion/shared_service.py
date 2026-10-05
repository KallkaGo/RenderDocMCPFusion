"""Ensure one detached local Hub, kept alive by stdio connector leases.

The public health endpoint identifies an instance. Its private state file also
holds the credential required for MCP/control calls. These are local same-user
tools, without authentication boundaries between different OS users.
"""

import argparse
import asyncio
from contextlib import contextmanager
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from .config import Config
from . import __version__


START_TIMEOUT = 20.0
LOCK_TIMEOUT = 25.0
STOP_TIMEOUT = 35.0
INSTANCE_ID = re.compile(r"[0-9a-f]{32}\Z")


def service_dir(config=None):
    override = os.environ.get("RENDERDOC_FUSION_SERVICE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/share")
    return base / "RenderDocMCPFusion" / "shared-service"


def _state_path(config):
    return service_dir(config) / "hub.json"


def _cancel_path(config, instance_id):
    if not isinstance(instance_id, str) or not INSTANCE_ID.fullmatch(instance_id):
        raise ValueError("Invalid Hub instance identity")
    return service_dir(config) / ("cancel-" + instance_id)


def public_record(record):
    return {key: value for key, value in record.items() if key not in ("token", "process_token")}


def read_record(config):
    try:
        record = json.loads(_state_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    return record


def _valid_record(record):
    return (isinstance(record, dict) and isinstance(record.get("instance_id"), str)
            and INSTANCE_ID.fullmatch(record["instance_id"])
            and isinstance(record.get("pid"), int) and not isinstance(record["pid"], bool) and record["pid"] > 0
            and isinstance(record.get("port"), int) and not isinstance(record["port"], bool)
            and 1 <= record["port"] <= 65535 and isinstance(record.get("token"), str) and bool(record["token"]))


def _require_compatible(config, record):
    if os.path.normcase(str(config.root.resolve())) != os.path.normcase(str(record.get("root", ""))):
        raise RuntimeError("Existing Hub uses a different resource root; no duplicate was started")
    if record.get("version") != __version__:
        raise RuntimeError("Existing Hub uses a different Fusion version; stop that Hub before updating")


def _publish(config, record):
    path = _state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("hub." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(record, output, ensure_ascii=False)
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_own(config, record):
    current = read_record(config)
    if current and all(current.get(key) == record.get(key) for key in ("instance_id", "pid", "token")):
        _state_path(config).unlink(missing_ok=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _request(record, *, stop=False):
    if not _valid_record(record):
        raise ValueError("Unrecognized private Hub record")
    headers = {"Accept": "application/json"}
    if stop:
        headers["Authorization"] = "Bearer " + record["token"]
    request = urllib.request.Request("http://127.0.0.1:%s/%s" % (record["port"], "shutdown" if stop else "health"),
                                     headers=headers, method="POST" if stop else "GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=1.0) as response:
        return json.load(response)


def record_is_live(record):
    try:
        health = _request(record)
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return (isinstance(health, dict) and health.get("service") == "renderdoc-mcp-fusion"
            and health.get("transport") == "streamable-http"
            and health.get("version") == record.get("version")
            and health.get("instance_id") == record["instance_id"] and health.get("pid") == record["pid"])


def update_connector(record, client_id, *, release=False):
    """Refresh or release one connector lease on an existing Hub only."""
    if not _valid_record(record):
        raise ValueError("Unrecognized private Hub record")
    if not isinstance(client_id, str) or not INSTANCE_ID.fullmatch(client_id):
        raise ValueError("Invalid connector identity")
    request = urllib.request.Request(
        "http://127.0.0.1:%s/connectors" % record["port"],
        data=json.dumps({"client_id": client_id, "action": "release" if release else "heartbeat"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + record["token"]},
        method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=2.0) as response:
        return json.load(response)


def _process_ref(config, pid, token=None):
    path = config.root / "bridge_extension" / "process_lifecycle.py"
    spec = importlib.util.spec_from_file_location("fusion_shared_process", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ProcessRef.open(pid, expected_creation_token=token)


def process_is_running(config, record):
    """Return True/False/None (unknown), never infer death from a timeout."""
    if not _valid_record(record):
        return None
    if os.name != "nt":
        try:
            os.kill(record["pid"], 0)
            # Confirm the Linux creation identity where available.
            if record.get("process_token"):
                current = Path("/proc/%s/stat" % record["pid"]).read_text().rsplit(")", 1)[1].split()[19]
                return current == record["process_token"]
            return None
        except ProcessLookupError:
            return False
        except (OSError, ValueError, IndexError):
            return None
    if not record.get("process_token"):
        return None
    ref = None
    try:
        ref = _process_ref(config, record["pid"], record["process_token"])
        return not ref.exited()
    except ValueError:
        return False
    except OSError as exc:
        return False if getattr(exc, "winerror", None) in (87, 1168) else None
    finally:
        if ref is not None:
            ref.close()


@contextmanager
def _file_lock(config, name, timeout=LOCK_TIMEOUT):
    directory = service_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("a+b", buffering=0) as lock:
        deadline = time.monotonic() + timeout
        while True:
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another connector is still preparing the shared Hub")
                time.sleep(0.05)
        try:
            # Windows can lock beyond EOF. Initialize the sentinel only while
            # owning its byte, so simultaneous first starters cannot write a
            # byte another process has already locked.
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def _singleton_lock(config, timeout=LOCK_TIMEOUT):
    with _file_lock(config, "hub.lock", timeout=timeout):
        yield


def _runtime_lock_held(config):
    try:
        with _file_lock(config, "hub.runtime.lock", timeout=0):
            return False
    except TimeoutError:
        return True


def _explorer_identity(config):
    """Refuse desktop delegation across user identities or Windows sessions."""
    import ctypes
    from ctypes import wintypes
    import win32api
    import win32con
    import win32security

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetShellWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    hwnd = user.GetShellWindow()
    pid = wintypes.DWORD()
    shell_thread = user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) if hwnd else 0
    if not hwnd or not shell_thread:
        raise RuntimeError("No interactive Explorer desktop is available for shared Hub startup")
    ref = _process_ref(config, pid.value)
    try:
        expected = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"
        if os.path.normcase(str(Path(ref.executable).resolve())) != os.path.normcase(str(expected.resolve())):
            raise RuntimeError("The desktop shell is not the Windows Explorer process")
        process = win32api.OpenProcess(0x1000, False, pid.value)
        try:
            own_token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            shell_token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
            try:
                own_sid = win32security.GetTokenInformation(own_token, win32security.TokenUser)[0]
                shell_sid = win32security.GetTokenInformation(shell_token, win32security.TokenUser)[0]
                if own_sid != shell_sid:
                    raise RuntimeError("Shared Hub startup requires the same OS user as the Explorer desktop; cross-user delegation was refused")
            finally:
                own_token.Close()
                shell_token.Close()
        finally:
            process.Close()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel.ProcessIdToSessionId.restype = wintypes.BOOL
        own_session, shell_session = wintypes.DWORD(), wintypes.DWORD()
        if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(own_session)) or not kernel.ProcessIdToSessionId(pid.value, ctypes.byref(shell_session)):
            raise ctypes.WinError(ctypes.get_last_error())
        if own_session.value != shell_session.value:
            raise RuntimeError("Shared Hub startup requires the same interactive session as Explorer")
        user.GetThreadDesktop.argtypes = [wintypes.DWORD]
        user.GetThreadDesktop.restype = wintypes.HANDLE
        user.GetProcessWindowStation.restype = wintypes.HANDLE
        user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                  wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        user.GetUserObjectInformationW.restype = wintypes.BOOL

        def object_name(handle):
            value = ctypes.create_unicode_buffer(256)
            required = wintypes.DWORD()
            if not handle or not user.GetUserObjectInformationW(handle, 2, value, ctypes.sizeof(value), ctypes.byref(required)):
                raise ctypes.WinError(ctypes.get_last_error())
            return value.value

        own_desktop = object_name(user.GetThreadDesktop(kernel.GetCurrentThreadId()))
        shell_desktop = object_name(user.GetThreadDesktop(shell_thread))
        if object_name(user.GetProcessWindowStation()).lower() != "winsta0" or own_desktop.lower() != "default" or own_desktop != shell_desktop:
            raise RuntimeError("Shared Hub startup requires Explorer's ordinary interactive desktop; isolated desktops were refused")
        if ref.exited():
            raise RuntimeError("Explorer exited before shared Hub startup")
        return int(hwnd), ref
    except BaseException:
        ref.close()
        raise


class _BrokerLaunch:
    def poll(self):
        # ShellExecute has no owned subprocess handle. Readiness and ownership
        # are established by the random Hub identity and lifetime file lock.
        return None


def _spawn(arguments, **options):
    if os.name != "nt":
        return subprocess.Popen(arguments, **options)
    from .hub_bootstrap import ENVIRONMENT_KEYS
    import pythoncom
    import pywintypes
    import win32com.client.dynamic
    from win32com.shell import shell

    config = Config.from_environment()
    hwnd, owner = _explorer_identity(config)
    specification = None
    windows = desktop = provider = browser = view = background = application = None
    pythoncom.CoInitialize()
    try:
        # The desktop view's Application belongs to the existing Explorer,
        # unlike a newly activated Shell.Application in the connector process.
        windows = pythoncom.CoCreateInstance(pywintypes.IID("{9BA05972-F6A8-11CF-A442-00A0C90A8F39}"),
                                             None, pythoncom.CLSCTX_LOCAL_SERVER, pythoncom.IID_IDispatch)
        desktop, desktop_hwnd = windows.InvokeTypes(
            1610743816, 0, pythoncom.DISPATCH_METHOD, (9, 0),
            ((16396, 1), (16396, 1), (3, 1), (16387, 2), (3, 1)), pythoncom.Empty, pythoncom.Empty, 8, 0, 1)
        import ctypes
        from ctypes import wintypes
        shell_pid = wintypes.DWORD()
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        if not desktop or not user.GetWindowThreadProcessId(desktop_hwnd, ctypes.byref(shell_pid)) or shell_pid.value != owner.pid:
            raise RuntimeError("Explorer COM desktop identity did not match the verified shell")
        provider = desktop.QueryInterface(pythoncom.IID_IServiceProvider)
        browser = provider.QueryService(shell.SID_STopLevelBrowser, shell.IID_IShellBrowser)
        browser_hwnd = browser.GetWindow()
        if not user.GetWindowThreadProcessId(browser_hwnd, ctypes.byref(shell_pid)) or shell_pid.value != owner.pid:
            raise RuntimeError("Explorer browser identity did not match the verified shell")
        view = browser.QueryActiveShellView()
        background = win32com.client.dynamic.Dispatch(view.GetItemObject(0, pythoncom.IID_IDispatch))
        application = background.Application
        if owner.exited():
            raise RuntimeError("Explorer exited before shared Hub startup")
        from .named_runtime import prepare_hub_executable
        executable = prepare_hub_executable(service_dir(config))
        bootstrap = Path(__file__).with_name("hub_bootstrap.py").resolve()
        environment = {key: options["env"][key] for key in ENVIRONMENT_KEYS if key != "RENDERDOC_FUSION_SERVICE_DIR"}
        environment["RENDERDOC_FUSION_SERVICE_DIR"] = str(service_dir(config).resolve())
        instance_id = json.loads(environment["RENDERDOC_FUSION_HUB_SPEC"])["instance_id"]
        specification = service_dir(config) / ("bootstrap-" + instance_id + ".json")
        with specification.open("x", encoding="utf-8") as output:
            json.dump({"environment": environment}, output)
        specification.chmod(0o600)
        application.ShellExecute(str(executable), subprocess.list2cmdline([str(bootstrap), str(specification.resolve())]),
                                 str(config.root.resolve()), "open", 0)
        return _BrokerLaunch()
    except BaseException:
        if specification is not None:
            specification.unlink(missing_ok=True)
        raise
    finally:
        # Release apartment-bound interfaces before leaving their COM apartment.
        application = background = view = browser = provider = desktop = windows = None
        owner.close()
        pythoncom.CoUninitialize()


def _child_environment(config, spec):
    return {**os.environ, "RENDERDOC_FUSION_HUB_SPEC": json.dumps(spec),
            "RENDERDOC_FUSION_ROOT": str(config.root),
            "RENDERDOC_FUSION_OUTPUT_DIR": str(config.output_dir),
            "RENDERDOC_FUSION_IPC_DIR": str(config.ipc_dir),
            "RENDERDOC_FUSION_ENGINE": str(config.engine_path),
            "RENDERDOC_FUSION_CLI": str(config.cli_path),
            "RENDERDOC_FUSION_TIMEOUT": str(config.timeout),
            "RENDERDOC_FUSION_INLINE_BYTES": str(config.inline_bytes),
            "RENDERDOC_FUSION_RENDERDOC": str(config.gui_executable or ""),
            "RENDERDOC_FUSION_GUI_AUTOSTART": "1" if config.gui_autostart else "0",
            "RENDERDOC_FUSION_GUI_VISIBLE": "1" if config.gui_visible else "0",
            "RENDERDOC_FUSION_GUI_START_TIMEOUT": str(config.gui_start_timeout)}


def ensure_service(config, client_id=None):
    """Find/start the Hub and optionally register a connector before returning."""
    if client_id is not None and (not isinstance(client_id, str) or not INSTANCE_ID.fullmatch(client_id)):
        raise ValueError("Invalid connector identity")
    with _singleton_lock(config):
        record = read_record(config)
        if record is not None:
            if record_is_live(record):
                _require_compatible(config, record)
                if client_id is not None:
                    update_connector(record, client_id)
                return record
            if process_is_running(config, record) is not False:
                _require_compatible(config, record)
                raise RuntimeError("Shared Hub is unresponsive; its exit is not confirmed, so no duplicate was started")
            _remove_own(config, record)
        elif _state_path(config).exists():
            raise RuntimeError("Private Hub state cannot be read; refusing to start a duplicate")
        spec = {"instance_id": uuid.uuid4().hex, "token": secrets.token_urlsafe(32)}
        log_path = service_dir(config) / "hub.log"
        options = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True})
        with log_path.open("ab") as log:
            process = _spawn([sys.executable, "-m", "renderdoc_mcp_fusion.shared_service", "serve"],
                             env=_child_environment(config, spec), stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, close_fds=True, **options)
        try:
            deadline = time.monotonic() + START_TIMEOUT
            while time.monotonic() < deadline:
                record = read_record(config)
                if record is not None:
                    _require_compatible(config, record)
                    if record_is_live(record):
                        # A previous launcher may have died before its detached
                        # Hub published. The lifetime lock still kept it unique.
                        if client_id is not None:
                            update_connector(record, client_id)
                        return record
                if process.poll() is not None and not _runtime_lock_held(config):
                    raise RuntimeError("Shared Hub failed to start; inspect " + str(log_path))
                time.sleep(0.05)
            raise TimeoutError("Shared Hub startup timed out; inspect " + str(log_path))
        except BaseException:
            _cancel_path(config, spec["instance_id"]).touch()
            (service_dir(config) / ("bootstrap-" + spec["instance_id"] + ".json")).unlink(missing_ok=True)
            record = read_record(config)
            if record is not None and record.get("instance_id") == spec["instance_id"] and record_is_live(record):
                try:
                    _request(record, stop=True)
                except (OSError, ValueError, urllib.error.URLError):
                    pass
            raise


def status(config):
    record = read_record(config)
    if record is None:
        state = "unresponsive" if _state_path(config).exists() else "stopped"
    elif record_is_live(record):
        state = "running"
    else:
        state = "stopped" if process_is_running(config, record) is False else "unresponsive"
    return {"ok": True, "state": state, "service": public_record(record) if record else None}


def stop_service(config):
    record = read_record(config)
    if record is None:
        if _state_path(config).exists():
            raise RuntimeError("Private Hub state cannot be read; no shutdown request was sent")
        return {"ok": True, "running": False}
    if not record_is_live(record):
        if process_is_running(config, record) is False:
            _remove_own(config, record)
            return {"ok": True, "running": False}
        _require_compatible(config, record)
        raise RuntimeError("Hub identity could not be verified; no shutdown request was sent")
    _require_compatible(config, record)
    _request(record, stop=True)
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        current = read_record(config)
        if current is None or current.get("instance_id") != record["instance_id"] or process_is_running(config, record) is False:
            return {"ok": True, "running": False, "stopped_pid": record["pid"]}
        time.sleep(0.05)
    raise TimeoutError("Hub shutdown is still in progress")


async def serve_service(config, spec, *, router_factory=None, reap_interval=1.0):
    # This second lock is owned by the actual detached process for its complete
    # lifetime, independently of the starter's short-lived hub.lock handle.
    ownership = _file_lock(config, "hub.runtime.lock", timeout=0)
    try:
        ownership.__enter__()
    except TimeoutError:
        return  # Another actual Hub owns the lifetime lock; never bind a second.
    try:
        await _serve_service(config, spec, router_factory=router_factory, reap_interval=reap_interval)
    finally:
        ownership.__exit__(None, None, None)


async def _serve_service(config, spec, *, router_factory=None, reap_interval=1.0):
    """Bind one endpoint, publish only when ready, and release all Hub resources."""
    # Heavy SDK imports belong only to the detached Hub, not each connector.
    import uvicorn
    from .http_service import HOST, bind_listener, create_http_app
    from .server import create_server

    cancel = _cancel_path(config, spec["instance_id"])
    if cancel.exists():
        cancel.unlink(missing_ok=True)
        return
    if router_factory is None:
        from .shared_router import SharedRouter
        router_factory = SharedRouter
    from .connector_leases import ConnectorLeases
    leases = ConnectorLeases()
    try:
        router = router_factory(config)
    except BaseException:
        leases.close()
        raise
    router.connector_leases = leases
    router.keep_alive = False
    listener = None
    serving = supervisor = None
    record = None
    try:
        listener = bind_listener(0)
        port = listener.getsockname()[1]
        process_token = None
        if os.name == "nt":
            try:
                ref = _process_ref(config, os.getpid())
                try:
                    process_token = ref.creation_token
                finally:
                    ref.close()
            except OSError:
                pass
        else:
            try:
                process_token = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
            except (OSError, IndexError):
                pass
        record = {**spec, "pid": os.getpid(), "port": port, "process_token": process_token,
                  "root": str(config.root.resolve()), "version": __version__,
                  "url": "http://127.0.0.1:%s/mcp" % port}
        runner = None

        def shutdown():
            runner.should_exit = True

        app = create_http_app(create_server(router=router, manage_lifespan=False), router,
                              port=port, token=spec["token"], instance_id=spec["instance_id"],
                              shutdown_callback=shutdown, reap_interval=reap_interval,
                              connector_registry=leases)
        runner = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, workers=1,
                                             lifespan="on", ws="none", proxy_headers=False,
                                             access_log=False, log_config=None, timeout_graceful_shutdown=30))
        serving = asyncio.create_task(runner.serve(sockets=[listener]))

        async def supervise():
            try:
                while not runner.started:
                    if serving.done() or cancel.exists():
                        shutdown()
                        return
                    await asyncio.sleep(0.01)
                while not await asyncio.to_thread(record_is_live, record):
                    if serving.done() or cancel.exists():
                        shutdown()
                        return
                    await asyncio.sleep(0.05)
                if cancel.exists():
                    shutdown()
                    return
                _publish(config, record)
                while True:
                    await asyncio.sleep(0.5)
                    leases.poll()
                    router.keep_alive = leases.status()["connector_count"] > 0
                    if cancel.exists() or leases.should_exit:
                        router.should_exit = True
                        shutdown()
                        return
            except Exception:
                shutdown()
                raise

        supervisor = asyncio.create_task(supervise())
        try:
            await asyncio.shield(serving)
        finally:
            shutdown()
            if not serving.done():
                await asyncio.shield(serving)
            if not runner.started and hasattr(runner, "lifespan"):
                await runner.lifespan.shutdown()
            supervisor.cancel()
            result = (await asyncio.gather(supervisor, return_exceptions=True))[0]
            if isinstance(result, Exception):
                raise result
    finally:
        leases.close()
        if listener is not None:
            listener.close()
        if serving is None:
            await router.close()
        if record is not None:
            _remove_own(config, record)
        cancel.unlink(missing_ok=True)


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Local shared RenderDoc MCP Hub control")
    parser.add_argument("action", choices=("serve", "status", "stop"))
    args = parser.parse_args()
    config = Config.from_environment()
    try:
        if args.action == "serve":
            spec = json.loads(os.environ["RENDERDOC_FUSION_HUB_SPEC"])
            asyncio.run(serve_service(config, spec))
            return
        result = stop_service(config) if args.action == "stop" else status(config)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
