"""Executive-brief tests (stdlib unittest).

Run:  python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fde_audit.analysis import commit as commit_mod
from fde_audit.analysis.prepare import prepare
from fde_audit.brief import commit_brief, prepare_brief
from fde_audit.db.store import Store

from .synth import seed_frames, seed_people
from .test_analysis import _temp_cfg


class BriefTest(unittest.TestCase):
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

    def test_prepare_packet_shape(self):
        info = prepare_brief(self.cfg, self.store, self.pid, period="all")
        self.assertTrue(info["opportunity_count"] > 0)
        packet = json.loads(Path(info["packet_path"]).read_text(encoding="utf-8"))
        self.assertEqual(packet["person_id"], self.pid)
        self.assertEqual(packet["period"], "all")
        # the ranked ledger and the activity stats are both in the packet
        self.assertEqual(packet["report"]["totals"]["opportunity_count"],
                         info["opportunity_count"])
        self.assertTrue(packet["activity"]["top_apps"])
        self.assertGreater(packet["activity"]["work_minutes"], 0)
        # the round-trip contract Claude follows
        self.assertEqual(packet["output_md_path"], info["output_md_path"])
        self.assertIn("digest brief commit --packet", packet["commit_command"])
        self.assertIn("EXECUTIVE BRIEF", packet["instructions"])

    def test_prepare_empty_ledger_writes_nothing(self):
        # a second person with no observations
        pid2 = self.store.add_person(display_name="Ghost", department="ops",
                                     created_at=1, email=None)
        info = prepare_brief(self.cfg, self.store, pid2, period="7d")
        self.assertEqual(info["opportunity_count"], 0)
        self.assertIsNone(info["packet_path"])

    def test_commit_round_trip(self):
        info = prepare_brief(self.cfg, self.store, self.pid, period="all")
        md = "# Executive brief\n\n" + ("This period was dominated by manual "
             "reconciliation across Excel and QuickBooks. " * 10)
        Path(info["output_md_path"]).write_text(md, encoding="utf-8")
        result = commit_brief(self.cfg, self.store, info["packet_path"])
        self.assertEqual(result["person_id"], self.pid)
        rows = self.store.list_digests(person_id=self.pid)
        saved = [r for r in rows if r["id"] == result["digest_id"]]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["period"], "exec-all")

    def test_commit_refuses_missing_or_stub_md(self):
        info = prepare_brief(self.cfg, self.store, self.pid, period="7d")
        with self.assertRaises(ValueError):        # not written yet
            commit_brief(self.cfg, self.store, info["packet_path"])
        Path(info["output_md_path"]).write_text("# TODO", encoding="utf-8")
        with self.assertRaises(ValueError):        # too short to be a brief
            commit_brief(self.cfg, self.store, info["packet_path"])
        self.assertFalse(self.store.list_digests(person_id=self.pid))
