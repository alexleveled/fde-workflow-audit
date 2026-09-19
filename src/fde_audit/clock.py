"""Time helpers: UTC epoch-millis, local-day, tz offset. One place so the whole
pipeline agrees on how timestamps are formed."""

from __future__ import annotations

import time
from datetime import datetime


def now_ms() -> int:
    """Current UTC time as epoch milliseconds."""
    return int(time.time() * 1000)


def local_day(ts_ms: int) -> str:
    """YYYY-MM-DD in the machine's local timezone for a UTC epoch-millis value."""
    return datetime.fromtimestamp(ts_ms / 1000).astimezone().strftime("%Y-%m-%d")


def tz_offset_min(ts_ms: int) -> int:
    """Local UTC offset in minutes for the given instant (handles DST)."""
    off = datetime.fromtimestamp(ts_ms / 1000).astimezone().utcoffset()
    return int(off.total_seconds() // 60) if off else 0
