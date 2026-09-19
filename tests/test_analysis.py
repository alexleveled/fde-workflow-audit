"""Phase 2 analysis-pass tests (stdlib unittest, no extra deps).

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from fde_audit.analysis import commit as commit_mod
from fde_audit.analysis import taxonomy
from fde_audit.analysis.mining import mine
from fde_audit.analysis.prepare import prepare
from fde_audit.analysis.sessionize import sessionize
from fde_audit.config import load_config
from fde_audit.db.store import Store

from .synth import H_A, H_B, seed_frames, seed_people

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.toml"


def _temp_cfg(tmp: Path):
    base = load_config(REPO_CONFIG)
    data_dir = tmp / "data"
    return dataclasses.replace(
        base,
        data_dir=data_dir,
        db_path=data_dir / "digest.db",
        screenshots_dir=data_dir / "screenshots",
        pause_file=data_dir / "PAUSE",
    )


def _frame(**kw):
    base = dict(id="x", ts_utc=0, app_name="A", window_title="t", url_domain=None,
               idle_ms=0, frame_hash=H_A, screenshot_path=None, dup_of=None,
               pruned=0, local_day="2026-07-13")
    base.update(kw)
    return base


class SessionizeTest(unittest.TestCase):
    def test_boundaries(self):
        frames = [
            _frame(id="1", ts_utc=0, app_name="Excel", window_title="a"),
            _frame(id="2", ts_utc=5000, app_name="Excel", window_title="a"),   # continue
            _frame(id="3", ts_utc=10000, app_name="Word", window_title="a"),   # app-change
            _frame(id="4", ts_utc=15000, app_name="Word", window_title="b"),   # title-change
            _frame(id="5", ts_utc=90000, app_name="Word", window_title="b"),   # idle-gap (75s)
            _frame(id="6", ts_utc=95000, app_name="Word", window_title="b",
                   frame_hash=H_B),                                            # hash-jump
        ]
        sessions = sessionize(frames, idle_gap_seconds=30, hash_jump_bits=40)
        reasons = [s.boundary_reason for s in sessions]
        self.assertEqual(reasons, ["first", "app-change", "title-change", "idle-gap", "hash-jump"])
        self.assertEqual(sessions[0].frame_count, 2)

    def test_time_cap(self):
        # same app/title/hash, no idle gap, but spanning > max_minutes -> split
        frames = [_frame(id=str(i), ts_utc=i * 5000, app_name="ERP", window_title="ERP")
                  for i in range(200)]  # 200 * 5s = 1000s ~ 16.6 min
        sessions = sessionize(frames, max_minutes=10, idle_gap_seconds=30)
        self.assertGreaterEqual(len(sessions), 2)
        self.assertIn("time-cap", [s.boundary_reason for s in sessions])

    def test_input_density(self):
        frames = [
            _frame(id="1", ts_utc=0, idle_ms=0),        # input
            _frame(id="2", ts_utc=5000, idle_ms=0),     # input
            _frame(id="3", ts_utc=10000, idle_ms=40000),  # reading
            _frame(id="4", ts_utc=15000, idle_ms=45000),  # reading
        ]
        s = sessionize(frames, interval_seconds=5)[0]
        self.assertAlmostEqual(s.input_density, 0.5, places=3)

    def test_representative_resolves_dup_and_dedups(self):
        frames = [
            _frame(id="img", ts_utc=0, screenshot_path="screenshots/a.jpg"),
            _frame(id="dup", ts_utc=5000, screenshot_path=None, dup_of="img"),
        ]
        s = sessionize(frames)[0]
        self.assertEqual(len(s.representatives), 1)             # deduped by path
        self.assertEqual(s.representatives[0].path, "screenshots/a.jpg")

    def test_long_session_start_mid_end(self):
        frames = [_frame(id=str(i), ts_utc=i * 5000,
                         screenshot_path=f"screenshots/{i}.jpg") for i in range(3)]
        s = sessionize(frames, long_session_minutes=0.0)[0]   # force "long"
        self.assertEqual([r.position for r in s.representatives], ["start", "mid", "end"])

    def test_pruned_image_not_offered(self):
        frames = [_frame(id="p", ts_utc=0, screenshot_path="screenshots/a.jpg", pruned=1)]
        s = sessionize(frames)[0]
        self.assertEqual(s.representatives, [])


class MiningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = _temp_cfg(Path(self.tmp.name))
        self.store = Store(cfg.db_path)
        self.store.init_db()
        pid, did = seed_people(self.store)
        seed_frames(self.store, pid, did, screenshots_dir=cfg.screenshots_dir)
        self.cfg, self.pid = cfg, pid
        self.sessions = sessionize(self.store.work_frames(pid),
                                   interval_seconds=5, hash_jump_bits=40)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_pingpong_detected(self):
        m = mine(self.sessions, pingpong_min_switches=4)
        self.assertTrue(m.pingpong, "expected an Excel<->QuickBooks ping-pong")
        pair = {m.pingpong[0]["app_a"], m.pingpong[0]["app_b"]}
        self.assertEqual(pair, {"Excel", "QuickBooks"})
        self.assertGreaterEqual(m.pingpong[0]["switches"], 4)

    def test_ritual_recurs_two_days(self):
        m = mine(self.sessions, ritual_min_days=2)
        seqs = [tuple(r["sequence"]) for r in m.rituals]
        self.assertIn(("Outlook", "Excel", "Chrome"), seqs)

    def test_top_apps_and_bigrams(self):
        m = mine(self.sessions)
        self.assertTrue(m.top_apps)
        self.assertTrue(m.app_bigrams)


class TaxonomyTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(taxonomy.validate_capability("Reconciliation"), "reconciliation")

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            taxonomy.validate_capability("frobnicating")
        with self.assertRaises(ValueError):
            taxonomy.validate_opportunity_type("magic")


class EndToEndCommitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _temp_cfg(Path(self.tmp.name))
        self.store = Store(self.cfg.db_path)
        self.store.init_db()
        self.pid, did = seed_people(self.store)
        seed_frames(self.store, self.pid, did, screenshots_dir=self.cfg.screenshots_dir)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _prepare_and_label(self):
        info = prepare(self.cfg, self.store, self.pid)
        packet = json.loads(Path(info["packet_path"]).read_text(encoding="utf-8"))
        # simulate the vision pass: label every session from its app name
        cap_by_app = {
            "Outlook": "comms-external", "Excel": "data-analysis",
            "Chrome": "research", "QuickBooks": "reconciliation", "ERP": "data-entry",
        }
        for s in packet["sessions"]:
            s["label"] = {
                "capability": cap_by_app.get(s["app_name"], "other"),
                "task": f"working in {s['app_name']}",
                "activity": s["app_name"],
                "category": "work",
                "low_value": s["app_name"] == "Chrome" and s["interaction_mode"] == "reading",
                "friction": None,
            }
        return info, packet

    def test_full_pipeline(self):
        info, packet = self._prepare_and_label()
        path = Path(info["packet_path"])
        path.write_text(json.dumps(packet), encoding="utf-8")

        result = commit_mod.commit(self.cfg, self.store, path)
        self.assertEqual(result["summaries"], len(packet["sessions"]))
        self.assertGreater(result["observations"], 0)

        # run marked done; watermark advanced
        run = self.store.get_analysis_run(info["run_id"])
        self.assertEqual(run["status"], "done")
        self.assertIsNotNone(self.store.analysis_watermark(self.pid))

        # summaries carry the controlled capability
        caps = {r["capability"] for r in self.store.query(
            "SELECT capability FROM summaries WHERE person_id=?", (self.pid,))}
        self.assertIn("reconciliation", caps)

        # a typed, priced integrate observation exists for the ping-pong
        integ = self.store.query(
            "SELECT * FROM observations WHERE person_id=? AND opportunity_type='integrate'",
            (self.pid,))
        self.assertTrue(integ)
        self.assertIsNotNone(integ[0]["est_minutes_per_week"])

    def test_consolidation_when_capability_spans_apps(self):
        info = prepare(self.cfg, self.store, self.pid)
        packet = json.loads(Path(info["packet_path"]).read_text(encoding="utf-8"))
        # label Excel AND QuickBooks both as reconciliation -> same function, 2 apps
        both = {"Excel": "reconciliation", "QuickBooks": "reconciliation"}
        for s in packet["sessions"]:
            s["label"] = {"capability": both.get(s["app_name"], "coding"),
                          "task": None, "activity": None, "category": None,
                          "low_value": False, "friction": None}
        Path(info["packet_path"]).write_text(json.dumps(packet), encoding="utf-8")
        commit_mod.commit(self.cfg, self.store, info["packet_path"])

        con = self.store.query(
            "SELECT * FROM observations WHERE person_id=? AND opportunity_type='consolidate'",
            (self.pid,))
        self.assertTrue(con, "expected a consolidate observation for reconciliation")
        row = next(r for r in con if r["pattern_key"] == "consolidate:reconciliation")
        self.assertEqual(row["occurrences"], 2)                 # Excel + QuickBooks
        self.assertIn("Excel", row["description"])
        self.assertIn("QuickBooks", row["description"])

    def test_consolidation_snapshot_does_not_inflate(self):
        # committing the same-labeled data twice must not double the app count
        for _ in range(2):
            info = prepare(self.cfg, self.store, self.pid, all_frames=True)
            packet = json.loads(Path(info["packet_path"]).read_text(encoding="utf-8"))
            both = {"Excel": "reconciliation", "QuickBooks": "reconciliation"}
            for s in packet["sessions"]:
                s["label"] = {"capability": both.get(s["app_name"], "coding"),
                              "task": None, "activity": None, "category": None,
                              "low_value": False, "friction": None}
            Path(info["packet_path"]).write_text(json.dumps(packet), encoding="utf-8")
            commit_mod.commit(self.cfg, self.store, info["packet_path"])
        row = self.store.query_one(
            "SELECT occurrences FROM observations WHERE person_id=? AND pattern_key='consolidate:reconciliation'",
            (self.pid,))
        self.assertEqual(row["occurrences"], 2)   # snapshot, not 4

    def test_watermark_excludes_reanalyzed(self):
        info, packet = self._prepare_and_label()
        Path(info["packet_path"]).write_text(json.dumps(packet), encoding="utf-8")
        commit_mod.commit(self.cfg, self.store, info["packet_path"])
        # second prepare with the watermark should see no new frames
        info2 = prepare(self.cfg, self.store, self.pid)
        self.assertEqual(info2["frame_count"], 0)

    def test_invalid_capability_rolls_back(self):
        info = prepare(self.cfg, self.store, self.pid)
        packet = json.loads(Path(info["packet_path"]).read_text(encoding="utf-8"))
        for s in packet["sessions"]:
            s["label"] = {"capability": "not-a-real-capability", "low_value": False,
                          "friction": None, "task": None, "activity": None, "category": None}
        Path(info["packet_path"]).write_text(json.dumps(packet), encoding="utf-8")
        with self.assertRaises(ValueError):
            commit_mod.commit(self.cfg, self.store, info["packet_path"])
        # nothing written; run failed
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) c FROM summaries")["c"], 0)
        self.assertEqual(self.store.get_analysis_run(info["run_id"])["status"], "failed")


if __name__ == "__main__":
    unittest.main()
