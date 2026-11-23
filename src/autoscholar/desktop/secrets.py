"""Current-user DPAPI, with atomic first-run creation of the profile encryption key."""

import ctypes
import os
import secrets
import sys
from ctypes import wintypes
from pathlib import Path
from tempfile import NamedTemporaryFile


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]


def protect(value: bytes, *, decrypt: bool = False) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI requires Windows")
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), buffer)
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def master_key(folder: Path) -> str:
    path = folder / "key.dpapi"
    if not path.exists():
        with NamedTemporaryFile(dir=folder, delete=False) as temp:
            temp.write(protect(secrets.token_hex(32).encode()))
            temp.flush()
            os.fsync(temp.fileno())
        try:
            os.rename(temp.name, path)
        except FileExistsError:
            Path(temp.name).unlink()
    return protect(path.read_bytes(), decrypt=True).decode()
