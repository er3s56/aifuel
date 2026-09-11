# -*- coding: utf-8 -*-
"""只读查询任务栏实际按钮边界；使用系统 COM，不依赖 pywin32。"""
import ctypes as c
from ctypes import wintypes as w
import uuid


class _Guid(c.Structure):
    _fields_ = [("bytes", c.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__((c.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))


class _Value(c.Union):
    _fields_ = [("pointer", c.c_void_p), ("record", c.c_void_p * 2),
                ("number", c.c_double), ("integer", c.c_long)]


class _Variant(c.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("vt", w.WORD), ("reserved", w.WORD * 3), ("value", _Value)]


def _method(pointer, index, args, *values):
    table = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
    function = c.WINFUNCTYPE(c.c_long, c.c_void_p, *args)(table[index])
    result = function(pointer, *values)
    if result < 0:
        raise OSError("Windows UI Automation query failed: 0x%08x" % (result & 0xffffffff))


def _button_rects(elements):
    count = c.c_int()
    _method(elements, 3, [c.POINTER(c.c_int)], c.byref(count))
    result = []
    for index in range(count.value):
        button = c.c_void_p()
        _method(elements, 4, [c.c_int, c.POINTER(c.c_void_p)], index, c.byref(button))
        try:
            offscreen = w.BOOL()
            _method(button, 38, [c.POINTER(w.BOOL)], c.byref(offscreen))
            if offscreen.value:
                continue
            rect = w.RECT()
            _method(button, 43, [c.POINTER(w.RECT)], c.byref(rect))
            if rect.right > rect.left and rect.bottom > rect.top:
                result.append((rect.left, rect.top, rect.right, rect.bottom))
        finally:
            if button:
                _method(button, 2, [])
    return tuple(result)


def taskbar_button_rects(hwnd):
    """在线程中调用，返回所有可见按钮的物理像素边界；失败抛出 OSError。

    应用、开始、搜索、溢出和天气入口统一避让，不依赖 Windows 的内部类名。
    不能用 ReBar/MSTaskSw 容器推断空白：Windows 11 按钮会伸出其边界。
    """
    ole = c.WinDLL("ole32")
    ole.CoInitializeEx.argtypes, ole.CoInitializeEx.restype = [c.c_void_p, w.DWORD], c.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoCreateInstance.argtypes = [c.POINTER(_Guid), c.c_void_p, w.DWORD,
                                    c.POINTER(_Guid), c.POINTER(c.c_void_p)]
    ole.CoCreateInstance.restype = c.c_long
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
        value.vt = 3  # VT_I4; UIA_ControlTypePropertyId = 30003
        value.integer = 50000  # UIA_ButtonControlTypeId
        condition = c.c_void_p()
        _method(client, 23, [c.c_int, _Variant, c.POINTER(c.c_void_p)],
                30003, value, c.byref(condition))
        pointers.append(condition)
        found = c.c_void_p()
        _method(element, 6, [c.c_int, c.c_void_p, c.POINTER(c.c_void_p)],
                4, condition, c.byref(found))  # TreeScope_Descendants
        pointers.append(found)
        if not found:
            raise OSError("Taskbar buttons unavailable")
        return _button_rects(found)
    finally:
        for pointer in reversed(pointers):
            if pointer:
                _method(pointer, 2, [])
        if previous:
            user.SetThreadDpiAwarenessContext(previous)
        ole.CoUninitialize()
