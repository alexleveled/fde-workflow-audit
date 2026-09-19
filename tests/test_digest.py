"""Phase 4 digest-generator tests (stdlib unittest).

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from fde_audit.analysis import commit as commit_mod
from fde_audit.analysis.prepare import prepare
from fde_audit.config import DigestCfg
from fde_audit.db.store import Store
from fde_audit.digest import generate_digest, render_markdown, save_digest

from .synth import seed_frames, seed_people
from .test_analysis import _temp_cfg


class DigestTest(unittest.TestCase):
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

    def test_generate_markdown_shape(self):
        d = generate_digest(self.cfg, self.store, person_id=self.pid)
        md = d["content_md"]
        self.assertTrue(md.startswith("# FDE Audit Digest"))
        self.assertIn("weekly", md)                     # default period label
        self.assertIn("## All opportunities", md)
        self.assertIn("| # | Type |", md)               # ranked table header
        self.assertIn("Recommended focus", md)          # focus section present
        self.assertEqual(d["token_cost"], 0)            # model-free
        self.assertTrue(d["focus"])

    def test_consolidation_in_markdown(self):
        d = generate_digest(self.cfg, self.store, person_id=self.pid)
        self.assertIn("## Consolidation candidates", d["content_md"])
        self.assertIn("reconciliation", d["content_md"])

    def test_loaded_rate_dollarizes(self):
        cfg = dataclasses.replace(
            self.cfg, digest=DigestCfg(loaded_hourly_rate=100.0))
        d = generate_digest(cfg, self.store, person_id=self.pid)
        # at least one priced opportunity => a $/wk figure appears
        self.assertIn("/wk", d["content_md"])
        self.assertIn("$", d["content_md"])
        self.assertIn("loaded rate", d["content_md"])

    def test_save_and_retrieve(self):
        d = generate_digest(self.cfg, self.store, person_id=self.pid)
        did = save_digest(self.store, d)
        rows = self.store.list_digests(person_id=self.pid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], did)
        self.assertEqual(rows[0]["period"], "weekly")
        # round-trip the stored markdown
        got = self.store.find_digest(did[:8], person_id=self.pid)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["content_md"], d["content_md"])

    def test_org_digest_not_saveable(self):
        d = generate_digest(self.cfg, self.store, person_id=None)  # all-people
        with self.assertRaises(ValueError):
            save_digest(self.store, d)

    def test_empty_ledger_digest(self):
        # a fresh person with no observations still renders a valid digest
        pid2 = self.store.add_person("Empty", "finance", created_at=1)
        d = generate_digest(self.cfg, self.store, person_id=pid2)
        self.assertIn("No opportunities recorded yet", d["content_md"])
        self.assertFalse(d["focus"])

    def test_render_markdown_period_label(self):
        rep = generate_digest(self.cfg, self.store, person_id=self.pid, period="monthly")
        self.assertIn("FDE Audit Digest — monthly", rep["content_md"])
        self.assertIn("Recommended focus this monthly", rep["content_md"])


if __name__ == "__main__":
    unittest.main()
