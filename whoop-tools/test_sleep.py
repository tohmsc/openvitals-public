#!/usr/bin/env python3
"""Tests for sleep.py (falling asleep / waking up from movement + heart rate), on synthetic nights.

Run:  .venv/bin/python -m unittest test_sleep -v
"""
import os, tempfile, time, unittest

_TMP = tempfile.mkdtemp(prefix="whoop-test-")
os.environ["WHOOP_DB"] = os.path.join(_TMP, "test.db")

import db, decode, sleep  # noqa: E402

MORNING = time.mktime(time.strptime("2026-11-19", "%Y-%m-%d"))   # local midnight of the day you woke
STILL, TOSS, MOVING = 0.002, 0.06, 0.15                            # g per one-second step


def at(hhmm, day=0):
    """Unix time of a local clock time; evening times belong to the day before."""
    h, m = map(int, hhmm.split(":"))
    return MORNING + day * 86400 + (h - 24 if h >= 12 and day == 0 and h >= 18 else h) * 3600 + m * 60


class SleepTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect()
        self.c.executescript(decode.EXTRA)
        self.c.execute(sleep.SCHEMA)
        for t in ("ble_hr", "motion_min", "sleep_night", "api_sleep"):
            self.c.execute(f"DELETE FROM {t}")
        self.c.commit()

    def tearDown(self):                     # other test modules share this database
        self.c.rollback()
        for t in ("ble_hr", "motion_min", "sleep_night", "api_sleep"):
            self.c.execute(f"DELETE FROM {t}")
        self.c.commit()
        self.c.close()

    def span(self, a, b, move, hr, hr_only=False):
        """Fill [a, b) minute by minute: movement per one-second step and a steady heart rate."""
        for m in range(int(a), int(b), 60):
            if not hr_only:
                self.c.execute("INSERT OR REPLACE INTO motion_min(minute,move_sum,n) VALUES(?,?,?)", (m, move * 59, 59))
            for s in range(0, 60, 2):
                self.c.execute("INSERT OR REPLACE INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)", (m + s, hr, ""))
        self.c.commit()

    def typical(self):
        self.span(at("19:00"), at("23:00"), MOVING, 75)       # evening, up and about
        self.span(at("23:00"), at("03:00"), STILL, 50)        # asleep
        self.span(at("03:00"), at("03:12"), MOVING, 72)       # up for 12 minutes
        self.span(at("03:12"), at("07:00"), STILL, 49)
        for t in ("01:00", "05:00", "05:40"):                 # turning over, a minute each
            self.span(at(t), at(t) + 60, TOSS, 55)
        self.span(at("07:00"), at("08:00"), MOVING, 70)       # morning

    def test_finds_falling_asleep_and_waking(self):
        self.typical()
        n = sleep.night(self.c, MORNING, now=at("09:00"))
        self.assertLessEqual(abs(n["onset"] - at("23:00")), 5 * 60)
        self.assertLessEqual(abs(n["wake"] - at("07:00")), 5 * 60)
        self.assertEqual(n["wakeups"], 1)                      # the 12 minutes; turning over is not waking
        # the 7-minute movement window smears a wake-up by a few minutes either side
        self.assertTrue(12 * 60 <= n["awake_s"] <= 20 * 60, n["awake_s"])
        self.assertAlmostEqual(n["asleep_s"], 8 * 3600 - 12 * 60, delta=15 * 60)
        self.assertEqual(n["status"], "done")
        self.assertEqual(n["date"], "2026-11-19")
        self.assertIn("woke up once", n["detail"])

    def test_lying_still_before_bed_is_not_the_night(self):
        self.typical()
        self.span(at("20:00"), at("20:25"), STILL, 56)         # sofa, very still, well before bed
        n = sleep.night(self.c, MORNING, now=at("09:00"))
        self.assertLessEqual(abs(n["onset"] - at("23:00")), 5 * 60)

    def test_dreaming_heart_rate_is_still_sleep(self):
        """REM sleep: lying still with heart rate 20 over the night's low (the lab check found the
        old 15 bpm rule cut such stretches out and started nights hours late)."""
        self.span(at("19:00"), at("23:00"), MOVING, 75)
        self.span(at("23:00"), at("07:00"), STILL, 50)
        self.span(at("23:20"), at("00:10"), STILL, 70)         # 50 min of still, raised heart rate
        self.span(at("07:00"), at("08:00"), MOVING, 70)
        n = sleep.night(self.c, MORNING, now=at("09:00"))
        self.assertLessEqual(abs(n["onset"] - at("23:00")), 5 * 60)
        self.assertEqual(n["wakeups"], 0)

    def test_long_wake_in_the_night_does_not_split_it(self):
        self.span(at("19:00"), at("23:00"), MOVING, 75)
        self.span(at("23:00"), at("02:00"), STILL, 50)
        self.span(at("02:00"), at("02:45"), MOVING, 70)        # up for 45 minutes
        self.span(at("02:45"), at("07:00"), STILL, 50)
        self.span(at("07:00"), at("08:00"), MOVING, 70)
        n = sleep.night(self.c, MORNING, now=at("09:00"))
        self.assertLessEqual(abs(n["onset"] - at("23:00")), 5 * 60)
        self.assertLessEqual(abs(n["wake"] - at("07:00")), 5 * 60)
        self.assertEqual(n["wakeups"], 1)

    def test_night_in_progress_until_the_morning_data_arrives(self):
        self.span(at("19:00"), at("23:00"), MOVING, 75)
        self.span(at("23:00"), at("05:00"), STILL, 50)        # the strap's history only reaches 05:00
        n = sleep.night(self.c, MORNING, now=at("05:10"))
        self.assertEqual(n["status"], "ongoing")
        self.assertIn("so far", n["headline"])
        # live heart rate (no movement yet) shows you up and about: the night is over
        self.span(at("05:00"), at("05:30"), None, 72, hr_only=True)
        n = sleep.night(self.c, MORNING, now=at("05:30"))
        self.assertEqual(n["status"], "done")

    def test_live_heart_rate_at_sleeping_level_does_not_end_the_night(self):
        self.span(at("19:00"), at("23:00"), MOVING, 75)
        self.span(at("23:00"), at("05:00"), STILL, 50)
        self.span(at("05:00"), at("05:30"), None, 51, hr_only=True)   # still asleep, Mac streaming
        self.assertEqual(sleep.night(self.c, MORNING, now=at("05:30"))["status"], "ongoing")

    def test_no_strap_no_night(self):
        self.assertIsNone(sleep.night(self.c, MORNING, now=at("09:00")))

    def test_compares_with_your_whoop_years_until_you_have_three_nights(self):
        for k in range(12):                                   # 6 h asleep on WHOOP
            self.c.execute("INSERT INTO api_sleep(id,start,end,nap,total_in_bed_ms,total_awake_ms) VALUES(?,?,?,0,?,?)",
                           (f"s{k}", f"2025-01-{k + 1:02d}T22:00:00.000Z", f"2025-01-{k + 2:02d}T06:00:00.000Z",
                            6.5 * 3600_000, 30 * 60_000))
        self.typical()
        n = sleep.night(self.c, MORNING, now=at("09:00"))
        self.assertEqual(n["usual_s"], 6 * 3600)
        self.assertIn("more than usual", n["headline"])

    def test_update_never_turns_a_finished_night_back_into_one_in_progress(self):
        self.typical()
        sleep.update(self.c, now=at("09:00"))
        self.c.execute("DELETE FROM ble_hr WHERE ts >= ?", (at("07:00"),))
        self.c.execute("DELETE FROM motion_min WHERE minute >= ?", (at("07:00"),))
        sleep.update(self.c, now=at("09:00") - 3600)          # e.g. a clock change or a partial rebuild
        row = self.c.execute("SELECT status FROM sleep_night WHERE date='2026-11-19'").fetchone()
        self.assertEqual(row["status"], "done")


class MotionTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect()
        self.c.executescript(decode.EXTRA)
        self.c.execute("DELETE FROM motion_min")
        self.c.commit()

    def tearDown(self):
        self.c.rollback()
        self.c.close()

    def test_steps_across_a_gap_are_not_movement(self):
        T = 1_795_000_020
        decode.add_motion(self.c, {T: (0, 0, 1), T + 1: (0, 0.1, 1), T + 30: (1, 0, 0)})   # 29 s gap
        r = self.c.execute("SELECT move_sum, n FROM motion_min").fetchone()
        self.assertEqual(r["n"], 1)
        self.assertAlmostEqual(r["move_sum"], 0.1)

    def test_runs_add_up_within_a_minute(self):
        T = 1_795_000_020
        decode.add_motion(self.c, {T: (0, 0, 1), T + 1: (0, 0, 1.2)})
        decode.add_motion(self.c, {T + 2: (0, 0, 1), T + 3: (0, 0, 1.1)})
        r = self.c.execute("SELECT move_sum, n FROM motion_min").fetchone()
        self.assertEqual(r["n"], 2)
        self.assertAlmostEqual(r["move_sum"], 0.3)


if __name__ == "__main__":
    unittest.main()
