"""Real-time capture liveness — is each machine still running `capture start`?

The capture loop writes a `frames` row every tick, and at minimum one heartbeat
row per `idle_heartbeat_seconds` even while the user is idle. So "is this device
capturing right now?" needs no new capture-side signal — it is simply:

    now - MAX(frames.ts_utc for that device) <= live_window  =>  up

`evaluate()` derives that read-only snapshot (used by `/api/live` and the CLI).
`sweep()` additionally compares each device against its stored `device_status`
and appends a `capture_status_events` row on every up<->down flip, so an admin
can see exactly when a machine stopped (or started) logging. `StatusMonitor` runs
`sweep()` on a timer inside the dashboard process so that log stays current even
when nobody has the page open.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from . import clock
from .config import Config
from .db.store import Store

log = logging.getLogger("digest.monitor")

UP = "up"
DOWN = "down"

_DAY_MS = 86_400_000


def classify(last_frame_ts: Optional[int], now: int, window_ms: int) -> str:
    """up if the newest frame is within the live window, else down."""
    if last_frame_ts is None:
        return DOWN
    return UP if (now - last_frame_ts) <= window_ms else DOWN


def _window_ms(cfg: Config) -> int:
    return int(cfg.monitor.live_window_seconds * 1000)


def _device_view(row: Any, now: int, window_ms: int) -> dict[str, Any]:
    last = row["last_frame_ts"]
    status = classify(last, now, window_ms)
    return {
        "device_id": row["device_id"],
        "hostname": row["hostname"],
        "os": row["os"],
        "person_id": row["person_id"],
        "person": row["display_name"],
        "department": row["department"],
        "status": status,
        "last_frame_ts": last,
        "silent_ms": (now - last) if last is not None else None,
        "last_app": row["last_app"],
        "last_state": row["last_state"],
        "frames_total": row["frames_total"] or 0,
        "frames_24h": row["frames_24h"] or 0,
    }


def evaluate(store: Store, cfg: Config, now: Optional[int] = None) -> dict[str, Any]:
    """Read-only liveness snapshot across every registered device."""
    now = now if now is not None else clock.now_ms()
    window_ms = _window_ms(cfg)
    rows = store.device_liveness(now - _DAY_MS)
    devices = [_device_view(r, now, window_ms) for r in rows]
    up = sum(1 for d in devices if d["status"] == UP)
    return {
        "generated_at": now,
        "live_window_seconds": cfg.monitor.live_window_seconds,
        "summary": {
            "capturing": up,
            "stopped": len(devices) - up,
            "total": len(devices),
        },
        "devices": devices,
    }


def sweep(store: Store, cfg: Config, now: Optional[int] = None) -> list[dict[str, Any]]:
    """Evaluate every device, persist current state, and append a transition row
    for each up<->down flip (and each device's first observation). Returns the
    transitions detected this pass."""
    now = now if now is not None else clock.now_ms()
    window_ms = _window_ms(cfg)
    transitions: list[dict[str, Any]] = []
    for row in store.device_liveness(now - _DAY_MS):
        did = row["device_id"]
        last = row["last_frame_ts"]
        status = classify(last, now, window_ms)
        prev = store.get_device_status(did)
        prev_status = prev["status"] if prev else None
        if prev_status != status:
            gap_ms = (now - last) if (status == DOWN and last is not None) else None
            store.insert_status_event(
                device_id=did, person_id=row["person_id"], status=status,
                prev_status=prev_status, ts=now, last_frame_ts=last, gap_ms=gap_ms,
            )
            transitions.append({
                "device_id": did, "hostname": row["hostname"],
                "person": row["display_name"], "status": status,
                "prev_status": prev_status, "ts": now, "gap_ms": gap_ms,
            })
        store.upsert_device_status(
            device_id=did, person_id=row["person_id"], status=status,
            last_frame_ts=last, since_ts=now, updated_at=now,
        )
    return transitions


def status_events(store: Store, limit: int = 50) -> list[dict[str, Any]]:
    return [dict(r) for r in store.list_status_events(limit)]


class StatusMonitor:
    """Background thread that sweeps liveness on a timer inside the dashboard
    process, so the status log records up/down changes even with no page open.
    Daemon thread; a fresh Store per sweep (sqlite connections are per-thread)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._sweep_once()  # seed current state immediately on startup
        self._thread = threading.Thread(
            target=self._run, name="status-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        interval = max(5.0, float(self.cfg.monitor.sweep_seconds))
        while not self._stop.wait(interval):
            self._sweep_once()

    def _sweep_once(self) -> None:
        try:
            with Store(self.cfg.db_path) as s:
                changes = sweep(s, self.cfg)
            for c in changes:
                log.info("device %s (%s) -> %s", c["hostname"], c["person"], c["status"])
        except Exception:  # a monitor error must never take the dashboard down
            log.exception("status sweep failed")
