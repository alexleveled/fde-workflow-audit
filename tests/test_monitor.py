"""Tests for real-time capture liveness (fde_audit.monitor)."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from fde_audit import monitor
from fde_audit.config import load_config
from fde_audit.db.store import Store
from tests.synth import _frame


class MonitorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        cfg = load_config()
        # A tight, explicit window so the arithmetic in the tests is obvious.
        cfg = dataclasses.replace(
            cfg,
            db_path=self.tmp / "t.db",
            monitor=dataclasses.replace(cfg.monitor, live_window_seconds=90.0),
        )
        self.cfg = cfg
        self.win_ms = int(cfg.monitor.live_window_seconds * 1000)
        self.store = Store(cfg.db_path)
        self.store.init_db()
        self.pid = self.store.add_person("Alex", "developer", 1)
        self.did = self.store.upsert_device(self.pid, "HOST-A", "Windows", "UTC", 1)

    def tearDown(self) -> None:
        self.store.close()

    def _frame_at(self, ts: int, hostdid: str | None = None) -> None:
        self.store.insert_frame(
            _frame(self.pid, hostdid or self.did, ts=ts, app="Code", title="x"))

    # -- classify ---------------------------------------------------------
    def test_classify_window(self) -> None:
        now = 1_000_000
        self.assertEqual(monitor.classify(now - 10_000, now, self.win_ms), monitor.UP)
        self.assertEqual(monitor.classify(now - self.win_ms, now, self.win_ms), monitor.UP)
        self.assertEqual(monitor.classify(now - self.win_ms - 1, now, self.win_ms), monitor.DOWN)
        self.assertEqual(monitor.classify(None, now, self.win_ms), monitor.DOWN)

    # -- evaluate ---------------------------------------------------------
    def test_evaluate_up_and_down(self) -> None:
        now = 10_000_000
        self._frame_at(now - 3_000)
        snap = monitor.evaluate(self.store, self.cfg, now)
        self.assertEqual(snap["summary"], {"capturing": 1, "stopped": 0, "total": 1})
        dev = snap["devices"][0]
        self.assertEqual(dev["status"], monitor.UP)
        self.assertEqual(dev["last_app"], "Code")
        self.assertEqual(dev["silent_ms"], 3_000)

        later = now + self.win_ms + 60_000
        snap2 = monitor.evaluate(self.store, self.cfg, later)
        self.assertEqual(snap2["summary"]["stopped"], 1)
        self.assertEqual(snap2["devices"][0]["status"], monitor.DOWN)

    def test_evaluate_device_with_no_frames_is_down(self) -> None:
        snap = monitor.evaluate(self.store, self.cfg, 10_000_000)
        self.assertEqual(snap["devices"][0]["status"], monitor.DOWN)
        self.assertIsNone(snap["devices"][0]["last_frame_ts"])

    # -- sweep / transition log ------------------------------------------
    def test_sweep_logs_transitions_once(self) -> None:
        now = 10_000_000
        self._frame_at(now - 2_000)

        first = monitor.sweep(self.store, self.cfg, now)
        self.assertEqual(len(first), 1)
        self.assertEqual((first[0]["status"], first[0]["prev_status"]), ("up", None))

        # No change on a second sweep at the same instant.
        self.assertEqual(monitor.sweep(self.store, self.cfg, now), [])

        # Silence past the window -> a single down transition, gap recorded.
        later = now + self.win_ms + 60_000
        down = monitor.sweep(self.store, self.cfg, later)
        self.assertEqual(len(down), 1)
        self.assertEqual(down[0]["status"], "down")
        self.assertEqual(down[0]["gap_ms"], later - (now - 2_000))
        # ...and it stays down without re-logging.
        self.assertEqual(monitor.sweep(self.store, self.cfg, later + 1000), [])

        # A fresh frame brings it back up -> one up transition.
        self._frame_at(later + 2_000)
        up = monitor.sweep(self.store, self.cfg, later + 3_000)
        self.assertEqual([(c["status"], c["prev_status"]) for c in up], [("up", "down")])

        events = monitor.status_events(self.store)
        self.assertEqual([e["status"] for e in events], ["up", "down", "up"])

    def test_since_ts_holds_across_unchanged_sweeps(self) -> None:
        now = 10_000_000
        self._frame_at(now - 2_000)
        monitor.sweep(self.store, self.cfg, now)
        row1 = self.store.get_device_status(self.did)
        self._frame_at(now + 1_000)
        monitor.sweep(self.store, self.cfg, now + 2_000)
        row2 = self.store.get_device_status(self.did)
        # status unchanged (still up) => since_ts must not move, last_frame_ts must.
        self.assertEqual(row1["since_ts"], row2["since_ts"])
        self.assertGreater(row2["last_frame_ts"], row1["last_frame_ts"])


if __name__ == "__main__":
    unittest.main()
