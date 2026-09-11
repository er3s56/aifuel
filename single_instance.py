"""Keep launches during shutdown pending until the previous process is gone."""
import ctypes
from ctypes import wintypes as wt
import sys
import time

_mutex = _closing = _reopen = None


def _kernel():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    api.CreateMutexW.restype = wt.HANDLE
    api.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
    api.CreateEventW.restype = wt.HANDLE
    api.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    api.WaitForSingleObject.restype = wt.DWORD
    for name in ("CloseHandle", "SetEvent", "ResetEvent"):
        getattr(api, name).argtypes = [wt.HANDLE]
        getattr(api, name).restype = wt.BOOL
    return api


def acquire(name="aifuel_widget", timeout=90.0):
    global _mutex, _closing, _reopen
    if sys.platform != "win32" or _mutex is not None:
        return True
    api = _kernel()
    mutex = api.CreateMutexW(None, True, "Local\\" + name)
    existed = ctypes.get_last_error() == 183
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    closing = api.CreateEventW(None, True, False, "Local\\" + name + "_closing")
    if not closing:
        error = ctypes.get_last_error()
        api.CloseHandle(mutex)
        raise ctypes.WinError(error)
    acquired = not existed
    reopen = api.CreateEventW(None, False, False, "Local\\" + name + "_reopen")
    if not reopen:
        error = ctypes.get_last_error()
        api.CloseHandle(closing)
        api.CloseHandle(mutex)
        raise ctypes.WinError(error)
    try:
        deadline = time.monotonic() + timeout
        if existed and api.WaitForSingleObject(closing, 0) == 0:
            api.SetEvent(reopen)
        # Normal duplicate launches still exit immediately. During shutdown keep
        # the launch alive, but do not start a second set of quota requests.
        while not acquired and api.WaitForSingleObject(closing, 0) == 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            result = api.WaitForSingleObject(mutex, min(100, max(1, int(remaining * 1000))))
            acquired = result in (0, 0x80)  # owned, or abandoned by exiting process
            if result == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        if acquired:
            api.ResetEvent(closing)
            api.ResetEvent(reopen)
            _mutex, _closing, _reopen = mutex, closing, reopen
        return acquired
    finally:
        if not acquired:
            api.CloseHandle(reopen)
            api.CloseHandle(closing)
            api.CloseHandle(mutex)


def begin_shutdown():
    if _closing is not None:
        api = _kernel()
        if api.WaitForSingleObject(_closing, 0) != 0:
            api.ResetEvent(_reopen)  # discard duplicate activations from an earlier close
        api.SetEvent(_closing)
    # Keep ownership until process exit, including worker/atexit cleanup.


def take_reopen_request():
    return _reopen is not None and _kernel().WaitForSingleObject(_reopen, 0) == 0


def cancel_shutdown():
    if _closing is not None:
        _kernel().ResetEvent(_closing)
