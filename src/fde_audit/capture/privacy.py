"""Privacy gate: app/title denylist and the pause file.

A denylist hit or an active pause file suppresses the screenshot; the metadata
row is still written (so time-in-app accounting stays complete) but with no
image and no pixel hash retained.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import PrivacyCfg


@dataclass(frozen=True)
class PrivacyDecision:
    allow_screenshot: bool
    reason: Optional[str]  # None when allowed; e.g. "denylist:app" / "paused"


class PrivacyGate:
    def __init__(self, cfg: PrivacyCfg, pause_file: Path):
        self.cfg = cfg
        self.pause_file = pause_file

    def is_paused(self) -> bool:
        return self.pause_file.exists()

    def evaluate(self, app_name: Optional[str], title: Optional[str]) -> PrivacyDecision:
        if self.is_paused():
            return PrivacyDecision(False, "paused")
        app_l = (app_name or "").lower()
        title_l = (title or "").lower()
        for needle in self.cfg.app_denylist:
            if needle and needle in app_l:
                return PrivacyDecision(False, f"denylist:app:{needle}")
        for needle in self.cfg.title_denylist:
            if needle and needle in title_l:
                return PrivacyDecision(False, f"denylist:title:{needle}")
        return PrivacyDecision(True, None)
