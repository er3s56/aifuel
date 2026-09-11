# -*- coding: utf-8 -*-
"""Fixed taskbar space, with a two-second lease owned by the quota window."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

WIDTH = "aifuel.TaskbarWidth.v1"
LEASE = "aifuel.TaskbarLease.v1"
READY = "aifuel.TaskbarReady.v1"


def runtime_bundle():
    root = Path(__file__).resolve().parent
    bundled = root / "taskbar_runtime"
    return bundled if bundled.is_dir() else root / "build/taskbar-runtime"


class ReservationClient:
    def __init__(self, api):
        self.api = api
        for name, args, result in (
            ("SetPropW", [wt.HWND, wt.LPCWSTR, wt.HANDLE], wt.BOOL),
            ("RemovePropW", [wt.HWND, wt.LPCWSTR], wt.HANDLE),
            ("GetPropW", [wt.HWND, wt.LPCWSTR], wt.HANDLE),
        ):
            function = getattr(api, name)
            function.argtypes, function.restype = args, result
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetTickCount64.restype = ctypes.c_ulonglong
        self.kernel.OpenProcess.argtypes, self.kernel.OpenProcess.restype = [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
        self.kernel.CloseHandle.argtypes = [wt.HANDLE]
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        self.directory = base / "aifuel/taskbar-runtime"
        self.hwnd = 0
        self.logical_width = 0
        self.worker = None
        self.closed = False
        self.error = ""
        self.next_start = 0.0

    def _running_executable(self):
        daemon = self.api.FindWindowW("WindhawkDaemon", None)
        if not daemon:
            return None
        process_id = wt.DWORD()
        self.api.GetWindowThreadProcessId(daemon, ctypes.byref(process_id))
        process = self.kernel.OpenProcess(0x1000, False, process_id.value)
        if not process:
            raise OSError("无法确认正在运行的任务栏扩展")
        try:
            path, size = ctypes.create_unicode_buffer(32768), wt.DWORD(32768)
            if not self.kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                raise OSError("无法确认正在运行的任务栏扩展")
            return Path(path.value)
        finally:
            self.kernel.CloseHandle(process)

    def _stop(self):
        # Only stop our private runtime; another Windhawk installation is user-owned.
        running = self._running_executable()
        if running and running.resolve().is_relative_to(self.directory.resolve()):
            subprocess.run([str(running), "-exit", "-wait"], timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW, check=True)

    def _reuse_symbols(self, target):
        # PDB paths include the module GUID/age. Windhawk validates that identity;
        # reuse downloaded files across upgrades, without copying old mod state.
        for source in self.directory.glob("*/AppData/Engine/Symbols"):
            if source == target / "AppData/Engine/Symbols":
                continue
            for pdb in source.rglob("*.pdb"):
                destination = target / "AppData/Engine/Symbols" / pdb.relative_to(source)
                try:
                    if pdb.is_file() and pdb.stat().st_size and not destination.exists():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(pdb, destination)
                except OSError:
                    pass  # Optional cache; Windhawk can still download the PDB.

    def _start(self):
        try:
            bundle = runtime_bundle()
            identity = (bundle / "runtime-id.txt").read_text(encoding="ascii").strip()
            if len(identity) != 16 or any(c not in "0123456789abcdef" for c in identity):
                raise OSError("无效的任务栏扩展版本")
            target = self.directory / identity
            executable = target / "windhawk.exe"
            if not (target / ".ready").is_file():
                shutil.copytree(bundle, target, dirs_exist_ok=True)
                self._reuse_symbols(target)
                (target / ".ready").touch()
            if self.closed:
                return
            running = self._running_executable()
            if running and running.resolve() != executable.resolve():
                if not running.resolve().is_relative_to(self.directory.resolve()):
                    raise OSError("已有其他 Windhawk 正在运行，请在其中安装 aifuel 占位扩展")
                self._stop()
            if not running or running.resolve() != executable.resolve():
                subprocess.Popen([str(executable), "-tray-only"], cwd=target,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            if self.closed:
                self._stop()
            self.error = ""
        except Exception as error:
            self.error = str(error)

    def request(self, hwnd, logical_width):
        if self.closed:
            return
        if self.hwnd != hwnd:
            self.release()
            self.hwnd = hwnd
        self.logical_width = logical_width
        # Match arrange()'s six-DIP gaps on both sides of the quota panel.
        if not self.api.SetPropW(hwnd, WIDTH, logical_width + 12):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.api.SetPropW(hwnd, LEASE, self.kernel.GetTickCount64()):
            self.release()
            raise ctypes.WinError(ctypes.get_last_error())
        now = time.monotonic()
        if now >= self.next_start and not (self.worker and self.worker.is_alive()):
            self.next_start = now + 10
            self.worker = threading.Thread(target=self._start, daemon=True)
            self.worker.start()

    @property
    def ready(self):
        return bool(self.hwnd and self.api.GetPropW(self.hwnd, READY) == self.logical_width + 12)

    def release(self):
        if self.hwnd:
            self.api.RemovePropW(self.hwnd, WIDTH)
            self.api.RemovePropW(self.hwnd, LEASE)
            self.hwnd = 0

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.release()
        try:
            self._stop()
        except (OSError, subprocess.SubprocessError):
            pass  # The expired lease still restores the original taskbar layout.
