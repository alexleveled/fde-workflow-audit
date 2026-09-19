"""The capture loop. Model-free: each tick writes a frames row (and maybe an
image) and nothing else. Never crashes on a per-tick error; clean Ctrl+C exit.

Two streams per tick, never conflated:
  * metadata row  — written every tick for active/passive (work) time; the sole
    source of time-in-app. Idle is collapsed (onset + <=1 heartbeat/min).
  * image / vision look — the only thing dedupe trims.

Three activity states (active | passive | idle) derived from two free signals:
input idle (GetLastInputInfo) and pixel change (frame_hash).
"""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path
from typing import Optional

from .. import clock
from ..config import Config
from ..db.store import Store, new_id
from ..platform import ForegroundInfo, get_platform
from .privacy import PrivacyGate
from .screenshot import Screenshotter, hamming_distance

log = logging.getLogger("digest.watcher")

MUTEX_NAME = "Global\\ImprovementDigestCapture"

ACTIVE = "active"
PASSIVE = "passive"
IDLE = "idle"


class Watcher:
    def __init__(self, cfg: Config, store: Store, person_id: str, device_id: str):
        self.cfg = cfg
        self.store = store
        self.person_id = person_id
        self.device_id = device_id
        self.plat = get_platform()
        self.shooter = Screenshotter(cfg.image)
        self.privacy = PrivacyGate(cfg.privacy, cfg.pause_file)

        # per-loop state
        self._prev_hash: Optional[str] = None
        self._prev_app: Optional[str] = None
        self._prev_title: Optional[str] = None
        self._prev_image_frame_id: Optional[str] = None
        self._prev_image_path: Optional[str] = None
        self._last_image_ts: float = 0.0     # monotonic-ish (uses tick wall ms)
        self._last_state: Optional[str] = None
        self._idle_last_row_ms: int = 0
        self._running = False
        self._mutex_handle = None

        # thresholds in ms
        self._idle_threshold_ms = int(cfg.capture.idle_threshold_seconds * 1000)
        self._passive_grace_ms = int(cfg.capture.passive_grace_seconds * 1000)
        self._heartbeat_ms = int(cfg.capture.idle_heartbeat_seconds * 1000)
        self._interval = cfg.capture.interval_seconds

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self.plat.set_dpi_awareness()
        self._mutex_handle = self.plat.acquire_single_instance(MUTEX_NAME)
        if self._mutex_handle is None:
            raise RuntimeError(
                "another capture instance is already running (single-instance mutex held)"
            )
        self._running = True
        signal.signal(signal.SIGINT, self._on_signal)
        try:
            signal.signal(signal.SIGTERM, self._on_signal)
        except (ValueError, AttributeError):
            pass
        log.info("capture started (interval=%ss, format=%s)",
                 self._interval, self.shooter.extension())
        self._loop()

    def _on_signal(self, *_a) -> None:
        log.info("shutdown signal received")
        self._running = False

    def stop(self) -> None:
        self._running = False

    def _cleanup(self) -> None:
        self.shooter.close()
        self.plat.release_single_instance(self._mutex_handle)
        self._mutex_handle = None
        log.info("capture stopped cleanly")

    # -- loop -------------------------------------------------------------
    def _loop(self) -> None:
        try:
            while self._running:
                start = time.time()
                try:
                    self.tick()
                except Exception:  # never let a per-tick error kill the loop
                    log.exception("tick failed; skipping")
                # sleep the remainder of the interval, staying responsive to stop
                elapsed = time.time() - start
                remaining = self._interval - elapsed
                while remaining > 0 and self._running:
                    time.sleep(min(0.25, remaining))
                    remaining -= 0.25
        finally:
            self._cleanup()

    # -- one tick ---------------------------------------------------------
    def tick(self) -> None:
        now = clock.now_ms()
        idle_ms = self.plat.get_idle_ms()
        fg: ForegroundInfo = self.plat.get_foreground_info()

        # 1) No foreground window (lock screen / UAC secure desktop / mid-switch)
        if fg.hwnd == 0:
            self._handle_idle_like(now, idle_ms, app=None, exe=None, title="",
                                   reason="no-foreground")
            return

        app = fg.app_name
        title = fg.title

        # 2) Privacy gate — metadata-only row, no image, no pixel hash retained.
        decision = self.privacy.evaluate(app, title)

        # 3) Classify state from input idle (pixel refinement happens after grab).
        state = self._classify_by_input(idle_ms, foreground_present=True)

        # Hard idle => collapse (onset + <=1 heartbeat/min), no screenshot, no grab.
        if state == IDLE:
            self._handle_idle_like(now, idle_ms, app=app, exe=fg.exe_path,
                                   title=title, reason="input-idle")
            return

        # active or passive => we intend to grab (unless privacy blocks it).
        if not decision.allow_screenshot:
            self._write_frame(
                now=now, idle_ms=idle_ms, state=state, app=app, exe=fg.exe_path,
                title=title, is_heartbeat=False, screenshot_path=None,
                dup_of=None, frame_hash=None, screen_w=None, screen_h=None,
            )
            self._remember(state, None, app, title, None, None)
            return

        # 4) Active-monitor resolution + 5) grab + hash
        rect = self.plat.get_monitor_rect_for_foreground()
        grab = self.shooter.grab(rect)
        cur_hash = grab.frame_hash

        changed = self._pixels_changed(cur_hash) or (app != self._prev_app) \
            or (title != self._prev_title)

        # Passive but pixels moved a lot => really active (video/render/scroll).
        if state == PASSIVE and changed:
            state = ACTIVE

        # 6) Decide image vs dup-link, with a forced heartbeat on unchanged screens.
        heartbeat_due = (now - self._last_image_ts) >= self._heartbeat_ms
        is_heartbeat = False
        screenshot_path: Optional[str] = None
        dup_of: Optional[str] = None

        if changed or heartbeat_due or self._prev_image_frame_id is None:
            # write a real image
            frame_id = new_id()
            screenshot_path = self._save_image(grab, now, frame_id)
            is_heartbeat = (not changed) and heartbeat_due
            self._last_image_ts = now
            self._write_frame(
                now=now, idle_ms=idle_ms, state=state, app=app, exe=fg.exe_path,
                title=title, is_heartbeat=is_heartbeat, screenshot_path=screenshot_path,
                dup_of=None, frame_hash=cur_hash,
                screen_w=grab.width, screen_h=grab.height, frame_id=frame_id,
            )
            self._prev_image_frame_id = frame_id
            self._prev_image_path = screenshot_path
        else:
            # near-duplicate: metadata row, no new image, dup_of -> last image frame
            dup_of = self._prev_image_frame_id
            self._write_frame(
                now=now, idle_ms=idle_ms, state=state, app=app, exe=fg.exe_path,
                title=title, is_heartbeat=False, screenshot_path=None,
                dup_of=dup_of, frame_hash=cur_hash,
                screen_w=grab.width, screen_h=grab.height,
            )

        self._remember(state, cur_hash, app, title,
                       self._prev_image_frame_id, self._prev_image_path)

    # -- helpers ----------------------------------------------------------
    def _classify_by_input(self, idle_ms: int, foreground_present: bool) -> str:
        """Input-only first pass; pixel change may promote passive->active later."""
        if idle_ms <= self._interval * 1000:
            return ACTIVE  # input within the last tick
        if idle_ms <= self._idle_threshold_ms:
            return PASSIVE  # no recent input but under the idle threshold
        # Over the idle threshold: reading grace keeps a present foreground as
        # passive (don't silently lose reading time) up to passive_grace.
        if (self.cfg.capture.passive_reading and foreground_present
                and idle_ms <= self._passive_grace_ms):
            return PASSIVE
        return IDLE

    def _pixels_changed(self, cur_hash: Optional[str]) -> bool:
        if self._prev_hash is None or cur_hash is None:
            return True
        return hamming_distance(self._prev_hash, cur_hash) > self.cfg.image.dupe_hamming_threshold

    def _handle_idle_like(self, now: int, idle_ms: int, app, exe, title, reason: str) -> None:
        """Collapse idle: one onset row on entering idle, then <=1 heartbeat/min
        (updating idle_ms). No screenshot. Skips the row entirely between beats."""
        entering = self._last_state != IDLE
        if entering:
            self._write_frame(
                now=now, idle_ms=idle_ms, state=IDLE, app=app, exe=exe, title=title,
                is_heartbeat=False, screenshot_path=None, dup_of=None,
                frame_hash=None, screen_w=None, screen_h=None,
            )
            self._idle_last_row_ms = now
        elif (now - self._idle_last_row_ms) >= self._heartbeat_ms:
            self._write_frame(
                now=now, idle_ms=idle_ms, state=IDLE, app=app, exe=exe, title=title,
                is_heartbeat=True, screenshot_path=None, dup_of=None,
                frame_hash=None, screen_w=None, screen_h=None,
            )
            self._idle_last_row_ms = now
        # else: within the heartbeat window -> write nothing (collapse)
        self._remember(IDLE, None, app, title,
                       self._prev_image_frame_id, self._prev_image_path)

    def _remember(self, state, cur_hash, app, title, image_frame_id, image_path) -> None:
        self._last_state = state
        if cur_hash is not None:
            self._prev_hash = cur_hash
        self._prev_app = app
        self._prev_title = title
        self._prev_image_frame_id = image_frame_id
        self._prev_image_path = image_path

    def _save_image(self, grab, now: int, frame_id: str) -> str:
        day = clock.local_day(now)
        ms = now % 1000
        stamp = time.strftime("%H%M%S", time.localtime(now / 1000))
        ext = self.shooter.extension()
        rel = Path(day) / f"{stamp}_{ms:03d}.{ext}"
        abs_path = self.cfg.screenshots_dir / rel
        self.shooter.save(grab, abs_path)
        # store path relative to data_dir for portability
        try:
            return str(abs_path.relative_to(self.cfg.data_dir))
        except ValueError:
            return str(abs_path)

    def _write_frame(self, *, now: int, idle_ms: int, state: str, app, exe, title,
                     is_heartbeat: bool, screenshot_path, dup_of, frame_hash,
                     screen_w, screen_h, frame_id: Optional[str] = None) -> str:
        row = {
            "id": frame_id or new_id(),
            "person_id": self.person_id,
            "device_id": self.device_id,
            "ts_utc": now,
            "local_day": clock.local_day(now),
            "tz_offset_min": clock.tz_offset_min(now),
            "app_name": app,
            "exe_path": exe,
            "window_title": title,
            "url_domain": None,
            "idle_ms": int(idle_ms),
            "activity_state": state,
            "is_heartbeat": 1 if is_heartbeat else 0,
            "screenshot_path": screenshot_path,
            "dup_of": dup_of,
            "frame_hash": frame_hash,
            "pruned": 0,
            "screen_w": screen_w,
            "screen_h": screen_h,
            "created_at": now,
        }
        return self.store.insert_frame(row)
