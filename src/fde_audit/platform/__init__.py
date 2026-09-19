"""OS-agnostic platform interface for the capture pipeline.

Every OS backend exposes the same surface so the watcher never imports a
platform module directly. Windows is implemented now; macos.py comes with the
org rollout behind this same interface.

Interface:
  set_dpi_awareness() -> None
  acquire_single_instance(name) -> handle | None   (None => another instance holds it)
  release_single_instance(handle) -> None
  get_foreground_info() -> ForegroundInfo
  get_idle_ms() -> int
  get_monitor_rect_for_foreground() -> Rect | None
  hostname() -> str
  os_name() -> str
  tz_name() -> str
  user_sid() -> str | None            (stable OS identity for fleet auto-enroll)
  user_login() -> str | None          (DOMAIN\\user; identity/display fallback)
  ad_department(sid=None) -> str | None  (AD `department` attr; None => fall back)
"""

from __future__ import annotations

import platform as _platform
import sys
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ForegroundInfo:
    hwnd: int                       # 0 => no foreground window (lock screen, secure desktop)
    title: str
    exe_path: Optional[str]
    app_name: Optional[str]         # derived from exe (e.g. "Code" from Code.exe)
    pid: Optional[int]


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def get_platform():
    """Return the platform backend module for the current OS."""
    if sys.platform == "win32":
        from . import windows
        return windows
    raise NotImplementedError(
        f"capture backend not implemented for platform {sys.platform!r}; "
        "Windows only in Phase 1 (macOS comes with the org rollout)"
    )


def os_name() -> str:
    return f"{_platform.system()} {_platform.release()}"
