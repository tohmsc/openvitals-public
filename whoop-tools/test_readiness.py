import json, os, tempfile, time, unittest

os.environ["WHOOP_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import db, readiness

TODAY = "2026-11-19"


def day(k):
    return readiness._shift(TODAY, k)


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect()
        for t in ("daily_night", "api_recovery"):
            self.c.execute(f"DELETE FROM {t}")
        self.c.execute("CREATE TABLE IF NOT EXISTS estimates (date TEXT, kind TEXT, version TEXT, value REAL, ok INTEGER,"
                       " why TEXT, row TEXT, inputs TEXT, computed_at REAL, PRIMARY KEY (date, kind, version))")
        self.c.execute("DELETE FROM estimates")
        # 20 ordinary nights: HRV 55-65 ms, resting HR 45-47
        for k in range(1, 21):
            self.c.execute("INSERT INTO daily_night(date, rhr, hrv, n_rr) VALUES(?,?,?,?)",
                           (day(-k), 45 + k % 3, 55 + k % 11, 5000))
        self.c.commit()

    def tearDown(self):
        self.c.rollback()
        self.c.close()

    def night(self, rhr, hrv):
        self.c.execute("INSERT OR REPLACE INTO daily_night(date, rhr, hrv, n_rr) VALUES(?,?,?,5000)", (TODAY, rhr, hrv))

    def strain(self, values):
        for k, v in enumerate(values, 1):
            self.c.execute("INSERT INTO estimates(date, kind, version, value, ok) VALUES(?, 'strain', 'v1', ?, 1)", (day(-k), v))

    def test_learning_without_own_nights(self):
        self.c.execute("DELETE FROM daily_night WHERE date < ?", (day(-3),))
        self.night(46, 60)
        r = readiness.evaluate(self.c, TODAY)
        self.assertEqual(r["level"], "learning")
        self.assertEqual(r["signals"], [])

    def test_an_ordinary_night_is_balanced(self):
        self.night(46, 60)
        r = readiness.evaluate(self.c, TODAY)
        self.assertEqual(r["level"], "balanced")
        self.assertEqual({s["key"]: s["flag"] for s in r["signals"]}, {"hrv": "neutral", "rhr": "neutral"})

    def test_hrv_crash_is_strained_and_with_high_rhr_run_down(self):
        self.night(46, 35)
        self.assertEqual(readiness.evaluate(self.c, TODAY)["level"], "strained")
        self.night(55, 35)
        self.assertEqual(readiness.evaluate(self.c, TODAY)["level"], "rundown")

    def test_good_night_with_steady_load_is_primed(self):
        self.night(43, 75)
        self.strain([10, 12, 9, 11, 13, 8, 10] * 4)
        r = readiness.evaluate(self.c, TODAY)
        self.assertEqual({s["key"]: s["flag"] for s in r["signals"]}, {"hrv": "good", "rhr": "good", "load": "good"})
        self.assertEqual(r["level"], "primed")
        self.assertAlmostEqual(r["acwr"], 1.0, places=2)

    def test_load_spike_flags_bad(self):
        self.night(46, 60)
        self.strain([18] * 7 + [8] * 21)
        r = readiness.evaluate(self.c, TODAY)
        load = next(s for s in r["signals"] if s["key"] == "load")
        self.assertEqual(load["flag"], "bad")          # 18 / 10.5 = 1.71
        self.assertEqual(r["level"], "strained")

    def test_breathing_rate_rise(self):
        self.night(46, 60)
        for k in range(1, 15):
            self.c.execute("INSERT INTO estimates(date, kind, version, value, ok, row) VALUES(?, 'sleep', 'v1', 80, 1, ?)",
                           (day(-k), json.dumps({"resp_rate": 14.0 + (k % 3) * 0.2})))
        self.c.execute("INSERT INTO estimates(date, kind, version, value, ok, row) VALUES(?, 'sleep', 'v1', 80, 1, ?)",
                       (TODAY, json.dumps({"resp_rate": 16.5})))
        resp = next(s for s in readiness.evaluate(self.c, TODAY)["signals"] if s["key"] == "resp")
        self.assertEqual(resp["flag"], "bad")


if __name__ == "__main__":
    unittest.main()
