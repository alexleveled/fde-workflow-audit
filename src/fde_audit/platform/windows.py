"""Windows platform backend — pure ctypes (no pywin32/psutil; no cp314 wheels).

Provides: DPI awareness, single-instance mutex, foreground window info
(title/exe/app/pid), input-idle milliseconds, and the monitor rect holding the
foreground window. Every function is defensive: Win32 calls that can fail or
stall are wrapped so the watcher's per-tick try can log-and-skip.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import Optional

from . import ForegroundInfo, Rect

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

# -- constants ------------------------------------------------------------
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MONITOR_DEFAULTTONEAREST = 0x00000002
ERROR_ALREADY_EXISTS = 183


# -- structs --------------------------------------------------------------
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", SID_AND_ATTRIBUTES)]


# -- signatures (set argtypes/restype so 64-bit handles aren't truncated) --
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wintypes.BOOL
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
kernel32.LocalFree.restype = wintypes.HLOCAL

advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
advapi32.GetTokenInformation.restype = wintypes.BOOL
advapi32.ConvertSidToStringSidW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)
]
advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL


# -- DPI ------------------------------------------------------------------
def set_dpi_awareness() -> None:
    """PER_MONITOR_AWARE_V2 so GetWindowRect/monitor coords are physical pixels
    on scaled displays. Falls back gracefully on older Windows."""
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user32.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ):
            return
    except (AttributeError, OSError):
        pass
    # Older Windows: try Shcore per-monitor (value 2), then system-DPI aware.
    try:
        shcore = ctypes.WinDLL("shcore")
        shcore.SetProcessDpiAwareness(2)
        return
    except (OSError, AttributeError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (OSError, AttributeError):
        pass


# -- single instance ------------------------------------------------------
def acquire_single_instance(name: str) -> Optional[int]:
    """Named mutex; returns the handle if we own it, None if another instance
    already holds it. A mutex can't go stale after a crash (unlike a lock file)."""
    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def release_single_instance(handle: Optional[int]) -> None:
    if handle:
        try:
            kernel32.CloseHandle(handle)
        except OSError:
            pass


# -- idle -----------------------------------------------------------------
def get_idle_ms() -> int:
    """Milliseconds since the last keyboard/mouse input, system-wide."""
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    tick = kernel32.GetTickCount()
    # GetTickCount wraps every ~49.7 days; mask to 32 bits to stay non-negative.
    return (tick - info.dwTime) & 0xFFFFFFFF


# -- foreground window ----------------------------------------------------
def _window_title(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _process_image_path(pid: int) -> Optional[str]:
    # PROCESS_QUERY_LIMITED_INFORMATION works across the elevation boundary
    # (elevated foreground apps) where PROCESS_QUERY_INFORMATION gets denied.
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def _app_name_from_exe(exe_path: Optional[str]) -> Optional[str]:
    if not exe_path:
        return None
    base = os.path.basename(exe_path)
    return os.path.splitext(base)[0] or base


def get_foreground_info() -> ForegroundInfo:
    """Title + exe path + app name + pid for the foreground window.
    hwnd == 0 (lock screen / UAC secure desktop / mid-switch) yields an empty
    record so the watcher records an idle-like row and takes no screenshot."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ForegroundInfo(hwnd=0, title="", exe_path=None, app_name=None, pid=None)

    title = _window_title(hwnd)
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    pid_val = int(pid.value) or None
    exe_path = _process_image_path(pid_val) if pid_val else None
    return ForegroundInfo(
        hwnd=int(hwnd),
        title=title,
        exe_path=exe_path,
        app_name=_app_name_from_exe(exe_path),
        pid=pid_val,
    )


# -- active monitor -------------------------------------------------------
def get_monitor_rect_for_foreground() -> Optional[Rect]:
    """Rect (physical px) of the monitor holding the foreground window, so the
    screenshot grabs the right display. mss alone has no 'active monitor'."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return None
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        return None
    r = info.rcMonitor
    return Rect(left=r.left, top=r.top, right=r.right, bottom=r.bottom)


# -- host metadata --------------------------------------------------------
def hostname() -> str:
    return os.environ.get("COMPUTERNAME") or _safe_hostname()


def _safe_hostname() -> str:
    import socket
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def os_name() -> str:
    import platform as _p
    return f"{_p.system()} {_p.release()}"


def tz_name() -> str:
    """IANA tz name if resolvable, else the platform's local tz key."""
    try:
        from datetime import datetime
        tz = datetime.now().astimezone().tzinfo
        key = getattr(tz, "key", None)  # zoneinfo exposes .key
        if key:
            return key
        return time.tzname[0] if time.tzname else "UTC"
    except Exception:
        return "UTC"


# -- identity (for fleet auto-enrollment) ---------------------------------
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1  # TOKEN_INFORMATION_CLASS.TokenUser


def user_sid() -> Optional[str]:
    """The logged-in Windows account's SID (e.g. 'S-1-5-21-...-1013'), read from
    this process's own access token — no network call, no credentials, since the
    process already runs as that user. This is the stable identity key: Active
    Directory assigns it at account creation and it survives renames/reinstalls.
    Returns None off a token or on any failure (caller falls back)."""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        return None
    try:
        size = wintypes.DWORD(0)
        # First call sizes the buffer (expected to "fail" with ERROR_INSUFFICIENT_BUFFER).
        advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
        if size.value == 0:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, TOKEN_USER_CLASS, buf, size, ctypes.byref(size)
        ):
            return None
        tu = ctypes.cast(buf, ctypes.POINTER(TOKEN_USER)).contents
        str_ptr = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(tu.User.Sid, ctypes.byref(str_ptr)):
            return None
        try:
            return str_ptr.value
        finally:
            kernel32.LocalFree(str_ptr)
    finally:
        kernel32.CloseHandle(token)


def user_login() -> Optional[str]:
    """'DOMAIN\\username' for the logged-in user — human-facing display name and
    the identity fallback when the SID can't be read."""
    domain = os.environ.get("USERDOMAIN") or os.environ.get("USERDNSDOMAIN")
    user = os.environ.get("USERNAME")
    if not user:
        return None
    return f"{domain}\\{user}" if domain else user


def ad_department(sid: Optional[str] = None) -> Optional[str]:
    """Best-effort Active Directory lookup of the current user's `department`
    attribute, via the built-in ADSI searcher (a PowerShell one-liner — no RSAT,
    no pip deps, works on any domain-joined box). Queries by SID when available,
    else by username. Returns None off-domain, on timeout, or when the attribute
    is empty — so the caller silently falls back to the package config default."""
    import subprocess

    filters = []
    if sid:
        # AD LDAP filters accept the string-SID form for objectSid.
        filters.append(f"(objectSid={sid})")
    user = os.environ.get("USERNAME")
    if user:
        filters.append(f"(sAMAccountName={user})")
    if not filters:
        return None

    for flt in filters:
        ps = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"$s=[adsisearcher]'{flt}';"
            "$s.PropertiesToLoad.Add('department') > $null;"
            "$r=$s.FindOne();"
            "if($r){ $r.Properties['department'] }"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        dept = (out.stdout or "").strip()
        if dept:
            # A multi-valued attribute would print multiple lines; take the first.
            return dept.splitlines()[0].strip() or None
    return None
