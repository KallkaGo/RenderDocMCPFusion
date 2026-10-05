#!/usr/bin/env python3
"""Start an isolated RenderDoc Fusion GUI session (Windows x64, Python 3.11+).

Print one JSON object. A nonzero exit means the bridge was not confirmed ready.
After launch, every failure includes the owned process ID for manual inspection;
the launcher never terminates a RenderDoc process or installs an extension.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


def default_renderdoc() -> str:
    return str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "RenderDoc" / "qrenderdoc.exe")


def _set_window_visibility(pid, visible):
    """Show/restore only this newly launched PID's unique Qt main window.

    Confirmation describes Win32 visibility in the launching desktop/session;
    it does not claim that a window on another desktop is visible to the user.
    """
    import ctypes
    from ctypes import wintypes
    try:
        user = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.EnumWindows.restype = wintypes.BOOL
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetWindow.restype = wintypes.HWND
        user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user.GetWindowLongW.restype = wintypes.LONG
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetClassNameW.restype = ctypes.c_int
        for name in ("IsWindowVisible", "IsIconic"):
            function = getattr(user, name)
            function.argtypes = [wintypes.HWND]
            function.restype = wintypes.BOOL
        user.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user.ShowWindowAsync.restype = wintypes.BOOL
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user.GetWindowRect.restype = wintypes.BOOL
        candidates = []

        @callback_type
        def visit(hwnd, _data):
            owner_pid = wintypes.DWORD()
            if not user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid)) or owner_pid.value != pid:
                return True
            if user.GetWindow(hwnd, 4) or user.GetWindowLongW(hwnd, -16) & 0x40000000:
                return True
            if user.GetWindowLongW(hwnd, -20) & 0x00000080:
                return True
            name = ctypes.create_unicode_buffer(256)
            if user.GetClassNameW(hwnd, name, len(name)) and re.fullmatch(r"Qt[56][0-9]*QWindowIcon", name.value):
                candidates.append(int(hwnd))
            return True

        if not user.EnumWindows(visit, 0):
            return {"confirmed": False, "reason": "window_enumeration_failed"}
        if len(candidates) != 1:
            return {"confirmed": False, "reason": "main_window_not_found" if not candidates else "ambiguous_main_windows"}
        hwnd = candidates[0]
        # Recheck ownership immediately before affecting this HWND.
        owner_pid = wintypes.DWORD()
        if not user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid)) or owner_pid.value != pid:
            return {"confirmed": False, "reason": "window_owner_changed"}
        if visible:
            if not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd):
                user.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
                user.ShowWindowAsync(hwnd, 5)  # SW_SHOW
            rect = wintypes.RECT()
            confirmed = (bool(user.IsWindowVisible(hwnd)) and not user.IsIconic(hwnd)
                         and bool(user.GetWindowRect(hwnd, ctypes.byref(rect)))
                         and rect.right > rect.left and rect.bottom > rect.top)
        else:
            user.ShowWindowAsync(hwnd, 0)  # SW_HIDE
            confirmed = not bool(user.IsWindowVisible(hwnd))
        return {"confirmed": bool(confirmed), "visible": bool(user.IsWindowVisible(hwnd)),
                "hwnd": hwnd, "reason": None if confirmed else "visibility_not_confirmed"}
    except (OSError, AttributeError) as exc:
        return {"confirmed": False, "reason": "visibility_check_failed", "error": str(exc)}


def launch_gui(*, renderdoc: str, capture: str | None = None,
               window_id: str | None = None, visible: bool = True,
               timeout: float = 60.0, ipc_dir: str | Path | None = None,
               on_spawn=None, state_dir: str | Path | None = None) -> dict:
    """Validate paths, spawn one GUI, and wait for its unique bootstrap status."""
    root = Path(__file__).resolve().parents[1]
    window_id = window_id if window_id is not None else "fusion-" + uuid.uuid4().hex[:12]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", window_id):
        raise ValueError("Window ID accepts letters, numbers, hyphens and underscores only")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Timeout must be a finite positive number of seconds")

    executable = Path(renderdoc).expanduser().resolve()
    bootstrap = root / "bridge_extension" / "bootstrap.py"
    bridge = root / "bridge_extension" / "renderdoc_fusion_bridge"
    for label, path in (("RenderDoc executable", executable), ("Bootstrap", bootstrap),
                        ("Fusion bridge", bridge / "__init__.py")):
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")
    capture_path = Path(capture).expanduser().resolve() if capture else None
    if capture_path is not None and not capture_path.is_file():
        raise FileNotFoundError(f"Capture not found: {capture_path}")

    state_root = Path(state_dir).expanduser().resolve() if state_dir is not None else root / ".runtime-state"
    state_root.mkdir(parents=True, exist_ok=True)
    status_path = state_root / f"{window_id}-{uuid.uuid4().hex}.json"
    environment = os.environ.copy()
    environment.update({
        "RENDERDOC_FUSION_BRIDGE_DIR": str(bridge),
        "RENDERDOC_FUSION_BOOTSTRAP_STATUS": str(status_path),
        "RENDERDOC_FUSION_WINDOW_ID": window_id,
        "RENDERDOC_FUSION_CAPTURE": str(capture_path) if capture_path is not None else "",
    })
    # A RenderDoc window survives MCP/hub shutdown and must not inherit a guardian.
    environment.pop("RENDERDOC_FUSION_OWNER_PID", None)
    environment.pop("RENDERDOC_FUSION_OWNER_TOKEN", None)
    if ipc_dir is not None:
        environment["RENDERDOC_FUSION_IPC_DIR"] = str(Path(ipc_dir).expanduser().resolve())
    # A GUI child must neither write non-JSONRPC output to the MCP stream nor
    # keep the server's pipe handles alive after it exits.
    popen_options = {"cwd": str(root), "env": environment, "shell": False,
                     "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                     "stderr": subprocess.DEVNULL}
    if not visible:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        popen_options["startupinfo"] = startup
    process = subprocess.Popen([str(executable), "--python", str(bootstrap)], **popen_options)
    if on_spawn is not None:
        on_spawn(process)
    payload = {"process_id": process.pid, "window_id": window_id,
               "status_file": str(status_path), "result": None}
    deadline = time.monotonic() + timeout
    status_error = None
    while True:
        try:
            result = json.loads(status_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict) or not isinstance(result.get("ready"), bool):
                raise ValueError("Bootstrap status must contain a boolean ready field")
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            # The bootstrap writes directly: a first read can see a partial JSON file.
            status_error = str(exc)
        else:
            payload["result"] = result
            if result["ready"]:
                visibility = _set_window_visibility(process.pid, visible)
                result["window"] = visibility
                if visibility["confirmed"]:
                    return payload
            else:
                payload.update(error_code="bootstrap_failed", error=(
                    "Fusion bridge is not ready. Inspect the status file and owned "
                    f"RenderDoc process {process.pid}: {result.get('error', 'bootstrap reported failure')}"))
                return payload

        exit_code = process.poll()
        if exit_code is not None:
            payload.update(error_code="process_exited", error=(
                f"Owned RenderDoc process {process.pid} exited with code {exit_code} "
                "before bridge readiness was confirmed."), exit_code=exit_code)
            return payload
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if payload["result"] and payload["result"].get("ready"):
                payload.update(error_code="window_visibility_failed", error=(
                    f"Bridge is ready but the requested window visibility was not confirmed for PID {process.pid}: "
                    + str(payload["result"].get("window", {}).get("reason"))))
                return payload
            detail = f" Last status read error: {status_error}" if status_error else ""
            payload.update(error_code="timeout", error=(
                f"Timed out after {timeout:g} seconds. Owned RenderDoc process {process.pid} "
                "was left running; inspect it and the status file." + detail))
            return payload
        time.sleep(min(0.2, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", help="Optional capture file to load")
    parser.add_argument("--renderdoc", default=default_renderdoc(), help="Path to qrenderdoc.exe")
    parser.add_argument("--ipc-dir", help="Isolated Fusion bridge IPC directory")
    parser.add_argument("--window-id", default="fusion-" + uuid.uuid4().hex[:12])
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument("--visible", dest="visible", action="store_true", help="Show the new RenderDoc window (default)")
    visibility.add_argument("--hidden", dest="visible", action="store_false", help="Hide the new RenderDoc main window")
    parser.set_defaults(visible=True)
    parser.add_argument("--timeout", type=float, default=60.0, help="Readiness timeout in seconds (default: 60)")
    args = parser.parse_args(argv)
    try:
        if os.name != "nt" or sys.maxsize <= 2**32:
            raise RuntimeError("This launcher requires Windows x64 and a 64-bit Python interpreter")
        if sys.version_info < (3, 11):
            raise RuntimeError("This launcher requires Python 3.11 or newer")
        payload = launch_gui(renderdoc=args.renderdoc, capture=args.capture,
                             window_id=args.window_id, visible=args.visible, timeout=args.timeout, ipc_dir=args.ipc_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        payload = {"process_id": None, "window_id": args.window_id, "status_file": None,
                   "result": None, "error_code": "launch_failed", "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if "error" not in payload else 1


if __name__ == "__main__":
    raise SystemExit(main())
