"""Phase 3 reporting / observation-confirmation tests (stdlib unittest).

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fde_audit.analysis import commit as commit_mod
from fde_audit.analysis.prepare import prepare
from fde_audit.db.store import Store
from fde_audit.reporting import build_report, render_text

from .synth import seed_frames, seed_people
from .test_analysis import _temp_cfg


class ReportingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _temp_cfg(Path(self.tmp.name))
        self.store = Store(self.cfg.db_path)
        self.store.init_db()
        self.pid, did = seed_people(self.store)
        seed_frames(self.store, self.pid, did, screenshots_dir=self.cfg.screenshots_dir)
        self._analyze()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _analyze(self):
        info = prepare(self.cfg, self.store, self.pid, all_frames=True)
        packet = json.loads(Path(info["packet_path"]).read_text(encoding="utf-8"))
        # Excel + QuickBooks both reconciliation -> a consolidation candidate.
        cap_by_app = {
            "Outlook": "comms-external", "Excel": "reconciliation",
            "Chrome": "research", "QuickBooks": "reconciliation", "ERP": "data-entry",
        }
        for s in packet["sessions"]:
            s["label"] = {
                "capability": cap_by_app.get(s["app_name"], "other"),
                "task": None, "activity": None, "category": None,
                "low_value": s["app_name"] == "Chrome", "friction": None,
            }
        Path(info["packet_path"]).write_text(json.dumps(packet), encoding="utf-8")
        commit_mod.commit(self.cfg, self.store, info["packet_path"])

    def test_report_ranks_and_totals(self):
        rep = build_report(self.cfg, self.store, person_id=self.pid)
        self.assertTrue(rep["opportunities"])
        # ranked by weekly minutes descending (None treated as 0, sorts last)
        mins = [o["est_minutes_per_week"] or 0 for o in rep["opportunities"]]
        self.assertEqual(mins, sorted(mins, reverse=True))
        self.assertEqual(rep["opportunities"][0]["rank"], 1)
        self.assertGreater(rep["totals"]["total_minutes_per_week"], 0)
        # the priced integrate (ping-pong) contributes to the type rollup
        self.assertIn("integrate", rep["totals"]["by_opportunity_type"])

    def test_consolidation_section_cross_app(self):
        rep = build_report(self.cfg, self.store, person_id=self.pid)
        caps = {c["capability"] for c in rep["consolidation"]}
        self.assertIn("reconciliation", caps)
        recon = next(c for c in rep["consolidation"] if c["capability"] == "reconciliation")
        self.assertEqual(recon["app_count"], 2)
        self.assertEqual(set(recon["apps"]), {"Excel", "QuickBooks"})

    def test_render_text_smoke(self):
        rep = build_report(self.cfg, self.store, person_id=self.pid)
        text = render_text(rep)
        self.assertIn("opportunity report", text)
        self.assertIn("CONSOLIDATE" if rep["consolidation"] else "opportunity", text)

    def test_status_filter_excludes_dismissed(self):
        rows = self.store.list_observations(person_id=self.pid)
        target = rows[0]["id"]
        self.store.set_observation_status(target, "dismissed")
        # default report hides dismissed
        rep = build_report(self.cfg, self.store, person_id=self.pid)
        self.assertNotIn(target, [o["id"] for o in rep["opportunities"]])
        # --all-status (statuses=None) shows it again
        rep_all = build_report(self.cfg, self.store, person_id=self.pid, statuses=None)
        self.assertIn(target, [o["id"] for o in rep_all["opportunities"]])

    def test_find_and_set_status_by_prefix_and_key(self):
        rows = self.store.list_observations(person_id=self.pid)
        obs = rows[0]
        # resolve by id prefix
        by_prefix = self.store.find_observation(obs["id"][:8], person_id=self.pid)
        self.assertEqual(len(by_prefix), 1)
        self.assertEqual(by_prefix[0]["id"], obs["id"])
        # resolve by pattern_key
        by_key = self.store.find_observation(obs["pattern_key"], person_id=self.pid)
        self.assertEqual(by_key[0]["id"], obs["id"])
        self.assertEqual(self.store.find_observation("nope-nothing"), [])

    def test_dismiss_survives_reanalysis(self):
        # dismissing a pattern must not be undone by a later commit re-touching it
        rows = self.store.list_observations(person_id=self.pid)
        pp = next(r for r in rows if r["pattern_key"].startswith("pingpong:"))
        self.store.set_observation_status(pp["id"], "dismissed")
        self._analyze()  # re-run over the same data
        again = self.store.query_one(
            "SELECT status FROM observations WHERE id = ?", (pp["id"],))
        self.assertEqual(again["status"], "dismissed")


if __name__ == "__main__":
    unittest.main()
