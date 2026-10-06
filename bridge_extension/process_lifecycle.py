"""Exact Windows process references and normal RenderDoc window shutdown.

Compatible with qrenderdoc's Python 3.8; no Qt bindings or extra dependencies.
A process handle stays attached to its original process across PID reuse.
"""
import ctypes
from ctypes import wintypes
import math
import os
import re
import threading
import time

_SYNCHRONIZE = 0x00100000
_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_FILETIME_UNIX_EPOCH = 116444736000000000
_GW_OWNER = 4
_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_CHILD = 0x40000000
_WS_EX_TOOLWINDOW = 0x00000080
_WM_CLOSE = 0x0010
_QT_MAIN_WINDOW_CLASS = re.compile(r'Qt[56][0-9]*QWindowIcon\Z')
_api = None
_api_lock = threading.Lock()
_parent_guards = []


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_size_t),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', wintypes.LONG),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', wintypes.WCHAR * 260),
    ]


class _WindowsAPI:
    def __init__(self):
        if os.name != 'nt':
            raise OSError('RenderDoc process lifecycle requires Windows')
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        self.kernel.Process32FirstW.restype = wintypes.BOOL
        self.kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        self.kernel.Process32NextW.restype = wintypes.BOOL
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [self.enum_callback, wintypes.LPARAM]
        self.user.EnumWindows.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user.GetWindow.restype = wintypes.HWND
        self.user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetClassNameW.restype = ctypes.c_int
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.GetWindowLongW.restype = wintypes.LONG
        self.user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user.PostMessageW.restype = wintypes.BOOL


def _windows_api():
    global _api
    with _api_lock:
        if _api is None:
            _api = _WindowsAPI()
        return _api


def _normalized_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


class ProcessRef:
    """Owned SYNCHRONIZE | QUERY_LIMITED_INFORMATION process handle."""
    def __init__(self, pid, handle, creation_token, executable):
        self.pid = pid
        self.creation_token = creation_token
        self.executable = executable
        self._handle = handle
        self._lock = threading.RLock()

    @classmethod
    def open(cls, pid, expected_creation_token=None, expected_executable=None, started_before=None):
        if isinstance(pid, bool):
            raise ValueError('Process PID must be a positive DWORD')
        numeric_pid = int(pid)
        if str(numeric_pid) != str(pid) or not 0 < numeric_pid <= 0xFFFFFFFF:
            raise ValueError('Process PID must be a positive DWORD')
        api = _windows_api()
        handle = api.kernel.OpenProcess(_SYNCHRONIZE | _QUERY_LIMITED_INFORMATION, False, numeric_pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            creation, exit_time, kernel_time, user_time = [wintypes.FILETIME() for _ in range(4)]
            if not api.kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                raise ctypes.WinError(ctypes.get_last_error())
            ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            token = str(ticks)
            if expected_creation_token is not None and str(expected_creation_token) != token:
                raise ValueError('Process creation token mismatch for PID {}'.format(numeric_pid))
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not api.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            executable = buffer.value
            if expected_executable is not None and _normalized_path(executable) != _normalized_path(expected_executable):
                raise ValueError('Process executable mismatch for PID {}'.format(numeric_pid))
            if started_before is not None:
                cutoff = float(started_before)
                if not math.isfinite(cutoff) or cutoff <= 0:
                    raise ValueError('Bridge started_at must be finite positive Unix seconds')
                # A bridge cannot belong to a process created after that bridge.
                if (ticks - _FILETIME_UNIX_EPOCH) / 10000000.0 > cutoff:
                    raise ValueError('Process was created after bridge started_at for PID {}'.format(numeric_pid))
            return cls(numeric_pid, handle, token, executable)
        except BaseException:
            api.kernel.CloseHandle(handle)
            raise

    def exited(self):
        with self._lock:
            if self._handle is None:
                raise RuntimeError('Process reference is closed')
            result = _windows_api().kernel.WaitForSingleObject(self._handle, 0)
            if result == _WAIT_OBJECT_0:
                return True
            if result == _WAIT_TIMEOUT:
                return False
            if result == _WAIT_FAILED:
                raise ctypes.WinError(ctypes.get_last_error())
            raise OSError('Unexpected process wait result: {}'.format(result))

    def close(self):
        with self._lock:
            if self._handle is not None:
                if not _windows_api().kernel.CloseHandle(self._handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _parent_process_id(pid):
    api = _windows_api()
    snapshot = api.kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value or not snapshot:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not api.kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            error = ctypes.get_last_error()
            if error == 18:  # ERROR_NO_MORE_FILES
                return None
            raise ctypes.WinError(error)
        while True:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID) or None
            if not api.kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error == 18:
                    return None
                raise ctypes.WinError(error)
    finally:
        if not api.kernel.CloseHandle(snapshot):
            raise ctypes.WinError(ctypes.get_last_error())


def get_client_process_ref():
    """Best-effort stable reference to the process that launched this MCP.

    Skip exactly one known pip console launcher. Never guess that arbitrary
    Python, shell, or Node ancestors are the intended client. Discovery failure
    disables this extra monitor rather than preventing server startup.
    """
    child = None
    wrapper = None
    candidate = None
    try:
        child = ProcessRef.open(os.getpid())
        parent_pid = _parent_process_id(child.pid)
        if parent_pid is None:
            return None
        candidate = ProcessRef.open(parent_pid)
        if int(candidate.creation_token) > int(child.creation_token):
            return None  # Parent PID was reused after this child was created.
        if os.path.basename(candidate.executable).lower() in ('rdc-mcp-fusion.exe', 'renderdoc-mcp-fusion.exe'):
            wrapper = candidate
            candidate = None
            parent_pid = _parent_process_id(wrapper.pid)
            if parent_pid is None:
                return None
            candidate = ProcessRef.open(parent_pid)
            if int(candidate.creation_token) > int(wrapper.creation_token):
                return None
        result = candidate
        candidate = None  # Transfer this open handle to the caller.
        return result
    except Exception:
        return None
    finally:
        for ref in (candidate, wrapper, child):
            if ref is not None:
                try:
                    ref.close()
                except Exception:
                    pass


def _is_main_window(api, hwnd, pid):
    actual_pid = wintypes.DWORD()
    if not api.user.GetWindowThreadProcessId(hwnd, ctypes.byref(actual_pid)) or actual_pid.value != pid:
        return False
    if api.user.GetWindow(hwnd, _GW_OWNER):
        return False
    name = ctypes.create_unicode_buffer(256)
    if not api.user.GetClassNameW(hwnd, name, len(name)) or not _QT_MAIN_WINDOW_CLASS.fullmatch(name.value):
        return False
    if api.user.GetWindowLongW(hwnd, _GWL_STYLE) & _WS_CHILD:
        return False
    if api.user.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
        return False
    return True


def _main_windows(api, pid):
    candidates = []
    errors = []

    @api.enum_callback
    def callback(hwnd, _lparam):
        try:
            if _is_main_window(api, hwnd, pid):
                candidates.append(int(hwnd))
        except Exception as exc:
            errors.append(exc)
            return False
        return True

    ctypes.set_last_error(0)
    success = api.user.EnumWindows(callback, 0)
    if errors:
        raise errors[0]
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())
    return candidates


