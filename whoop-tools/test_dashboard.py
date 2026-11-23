#!/usr/bin/env python3
"""Tests for dashboard.py endpoints on an empty DB and malformed inputs."""
import os, tempfile, unittest

os.environ.setdefault("WHOOP_DB", os.path.join(tempfile.mkdtemp(prefix="whoop-dash-"), "t.db"))

import dashboard, db  # noqa: E402


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self._orig_db = db.DB_PATH
        self._orig_demo = dashboard.DEMO
        db.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="whoop-dash-"), "t.db")
        dashboard.DEMO = False
        self.client = dashboard.app.test_client()

    def tearDown(self):
        db.DB_PATH = self._orig_db
        dashboard.DEMO = self._orig_demo

    def test_empty_db_endpoints_return_sane_json(self):
        with self.client.get("/") as r0:
            self.assertEqual(r0.status_code, 200)
        r = self.client.get("/api/summary")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["recovery"], [])
        self.assertEqual(j["today_hr"], [])
        self.assertEqual(j["alerts"], [])
        rl = self.client.get("/api/live")
        self.assertEqual(rl.status_code, 200)
        self.assertFalse(rl.get_json()["live"])
        ra = self.client.get("/api/alarm")
        self.assertEqual(ra.status_code, 200)
        self.assertFalse(ra.get_json()["enabled"])

    def test_corrupt_live_state_does_not_500(self):
        c = db.connect()
        try:
            db.set_state(c, "live_hr_now", "not-json")
        finally:
            c.close()
        r = self.client.get("/api/live")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["live"])

        c = db.connect()
        try:
            db.set_state(c, "live_hr_now", '{"hr": 65, "ts": null}')
        finally:
            c.close()
        r = self.client.get("/api/live")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["live"])

    def test_non_object_json_bodies_return_400_not_500(self):
        for path in ("/api/feel", "/api/alarm"):
            for payload in ("[1]", '"str"', "123", "null"):
                r = self.client.post(path, headers={"X-Vitals": "1"},
                                     data=payload, content_type="application/json")
                self.assertEqual(r.status_code, 400, (path, payload, r.status_code))


if __name__ == "__main__":
    unittest.main()
