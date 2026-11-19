#!/usr/bin/env python3
"""Tests for stress.py (stress monitor) on synthetic days.

Run:  .venv/bin/python -m unittest test_stress -v
"""
import os, tempfile, time, unittest

_TMP = tempfile.mkdtemp(prefix="whoop-stress-test-")
os.environ["WHOOP_DB"] = os.path.join(_TMP, "test.db")

import db, decode, sleep, stress  # noqa: E402

DATE = "2026-11-10"
MIDNIGHT = time.mktime(time.strptime(DATE, "%Y-%m-%d"))
TABLES = ("ble_hr", "motion_min", "sleep_night", "daily_night", "api_profile", "api_recovery", "api_sleep",
          "stress", "sync_state")
CALM = (60, (1000, 1040))         # heart rate, alternating beat intervals: RMSSD 40 ms
MEDIUM = (64, (920, 956))         # +4 bpm, RMSSD 36 ms
TENSE = (72, (833, 848))          # +12 bpm, RMSSD 15 ms


def at(h, m=0, day=0):
    return MIDNIGHT + day * 86400 + h * 3600 + m * 60


class StressTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect()
        self.c.executescript(decode.EXTRA)
        self.c.execute(sleep.SCHEMA)
        self.c.execute(stress.SCHEMA)
        self.clear()

    def tearDown(self):                     # other test modules share this database
        self.clear()
        self.c.close()

    def clear(self):
        for t in TABLES:
            self.c.execute(f"DELETE FROM {t}")
        self.c.commit()

    def fill(self, a, b, heart=CALM, move=0.01, beats=True):
        """1 Hz heart rate (with beat intervals unless beats=False) and per-minute wrist movement."""
        hr, rr = heart
        self.c.executemany("INSERT OR REPLACE INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)",
                           [(t, hr, str(rr[int(t) % 2]) if beats else "") for t in range(int(a), int(b))])
        if move is not None:
            self.c.executemany("INSERT OR REPLACE INTO motion_min(minute,move_sum,n) VALUES(?,?,60)",
                               [(m, move * 60) for m in range(int(a), int(b), 60)])
        self.c.commit()

    def calm_history(self, days=2):
        """Two hours of calm, still, awake time on each of the days before DATE."""
        for k in range(1, days + 1):
            self.fill(at(14, day=-k), at(16, day=-k))

    def day(self, h=18):
        return stress.day(self.c, DATE, now=at(h), base=stress.baseline(self.c, DATE, now=at(h)))

    def by_time(self, d, h, m=0):
        return next(w for w in d["series"] if w["ts"] == at(h, m))

    # ---- scoring ----
    def test_calm_is_low_and_tense_while_still_is_higher(self):
        self.calm_history()
        self.fill(at(9), at(10), CALM)
        self.fill(at(10), at(11), MEDIUM)
        self.fill(at(11), at(12), TENSE)
        d = self.day()
        self.assertEqual(d["baseline"]["source"], "own")
        self.assertAlmostEqual(d["baseline"]["hr"], 60)
        self.assertAlmostEqual(d["baseline"]["rmssd"], 40, delta=1)
        calm, med, tense = self.by_time(d, 9, 30), self.by_time(d, 10, 30), self.by_time(d, 11, 30)
        self.assertEqual(stress.level(calm["score"]), "Low")
        self.assertEqual(stress.level(med["score"]), "Medium")
        self.assertEqual(stress.level(tense["score"]), "High")
        self.assertLess(calm["score"], med["score"])
        self.assertLessEqual(tense["score"], 3.0)

    def test_moving_is_not_stress_and_the_heart_gets_time_to_settle(self):
        self.calm_history()
        self.fill(at(9), at(9, 30), TENSE, move=0.4)          # walking: HR up, wrist moving
        self.fill(at(9, 30), at(10), CALM)
        d = self.day()
        self.assertEqual(self.by_time(d, 9, 10)["state"], "moving")
        self.assertIsNone(self.by_time(d, 9, 10)["score"])
        self.assertEqual(self.by_time(d, 9, 30)["state"], "settling")
        self.assertEqual(self.by_time(d, 9, 35)["state"], "ok")

    def test_exercise_zone_heart_rate_is_not_stress_even_with_a_still_wrist(self):
        self.calm_history()
        self.fill(at(9), at(9, 20), (140, (420, 435)))         # cycling: wrist still, heart working
        d = self.day()
        self.assertEqual(self.by_time(d, 9, 5)["state"], "active")
        self.assertIsNone(self.by_time(d, 9, 5)["score"])

    def test_it_says_when_it_cant_tell(self):
        self.calm_history()
        self.fill(at(9), at(9, 30), CALM, beats=False)        # heart rate but no clean beats
        self.fill(at(10), at(10, 30), CALM, move=None)        # movement not handed over yet
        d = self.day(h=12)
        self.assertEqual(self.by_time(d, 9, 5)["state"], "few_beats")
        self.assertEqual(self.by_time(d, 10, 5)["state"], "no_motion")
        self.assertEqual(self.by_time(d, 11)["state"], "no_data")             # off the wrist
        self.assertEqual(d["now"]["state"], "no_data")                       # nothing in the last 30 min
        self.assertIsNone(d["now"]["level"])
        self.assertIn("Nothing from the strap", d["now"]["why"])

    def test_now_keeps_the_last_score_while_newer_movement_data_is_on_its_way(self):
        self.calm_history()
        self.fill(at(11, 30), at(11, 45), TENSE)
        self.fill(at(11, 45), at(12), TENSE, move=None)
        d = self.day(h=12)
        self.assertEqual(d["now"]["level"], "High")
        self.assertEqual(d["now"]["ts"], at(11, 40))
        self.assertTrue(d["now"]["pending"])
        self.fill(at(11, 45), at(12), TENSE, move=0.5)       # ...and then it turns out you were moving
        d = self.day(h=12)
        self.assertEqual(d["now"]["state"], "moving")
        self.assertIsNone(d["now"]["level"])

    def test_sleep_is_not_scored(self):
        self.calm_history()
        self.fill(at(0), at(6), (48, (1240, 1300)), move=0.001)
        self.c.execute("INSERT INTO sleep_night(date,onset,wake,asleep_s,status) VALUES(?,?,?,?,'done')",
                       (DATE, at(0, 10), at(5, 50), 5 * 3600))
        self.c.commit()
        d = self.day()
        self.assertEqual(self.by_time(d, 3)["state"], "asleep")
        self.assertEqual(self.by_time(d, 0)["state"], "ok")                # before falling asleep

    # ---- baseline ----
    def test_baseline_starts_from_defaults_and_says_so(self):
        self.fill(at(9), at(10), CALM)
        b = stress.baseline(self.c, DATE, now=at(18))
        self.assertEqual(b["source"], "default")
        self.assertIn("Still learning your calm", b["text"])
        self.c.execute("INSERT INTO daily_night(date,rhr,hrv) VALUES(?,?,?)", ("2026-11-09", 50, 60))
        self.c.commit()
        b = stress.baseline(self.c, DATE, now=at(18))
        self.assertEqual(b["source"], "night")
        self.assertAlmostEqual(b["hr"], 50 + stress.FROM_NIGHT["hr"])
        self.assertIn("guessed from your latest night", b["text"])

    def test_one_day_of_calm_is_not_enough_for_your_own_baseline(self):
        self.calm_history(days=1)                             # 24 windows on one day
        self.assertNotEqual(stress.baseline(self.c, DATE, now=at(18))["source"], "own")
        self.calm_history(days=2)
        b = stress.baseline(self.c, DATE, now=at(18))
        self.assertEqual(b["source"], "own")
        self.assertIn("own calm", b["text"])

    def test_todays_stress_does_not_move_todays_baseline(self):
        self.calm_history()
        before = stress.baseline(self.c, DATE, now=at(18))
        self.fill(at(9), at(12), TENSE)
        after = stress.baseline(self.c, DATE, now=at(18))
        self.assertEqual(before["hr"], after["hr"])

    def test_update_stores_the_day_and_only_rewrites_what_changed(self):
        self.calm_history()
        self.fill(at(9), at(10), CALM)
        stress.update(self.c, now=at(10))
        n = self.c.execute("SELECT COUNT(*) n FROM stress WHERE date=?", (DATE,)).fetchone()["n"]
        self.assertEqual(n, 120)                              # 00:00-10:00 in 5-minute windows
        stamp = self.c.execute("SELECT computed_at FROM stress WHERE ts=?", (at(9),)).fetchone()["computed_at"]
        time.sleep(0.01)
        stress.update(self.c, now=at(10))
        again = self.c.execute("SELECT computed_at FROM stress WHERE ts=?", (at(9),)).fetchone()["computed_at"]
        self.assertEqual(stamp, again)
        self.assertEqual(stress.level(self.c.execute("SELECT score FROM stress WHERE ts=?", (at(9, 30),)).fetchone()["score"]),
                         "Low")


if __name__ == "__main__":
    unittest.main()
