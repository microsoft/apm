"""Windows half of the verdict key's privacy check (QL D1 Windows ruling, #4977).

The key file must be owned by the current user's SID, must not be a reparse
point, and its DACL (explicit and inherited ACEs) may allow access only to
that SID, LocalSystem and BUILTIN\\Administrators. Any API failure is "not
private". Creation strips inheritance and grants only the current user.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_SE_FILE_OBJECT = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ACCESS_ALLOWED_ACE_TYPE = 0
# ACCESS_ALLOWED (SID-checked), ACCESS_DENIED, SYSTEM_AUDIT, SYSTEM_ALARM, MANDATORY_LABEL.
_ACE_TYPES_KNOWN = frozenset({0, 1, 2, 3, 0x11})
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_ACL_REVISION = 2
_FILE_ALL_ACCESS = 0x001F01FF
_WIN_LOCAL_SYSTEM_SID = 22
_WIN_BUILTIN_ADMINISTRATORS_SID = 26
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _api():  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.POINTER(ctypes.c_void_p)
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        pointer,
        pointer,
        pointer,
        pointer,
        pointer,
    ]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.IsWellKnownSid.argtypes = [ctypes.c_void_p, ctypes.c_int]
    advapi.IsValidSid.argtypes = [ctypes.c_void_p]
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, pointer]
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    return ctypes, wintypes, advapi, kernel


def _current_user_sid(ctypes, wintypes, advapi, kernel):  # pragma: no cover - Windows only
    """(SID pointer, owning buffer) of the process token's user, or (None, None)."""
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        return None, None
    try:
        buffer = ctypes.create_string_buffer(1024)
        size = wintypes.DWORD()
        if not advapi.GetTokenInformation(token, _TOKEN_USER, buffer, 1024, ctypes.byref(size)):
            return None, None
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is the SID pointer.
        return ctypes.c_void_p.from_buffer(buffer).value, buffer
    finally:
        kernel.CloseHandle(token)


def private_to_current_user(path: Path) -> bool:  # pragma: no cover - Windows only
    """The owner is the current user and only user/SYSTEM/Administrators are granted access."""
    if getattr(os.lstat(path), "st_file_attributes", 0) & _REPARSE_POINT:
        return False
    try:
        ctypes, wintypes, advapi, kernel = _api()
    except (OSError, AttributeError):
        return False
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    status = advapi.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if status != 0:
        return False
    try:
        user, _keepalive = _current_user_sid(ctypes, wintypes, advapi, kernel)
        if not user or not owner.value or not advapi.EqualSid(owner, ctypes.c_void_p(user)):
            return False
        return _dacl_private(ctypes, advapi, dacl, user)
    finally:
        kernel.LocalFree(descriptor)


def _dacl_private(ctypes: Any, advapi: Any, dacl: Any, user: Any) -> bool:  # pragma: no cover
    """Every allowing ACE names the user, SYSTEM or Administrators (W3d B-a allowlist)."""
    if not dacl.value:
        return False  # a NULL DACL grants everyone everything
    ace_count = ctypes.c_ushort.from_address(dacl.value + 4).value  # ACL.AceCount
    for index in range(ace_count):
        ace = ctypes.c_void_p()
        if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
            return False
        ace_type = ctypes.c_ubyte.from_address(ace.value).value
        if ace_type not in _ACE_TYPES_KNOWN:
            return False  # callback, compound or object ACEs may grant access: refuse
        if ace_type != _ACCESS_ALLOWED_ACE_TYPE:
            continue  # deny, audit, alarm and label ACEs never widen access
        sid = ctypes.c_void_p(ace.value + 8)  # ACE_HEADER (4) + ACCESS_MASK (4)
        if not advapi.IsValidSid(sid):
            return False
        if not (
            advapi.EqualSid(sid, ctypes.c_void_p(user))
            or advapi.IsWellKnownSid(sid, _WIN_LOCAL_SYSTEM_SID)
            or advapi.IsWellKnownSid(sid, _WIN_BUILTIN_ADMINISTRATORS_SID)
        ):
            return False
    return True


def restrict_to_current_user(path: Path) -> bool:  # pragma: no cover - Windows only
    """Own *path* as the token user with a protected DACL granting only that user.

    Done through the Windows security API (no child process): a fresh ACL with
    one ACCESS_ALLOWED ACE for the token user's SID, set with
    PROTECTED_DACL_SECURITY_INFORMATION so nothing is inherited.
    """
    try:
        ctypes, wintypes, advapi, kernel = _api()
    except (OSError, AttributeError):
        return False
    user, _keepalive = _current_user_sid(ctypes, wintypes, advapi, kernel)
    if not user:
        return False
    advapi.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi.GetLengthSid.restype = wintypes.DWORD
    advapi.InitializeAcl.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    advapi.AddAccessAllowedAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    advapi.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    size = 8 + 8 + advapi.GetLengthSid(ctypes.c_void_p(user)) + 16  # ACL + ACE header/mask + SID
    acl = ctypes.create_string_buffer(size)
    if not advapi.InitializeAcl(acl, size, _ACL_REVISION):
        return False
    if not advapi.AddAccessAllowedAce(acl, _ACL_REVISION, _FILE_ALL_ACCESS, ctypes.c_void_p(user)):
        return False
    status = advapi.SetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION
        | _DACL_SECURITY_INFORMATION
        | _PROTECTED_DACL_SECURITY_INFORMATION,
        ctypes.c_void_p(user),
        None,
        ctypes.cast(acl, ctypes.c_void_p),
        None,
    )
    return status == 0
