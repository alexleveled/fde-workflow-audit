"""mss wrapper: active-monitor grab, coarse frame hash for near-dupe detection,
and JPEG (Pillow) or PNG (mss fallback) output.

The frame hash is a pure-Python average-hash over a tiny grayscale downsample of
the raw BGRA buffer — no model, no comprehension, just "did the pixels change?".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import mss
import mss.tools

from ..config import ImageCfg
from ..platform import Rect

try:
    from PIL import Image
    _HAVE_PIL = True
except ImportError:  # pragma: no cover - fallback path
    _HAVE_PIL = False


@dataclass
class Grab:
    width: int
    height: int
    raw: bytes          # BGRA pixel buffer from mss
    mss_size: tuple[int, int]
    frame_hash: str


class Screenshotter:
    """Holds one mss instance (must live on the capture thread) and matches the
    active-monitor rect against mss's monitor list."""

    def __init__(self, image_cfg: ImageCfg):
        self.cfg = image_cfg
        self._sct = mss.mss()

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass

    # -- monitor selection -----------------------------------------------
    def _pick_monitor(self, rect: Optional[Rect]) -> dict:
        """Match the foreground monitor rect to an mss monitor. Falls back to
        the all-monitors virtual screen (index 0) only if nothing matches."""
        monitors = self._sct.monitors  # [0]=virtual, [1..]=physical
        if rect is None:
            return monitors[1] if len(monitors) > 1 else monitors[0]
        for mon in monitors[1:]:
            if mon["left"] == rect.left and mon["top"] == rect.top:
                return mon
        # No exact top-left match: pick the monitor whose center contains the
        # rect's top-left (handles minor off-by-one from DPI rounding).
        cx, cy = rect.left, rect.top
        for mon in monitors[1:]:
            if (mon["left"] <= cx < mon["left"] + mon["width"]
                    and mon["top"] <= cy < mon["top"] + mon["height"]):
                return mon
        return monitors[1] if len(monitors) > 1 else monitors[0]

    # -- capture ----------------------------------------------------------
    def grab(self, rect: Optional[Rect]) -> Grab:
        mon = self._pick_monitor(rect)
        shot = self._sct.grab(mon)
        raw = bytes(shot.raw)
        return Grab(
            width=shot.width,
            height=shot.height,
            raw=raw,
            mss_size=(shot.width, shot.height),
            frame_hash=self._average_hash(raw, shot.width, shot.height),
        )

    def save(self, grab: Grab, path: Path) -> None:
        """Write the grab to disk as JPEG (Pillow) or PNG. Caller decides the
        extension via config; this respects it."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.cfg.format == "jpeg" and _HAVE_PIL:
            # mss raw is BGRA; PIL reads it with the 'raw','BGRX' decoder.
            img = Image.frombytes("RGB", (grab.width, grab.height), grab.raw, "raw", "BGRX")
            img.save(path, format="JPEG", quality=self.cfg.jpeg_quality)
        else:
            # PNG via mss.tools (no Pillow needed).
            mss.tools.to_png(grab.raw, (grab.width, grab.height), output=str(path))

    def extension(self) -> str:
        return "jpg" if (self.cfg.format == "jpeg" and _HAVE_PIL) else "png"

    # -- hashing ----------------------------------------------------------
    def _average_hash(self, raw: bytes, width: int, height: int) -> str:
        """Average-hash over an NxN grayscale downsample of the raw BGRA buffer.
        Pure Python, no deps. Returns a hex string; comparison is Hamming
        distance on the underlying bits."""
        n = self.cfg.dupe_hash_size
        if width == 0 or height == 0:
            return "0" * (n * n // 4)
        # Nearest-neighbour sample of n*n points; average luma; bit = above mean.
        samples = []
        stride = 4  # BGRA
        row_bytes = width * stride
        for gy in range(n):
            sy = min(height - 1, (gy * height) // n)
            base = sy * row_bytes
            for gx in range(n):
                sx = min(width - 1, (gx * width) // n)
                off = base + sx * stride
                b = raw[off]
                g = raw[off + 1]
                r = raw[off + 2]
                # integer luma approximation
                samples.append((r * 54 + g * 183 + b * 19) >> 8)
        mean = sum(samples) / len(samples)
        bits = 0
        for i, s in enumerate(samples):
            if s >= mean:
                bits |= (1 << i)
        hexlen = (n * n + 3) // 4
        return format(bits, f"0{hexlen}x")


def hamming_distance(hash_a: Optional[str], hash_b: Optional[str]) -> int:
    """Bit difference between two hex hash strings. Large sentinel if either is
    missing (=> treated as changed)."""
    if not hash_a or not hash_b or len(hash_a) != len(hash_b):
        return 1 << 30
    return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")