def request_gui_close(ref):
    """Post one normal WM_CLOSE to one verified main window, or refuse.

    A successful request may be cancelled by RenderDoc's unsaved-work prompt.
    Callers must never turn a refused or cancelled request into process killing.
    """
    result = {'requested': False, 'exited': False, 'reason': None, 'pid': ref.pid, 'hwnd': None}
    # Keep close() from invalidating the handle during the identity checks.
    with ref._lock:
        if ref.exited():
            result.update(exited=True, reason='process_exited')
            return result
        if os.path.basename(ref.executable).lower() != 'qrenderdoc.exe':
            result['reason'] = 'not_renderdoc_executable'
            return result
        api = _windows_api()
        candidates = _main_windows(api, ref.pid)
        if len(candidates) != 1:
            result['reason'] = 'main_window_not_found' if not candidates else 'ambiguous_main_windows'
            return result
        hwnd = candidates[0]
        # Refresh uniqueness and the exact HWND immediately before posting.
        if _main_windows(api, ref.pid) != [hwnd] or not _is_main_window(api, hwnd, ref.pid):
            result['reason'] = 'main_window_changed'
            return result
        if ref.exited():
            result.update(exited=True, reason='process_exited')
            return result
        result['hwnd'] = hwnd
        if not api.user.PostMessageW(hwnd, _WM_CLOSE, 0, 0):
            result['reason'] = 'post_message_failed:{}'.format(ctypes.get_last_error())
            return result
        result.update(requested=True, reason='normal_close_requested')
        return result


class _ParentGuard:
    def __init__(self, owner, gui, close_marker=None):
        self.close_marker = close_marker
        self.owner = owner
        self.gui = gui
        self.result = None
        self.error = None
        self.thread = threading.Thread(target=self._watch, name='RenderDocFusionParentGuard', daemon=True)

    def _close_already_requested(self):
        if not self.close_marker:
            return False
        try:
            with open(self.close_marker, 'r', encoding='utf-8') as stream:
                return stream.read() == self.owner.creation_token
        except (OSError, UnicodeError):
            return False

    def _watch(self):
        try:
            while not self.owner.exited():
                time.sleep(0.5)
            deadline = time.monotonic() + 30.0
            while True:
                if self._close_already_requested():
                    self.result = {'requested': False, 'exited': False,
                                   'reason': 'normal_close_already_requested',
                                   'pid': self.gui.pid, 'hwnd': None}
                    return
                self.result = request_gui_close(self.gui)
                # Only wait for a main window that has not appeared yet. Once
                # posted, never close again if the user cancels a save prompt.
                if self.result['reason'] != 'main_window_not_found' or time.monotonic() >= deadline:
                    return
                time.sleep(0.5)
        except Exception as exc:
            self.error = str(exc)
        finally:
            for ref in (self.gui, self.owner):
                try:
                    ref.close()
                except Exception as exc:
                    if self.error is None:
                        self.error = str(exc)


def install_parent_guard(owner_pid, owner_creation_token, close_marker=None):
    """Validate owner identity synchronously, then watch its actual exit."""
    if not owner_creation_token:
        raise ValueError('Parent guardian requires an owner creation token')
    if int(owner_pid) == os.getpid():
        raise ValueError('RenderDoc cannot be its own lifecycle owner')
    for existing in _parent_guards:
        if existing.owner.pid == int(owner_pid) and existing.owner.creation_token == str(owner_creation_token):
            return existing
        raise ValueError('RenderDoc already has a different lifecycle owner')
    owner = ProcessRef.open(owner_pid, expected_creation_token=owner_creation_token)
    gui = None
    try:
        gui = ProcessRef.open(os.getpid())
        if os.path.basename(gui.executable).lower() != 'qrenderdoc.exe':
            raise ValueError('Parent guardian must run inside qrenderdoc.exe')
        guard = _ParentGuard(owner, gui, close_marker=close_marker)
        # Retain before starting: qrenderdoc's temporary script scope may vanish.
        _parent_guards.append(guard)
        try:
            guard.thread.start()
        except BaseException:
            _parent_guards.remove(guard)
            raise
        return guard
    except BaseException:
        if gui is not None:
            gui.close()
        owner.close()
        raise
