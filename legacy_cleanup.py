"""Retire only the private Windhawk runtime shipped by older AI Fuel versions."""
import ctypes as c
from ctypes import wintypes as wt
import os
from pathlib import Path
import re
import subprocess
import sys


def _running_executable():
    user = c.WinDLL("user32", use_last_error=True)
    user.FindWindowW.argtypes, user.FindWindowW.restype = [wt.LPCWSTR, wt.LPCWSTR], wt.HWND
    user.GetWindowThreadProcessId.argtypes = [wt.HWND, c.POINTER(wt.DWORD)]
    hwnd = user.FindWindowW("WindhawkDaemon", None)
    if not hwnd:
        return None
    pid = wt.DWORD()
    user.GetWindowThreadProcessId(hwnd, c.byref(pid))
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, c.POINTER(wt.DWORD)]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    process = kernel.OpenProcess(0x1000, False, pid.value)
    if not process:
        return None
    try:
        path, size = c.create_unicode_buffer(32768), wt.DWORD(32768)
        if kernel.QueryFullProcessImageNameW(process, 0, path, c.byref(size)):
            return Path(path.value)
    finally:
        kernel.CloseHandle(process)


def stop_private_runtime():
    """No downloads or launches in normal use; a bounded exit command on upgrade."""
    if sys.platform != "win32":
        return
    try:
        running = _running_executable()
        if running is None:
            return
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        root = (base / "aifuel/taskbar-runtime").resolve()
        relative = running.resolve().relative_to(root)
        if (len(relative.parts) != 2 or not re.fullmatch(r"[0-9a-f]{16}", relative.parts[0])
                or relative.name.lower() != "windhawk.exe"):
            return
        subprocess.run([str(running), "-exit", "-wait"], timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # Cleanup must never prevent the floating widget from starting.
