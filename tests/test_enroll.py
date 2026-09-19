"""Fleet auto-enrollment tests (stdlib unittest, no extra deps).

Exercises the identity/department resolution and find-or-create logic with a
fake platform backend, so the domain-joined and off-domain paths are both
covered on any machine.

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fde_audit import enroll
from fde_audit.db.store import Store

from .test_analysis import _temp_cfg


class FakePlatform:
    """Stand-in for a platform backend. `ad` is what AD would return for the
    department (None => off-domain / attribute unset)."""

    def __init__(self, sid="S-1-5-21-1-2-3-1001", login="ACME\\jdoe",
                 host="FIN-PC-01", ad=None):
        self._sid, self._login, self._host, self._ad = sid, login, host, ad

    def user_sid(self):
        return self._sid

    def user_login(self):
        return self._login

    def ad_department(self, sid=None):
        return self._ad

    def hostname(self):
        return self._host

    def tz_name(self):
        return "UTC"


class EnrollTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _temp_cfg(Path(self.tmp.name))
        # The packaged/config default department (what a "Finance package" bakes).
        object.__setattr__(self.cfg.identity, "default_department", "Finance")
        self.store = Store(self.cfg.db_path)
        self.store.init_db()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # -- identity resolution ---------------------------------------------
    def test_identity_prefers_sid(self):
        ext_key, display = enroll.resolve_identity(FakePlatform())
        self.assertEqual(ext_key, "S-1-5-21-1-2-3-1001")
        self.assertEqual(display, "ACME\\jdoe")

    def test_identity_falls_back_to_login_then_host(self):
        no_sid = FakePlatform(sid=None)
        self.assertEqual(enroll.resolve_identity(no_sid)[0], "ACME\\jdoe")
        bare = FakePlatform(sid=None, login=None)
        self.assertEqual(enroll.resolve_identity(bare), ("host:FIN-PC-01", "FIN-PC-01"))

    # -- department precedence -------------------------------------------
    def test_department_uses_config_when_ad_empty(self):
        dept, source = enroll.resolve_department(self.cfg, FakePlatform(ad=None), "sid")
        self.assertEqual((dept, source), ("Finance", "config-default"))

    def test_department_prefers_ad(self):
        dept, source = enroll.resolve_department(
            self.cfg, FakePlatform(ad="Procurement"), "sid")
        self.assertEqual((dept, source), ("Procurement", "active-directory"))

    # -- find-or-create + device -----------------------------------------
    def test_enroll_creates_then_is_idempotent(self):
        plat = FakePlatform(ad=None)
        p1, d1 = enroll.auto_enroll(self.cfg, self.store, plat)
        self.assertEqual(p1["department"], "Finance")          # config fallback
        self.assertEqual(p1["ext_key"], "S-1-5-21-1-2-3-1001")
        p2, d2 = enroll.auto_enroll(self.cfg, self.store, plat)
        self.assertEqual(p1["person_id"], p2["person_id"])     # same person
        self.assertEqual(d1, d2)                               # same device
        self.assertEqual(len(self.store.list_people()), 1)
        self.assertEqual(len(self.store.list_devices()), 1)

    def test_ad_self_corrects_but_outage_keeps_last_known(self):
        p1, _ = enroll.auto_enroll(self.cfg, self.store, FakePlatform(ad="Procurement"))
        self.assertEqual(p1["department"], "Procurement")
        # Employee transfers; AD now says Legal -> record updates in place.
        p2, _ = enroll.auto_enroll(self.cfg, self.store, FakePlatform(ad="Legal"))
        self.assertEqual(p2["person_id"], p1["person_id"])
        self.assertEqual(p2["department"], "Legal")
        # AD goes dark -> keep the last real value, don't reset to config default.
        p3, _ = enroll.auto_enroll(self.cfg, self.store, FakePlatform(ad=None))
        self.assertEqual(p3["department"], "Legal")


if __name__ == "__main__":
    unittest.main()
