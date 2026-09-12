"""Read Windows-protected data using DPAPI and authenticated AES-GCM in CNG."""
import ctypes as c
from ctypes import wintypes as w


class _Blob(c.Structure):
    _fields_ = [("size", w.DWORD), ("data", c.c_void_p)]


class _AuthInfo(c.Structure):
    _fields_ = [
        ("size", w.ULONG), ("version", w.ULONG),
        ("nonce", c.c_void_p), ("nonce_size", w.ULONG),
        ("aad", c.c_void_p), ("aad_size", w.ULONG),
        ("tag", c.c_void_p), ("tag_size", w.ULONG),
        ("mac", c.c_void_p), ("mac_size", w.ULONG),
        ("aad_total", w.ULONG), ("data_total", c.c_ulonglong), ("flags", w.ULONG),
    ]


def unprotect(data):
    crypt = c.WinDLL("crypt32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    crypt.CryptUnprotectData.argtypes = [c.POINTER(_Blob), c.c_void_p, c.c_void_p,
                                        c.c_void_p, c.c_void_p, w.DWORD, c.POINTER(_Blob)]
    crypt.CryptUnprotectData.restype = w.BOOL
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    buffer = c.create_string_buffer(data)
    source = _Blob(len(data), c.addressof(buffer))
    result = _Blob()
    # CRYPTPROTECT_UI_FORBIDDEN: background reads must never open a system dialog.
    if not crypt.CryptUnprotectData(c.byref(source), None, None, None, None, 1, c.byref(result)):
        raise OSError("Windows credential decryption failed")
    try:
        return c.string_at(result.data, result.size)
    finally:
        c.memset(result.data, 0, result.size)
        kernel.LocalFree(result.data)


def decrypt_gcm(key, nonce, ciphertext, tag):
    if len(key) != 32 or len(nonce) != 12 or len(tag) != 16:
        raise ValueError("Invalid AES-GCM parameters")
    api = c.WinDLL("bcrypt")
    ptr, num = c.c_void_p, w.ULONG
    signatures = {
        "BCryptOpenAlgorithmProvider": [c.POINTER(ptr), w.LPCWSTR, w.LPCWSTR, num],
        "BCryptSetProperty": [ptr, w.LPCWSTR, ptr, num, num],
        "BCryptGenerateSymmetricKey": [ptr, c.POINTER(ptr), ptr, num, ptr, num, num],
        "BCryptDecrypt": [ptr, ptr, num, ptr, ptr, num, ptr, num, c.POINTER(num), num],
        "BCryptDestroyKey": [ptr],
        "BCryptCloseAlgorithmProvider": [ptr, num],
    }
    for name, args in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, w.LONG

    def check(status):
        if status < 0:
            raise OSError("Windows authenticated decryption failed")

    algorithm, handle = ptr(), ptr()
    secret = c.create_string_buffer(key)
    iv = c.create_string_buffer(nonce)
    auth_tag = c.create_string_buffer(tag)
    encrypted = c.create_string_buffer(ciphertext)
    plain = c.create_string_buffer(max(1, len(ciphertext)))
    info = _AuthInfo(size=c.sizeof(_AuthInfo), version=1, nonce=c.addressof(iv),
                     nonce_size=len(nonce), tag=c.addressof(auth_tag), tag_size=len(tag))
    written = num()
    try:
        check(api.BCryptOpenAlgorithmProvider(c.byref(algorithm), "AES", None, 0))
        mode = c.create_unicode_buffer("ChainingModeGCM")
        check(api.BCryptSetProperty(algorithm, "ChainingMode", mode, c.sizeof(mode), 0))
        # CNG allocates and owns the key object when its buffer is NULL (Windows 7+).
        check(api.BCryptGenerateSymmetricKey(algorithm, c.byref(handle), None, 0, secret, len(key), 0))
        check(api.BCryptDecrypt(handle, encrypted, len(ciphertext), c.byref(info), None, 0,
                               plain, len(plain), c.byref(written), 0))
        return plain.raw[:written.value]
    finally:
        if handle:
            api.BCryptDestroyKey(handle)
        if algorithm:
            api.BCryptCloseAlgorithmProvider(algorithm, 0)
        c.memset(secret, 0, c.sizeof(secret))
        c.memset(plain, 0, c.sizeof(plain))
