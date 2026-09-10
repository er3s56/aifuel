# -*- coding: utf-8 -*-
"""只读查询 Windows 11 天气按钮边界；使用系统 COM，不依赖 pywin32。"""
import ctypes as c
from ctypes import wintypes as w
import uuid


class _Guid(c.Structure):
    _fields_ = [("bytes", c.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__((c.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))


class _Value(c.Union):
    _fields_ = [("pointer", c.c_void_p), ("record", c.c_void_p * 2), ("number", c.c_double)]


class _Variant(c.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("vt", w.WORD), ("reserved", w.WORD * 3), ("value", _Value)]


def _method(pointer, index, args, *values):
    table = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
    function = c.WINFUNCTYPE(c.c_long, c.c_void_p, *args)(table[index])
    result = function(pointer, *values)
    if result < 0:
        raise OSError("Windows UI Automation query failed: 0x%08x" % (result & 0xffffffff))


def widget_rect(hwnd):
    """在线程中调用，返回物理像素边界或 None（无天气按钮）；失败抛出 OSError。"""
    ole = c.WinDLL("ole32")
    auto = c.WinDLL("oleaut32")
    ole.CoInitializeEx.argtypes, ole.CoInitializeEx.restype = [c.c_void_p, w.DWORD], c.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoCreateInstance.argtypes = [c.POINTER(_Guid), c.c_void_p, w.DWORD,
                                    c.POINTER(_Guid), c.POINTER(c.c_void_p)]
    ole.CoCreateInstance.restype = c.c_long
    auto.SysAllocString.argtypes, auto.SysAllocString.restype = [w.LPCWSTR], c.c_void_p
    auto.VariantClear.argtypes = [c.POINTER(_Variant)]
    if ole.CoInitializeEx(None, 0) < 0:
        raise OSError("Windows UI Automation initialization failed")
    pointers = []
    value = _Variant()
    user = c.WinDLL("user32")
    user.SetThreadDpiAwarenessContext.argtypes = [c.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = c.c_void_p
    previous = user.SetThreadDpiAwarenessContext(c.c_void_p(-4))
    try:
        client = c.c_void_p()
        clsid = _Guid("ff48dba4-60ef-4201-aa87-54103eef594e")
        iid = _Guid("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee")
        if ole.CoCreateInstance(c.byref(clsid), None, 1, c.byref(iid), c.byref(client)) < 0:
            raise OSError("Windows UI Automation unavailable")
        pointers.append(client)
        element = c.c_void_p()
        _method(client, 6, [w.HWND, c.POINTER(c.c_void_p)], hwnd, c.byref(element))
        pointers.append(element)
        value.vt = 8  # VT_BSTR; UIA_AutomationIdPropertyId = 30011
        value.pointer = auto.SysAllocString("WidgetsButton")
        if not value.pointer:
            raise OSError("Cannot allocate UI Automation condition")
        condition = c.c_void_p()
        _method(client, 23, [c.c_int, _Variant, c.POINTER(c.c_void_p)],
                30011, value, c.byref(condition))
        pointers.append(condition)
        found = c.c_void_p()
        _method(element, 5, [c.c_int, c.c_void_p, c.POINTER(c.c_void_p)],
                4, condition, c.byref(found))  # TreeScope_Descendants
        if not found:
            return None
        pointers.append(found)
        offscreen = w.BOOL()
        _method(found, 38, [c.POINTER(w.BOOL)], c.byref(offscreen))
        if offscreen.value:
            return None
        rect = w.RECT()
        _method(found, 43, [c.POINTER(w.RECT)], c.byref(rect))
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)
    finally:
        auto.VariantClear(c.byref(value))
        for pointer in reversed(pointers):
            if pointer:
                _method(pointer, 2, [])
        if previous:
            user.SetThreadDpiAwarenessContext(previous)
        ole.CoUninitialize()
