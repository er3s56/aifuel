# -*- coding: utf-8 -*-
"""开机自启：往启动文件夹放/删一个快捷方式。

选启动文件夹而不是注册表 Run 键，是因为它不需要管理员权限、用户在
shell:startup 里看得见也能自己删、且杀软对它最不敏感。

状态不落配置文件，直接由「快捷方式是否存在」决定 —— 否则用户手动删掉
快捷方式后，配置还会一直声称"已启用"，两份真相必然漂移。
"""
from __future__ import annotations

import os
import ctypes as c
from ctypes import wintypes as wt
from contextlib import contextmanager
import sys
import uuid

LINK_NAME = "aifuel.lnk"


def startup_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def link_path() -> str:
    return os.path.join(startup_dir(), LINK_NAME)


def _here() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def target(frontend: str = "qt") -> "tuple[str, str]":
    """返回 (要启动的东西, 工作目录)。

    打包成 exe 时就指向自己；源码运行时指向当前前端的 .vbs 启动器。
    """
    here = _here()
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable), here
    if frontend not in ("qt", "tk"):
        raise ValueError("未知前端: %s" % frontend)
    return os.path.join(here, "run_%s.vbs" % frontend), here


def is_enabled() -> bool:
    return os.path.isfile(link_path())


class _Guid(c.Structure):
    _fields_ = [("bytes", c.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__((c.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))


def _com_method(pointer, index, args, *values):
    table = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
    result = c.WINFUNCTYPE(c.c_long, c.c_void_p, *args)(table[index])(pointer, *values)
    if result < 0:
        raise OSError("Shortcut COM failure: 0x%08x" % (result & 0xffffffff))


@contextmanager
def _shell_link():
    ole = c.WinDLL("ole32")
    ole.CoInitializeEx.argtypes, ole.CoInitializeEx.restype = [c.c_void_p, wt.DWORD], c.c_long
    ole.CoCreateInstance.argtypes = [c.POINTER(_Guid), c.c_void_p, wt.DWORD,
                                    c.POINTER(_Guid), c.POINTER(c.c_void_p)]
    ole.CoCreateInstance.restype = c.c_long
    initialized = ole.CoInitializeEx(None, 2)
    if initialized < 0 and (initialized & 0xffffffff) != 0x80010106:  # RPC_E_CHANGED_MODE
        raise OSError("Cannot initialize shortcut COM")
    shell, persist = c.c_void_p(), c.c_void_p()
    try:
        hr = ole.CoCreateInstance(_Guid("00021401-0000-0000-c000-000000000046"), None, 1,
                                  _Guid("000214f9-0000-0000-c000-000000000046"), c.byref(shell))
        if hr < 0:
            raise OSError("Cannot create Unicode shell link: 0x%08x" % (hr & 0xffffffff))
        _com_method(shell, 0, [c.POINTER(_Guid), c.POINTER(c.c_void_p)],
                    _Guid("0000010b-0000-0000-c000-000000000046"), c.byref(persist))
        yield shell, persist
    finally:
        if persist:
            _com_method(persist, 2, [])
        if shell:
            _com_method(shell, 2, [])
        if initialized >= 0:
            ole.CoUninitialize()


def _make_shortcut(link: str, tgt: str, workdir: str, icon: str = "") -> None:
    """Use IShellLinkW directly: paths stay Unicode data, never shell code.

    ctypes is bundled with Python; source and packaged builds use the same API.
    """
    if tgt.lower().endswith(".vbs"):
        # VBS 用 wscript 拉起（且不弹控制台）；Python 脚本不能交给 wscript。
        target_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                 "System32", "wscript.exe")
        arguments = '"%s"' % tgt
    else:
        target_path, arguments = tgt, ""
    with _shell_link() as (shell, persist):
        _com_method(shell, 20, [wt.LPCWSTR], target_path)  # SetPath
        _com_method(shell, 9, [wt.LPCWSTR], workdir)
        _com_method(shell, 11, [wt.LPCWSTR], arguments)
        _com_method(shell, 15, [c.c_int], 7)  # SW_SHOWMINNOACTIVE
        if icon:
            _com_method(shell, 17, [wt.LPCWSTR, c.c_int], icon, 0)
        _com_method(persist, 6, [wt.LPCWSTR, wt.BOOL], link, True)


def enable(frontend: str = "qt") -> "tuple[bool, str]":
    """开启自启。返回 (成功?, 给用户看的说明)。"""
    if sys.platform != "win32":
        return False, "只支持 Windows"
    tgt, workdir = target(frontend)
    if not os.path.exists(tgt):
        return False, "找不到启动目标：\n%s\n请保留对应的 VBS 启动器，或重新打包 exe。" % tgt
    try:
        os.makedirs(startup_dir(), exist_ok=True)
        icon = os.path.join(_here(), "aifuel.ico")
        _make_shortcut(link_path(), tgt, workdir, icon if os.path.isfile(icon) else "")
    except Exception as e:
        return False, "创建快捷方式失败：%s" % e
    if not is_enabled():
        return False, "快捷方式没能写入启动文件夹"
    return True, "已开启。开机会自动启动：\n%s" % tgt


def disable() -> "tuple[bool, str]":
    try:
        if os.path.isfile(link_path()):
            os.remove(link_path())
    except Exception as e:
        return False, "删除快捷方式失败：%s" % e
    return not is_enabled(), "已关闭开机自启"


def _read_shortcut(link: str) -> tuple[str, str]:
    with _shell_link() as (shell, persist):
        _com_method(persist, 5, [wt.LPCWSTR, wt.DWORD], link, 0)
        path, arguments = c.create_unicode_buffer(32768), c.create_unicode_buffer(32768)
        _com_method(shell, 3, [wt.LPWSTR, c.c_int, c.c_void_p, wt.DWORD],
                    path, len(path), None, 4)
        _com_method(shell, 10, [wt.LPWSTR, c.c_int], arguments, len(arguments))
        return path.value, arguments.value


def migrate_existing() -> tuple[bool, str]:
    """Installer-only migration: preserve disabled startup and unrelated links."""
    if not is_enabled():
        return True, "未启用自启，保持关闭"
    try:
        previous, arguments = _read_shortcut(link_path())
        name = os.path.basename(previous).lower()
        owned = name in ("aifuel.exe", "aifuel_debug.exe")
        if name == "wscript.exe":
            owned = os.path.basename(arguments.strip('"')).lower() in ("run_qt.vbs", "run_tk.vbs")
        if not owned:
            return True, "保留其他程序的快捷方式"
        return enable()
    except Exception as error:
        return False, "迁移自启失败：%s" % error


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    tgt, wd = target()
    print("启动文件夹:", startup_dir())
    print("快捷方式  :", link_path())
    print("启动目标  :", tgt, "(存在:", os.path.exists(tgt), ")")
    print("工作目录  :", wd)
    print("当前状态  :", "已开启" if is_enabled() else "未开启")
    if len(sys.argv) > 1:
        ok, msg = enable() if sys.argv[1] == "on" else disable()
        print("\n%s -> %s\n%s" % (sys.argv[1], "OK" if ok else "失败", msg))
